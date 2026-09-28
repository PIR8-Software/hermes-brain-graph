"""Safe, conflict-aware Markdown vault API for Brain Workspace."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from fastapi import APIRouter, Body, HTTPException, Query

router = APIRouter()


def _default_brain_root() -> Path:
    env = os.environ.get("HERMES_BRAIN_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / "brain").resolve()


BRAIN_ROOT = _default_brain_root()
MAX_NOTES = 5_000
MAX_EDGES = 20_000
MAX_NOTE_BYTES = 2_000_000
MAX_SEARCH_RESULTS = 100
MAX_UNRESOLVED = 500
WIKILINK_RE = re.compile(r"(!?)\[\[([^\]|#]+)(#[^\]|]*)?(?:\|([^\]]*))?\]\]")
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+[\"'][^)]*[\"'])?\)")
TAG_RE = re.compile(r"(?<![\w/])#([\w][\w/-]*)")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_MUTATION_LOCK = threading.RLock()

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses the process lock only
    fcntl = None


@contextmanager
def _mutation_guard():
    """Serialize plugin mutations in-process and across cooperating Linux workers."""
    with _MUTATION_LOCK:
        BRAIN_ROOT.mkdir(parents=True, exist_ok=True)
        lock_path = BRAIN_ROOT / ".brain-workspace.lock"
        with lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fail(status: int, detail: str):
    raise HTTPException(status_code=status, detail=detail)


def _inside_root(path: Path) -> bool:
    try:
        path.relative_to(BRAIN_ROOT)
        return True
    except ValueError:
        return False


def _fully_unquote(value: str) -> str:
    for _ in range(4):
        decoded = unquote(value)
        if decoded == value:
            return decoded
        value = decoded
    return value


def _validate_relative(relative: str) -> str:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        _fail(400, "Invalid note path")
    value = _fully_unquote(relative).replace("\\", "/")
    if "\x00" in value or re.match(r"^[A-Za-z]:", value):
        _fail(400, "Invalid note path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        _fail(400, "Invalid note path")
    if pure.suffix.lower() != ".md":
        _fail(400, "Only Markdown (.md) notes are allowed")
    if any(part.casefold() == ".trash" for part in pure.parts):
        _fail(400, "The vault trash is not directly editable")
    return pure.as_posix()


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
    """Reject every existing lexical component, not merely the resolved target."""
    rel = path.relative_to(BRAIN_ROOT)
    current = BRAIN_ROOT
    parts = rel.parts if include_leaf else rel.parts[:-1]
    for part in parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            _fail(400, f"Invalid note path: {exc}")
        if stat.S_ISLNK(mode):
            _fail(400, "Symlinks are not allowed in note paths")


def _safe_note_path(relative: str, *, must_exist: bool = True) -> Path:
    rel = _validate_relative(relative)
    lexical = BRAIN_ROOT / rel
    _reject_symlink_components(lexical, include_leaf=True)
    candidate = lexical.resolve(strict=False)
    if not _inside_root(candidate):
        _fail(400, "Invalid note path")
    if must_exist:
        if not lexical.is_file():
            _fail(404, "Note not found")
        if lexical.suffix.lower() != ".md":
            _fail(400, "Invalid note path")
    return lexical


def _note_files() -> list[Path]:
    notes: list[Path] = []
    if not BRAIN_ROOT.is_dir():
        return notes
    for directory, dirs, files in os.walk(BRAIN_ROOT, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if d.casefold() != ".trash" and not (base / d).is_symlink())
        for name in sorted(files):
            candidate = base / name
            if candidate.suffix.lower() != ".md" or candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, ValueError):
                continue
            if _inside_root(resolved) and resolved.is_file():
                notes.append(candidate)
                if len(notes) >= MAX_NOTES:
                    return sorted(notes, key=lambda p: p.relative_to(BRAIN_ROOT).as_posix().casefold())
    return sorted(notes, key=lambda p: p.relative_to(BRAIN_ROOT).as_posix().casefold())


def _read_bytes(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_NOTE_BYTES + 1)
    except OSError as exc:
        _fail(500, f"Could not read note: {exc}")
    if len(data) > MAX_NOTE_BYTES:
        _fail(413, "Note is too large")
    return data


def _version_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _version(path: Path) -> str:
    return _version_bytes(_read_bytes(path))


def _encode_content(content: object) -> bytes:
    if not isinstance(content, str):
        _fail(422, "content must be a string")
    data = content.encode("utf-8")
    if len(data) > MAX_NOTE_BYTES:
        _fail(413, "Note is too large")
    return data


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        _fail(422, "Note is not valid UTF-8")


def _frontmatter(text: str) -> dict:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {"raw": "", "properties": {}, "tags": [], "aliases": []}
    raw = match.group(1); props: dict[str, object] = {}; current = None
    for line in raw.splitlines():
        if re.match(r"^\s+-\s+", line) and current:
            value = re.sub(r"^\s+-\s+", "", line).strip().strip("'\"")
            previous = props.get(current)
            if not isinstance(previous, list): previous = [] if previous in (None, "") else [previous]
            previous.append(value); props[current] = previous; continue
        item = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not item: current = None; continue
        current, value = item.group(1), item.group(2).strip()
        props[current] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()] if value.startswith("[") and value.endswith("]") else value.strip("'\"")
    def values(key: str) -> list[str]:
        value = props.get(key, [])
        if isinstance(value, list): return [str(v).lstrip("#") for v in value]
        return [v.strip().lstrip("#") for v in str(value).split(",") if v.strip()]
    return {"raw": raw, "properties": props, "tags": values("tags") + values("tag"), "aliases": values("aliases") + values("alias")}


def _metadata(text: str) -> dict:
    fm = _frontmatter(text)
    fm["tags"] = sorted(set(fm["tags"] + TAG_RE.findall(FRONTMATTER_RE.sub("", text, count=1))), key=str.casefold)
    return fm


def _normalise_target(raw: str) -> str:
    return _fully_unquote(raw.strip()).replace("\\", "/").split("#", 1)[0].split("?", 1)[0].strip()


_INDEX_CACHE: dict[str, tuple[int, int, bytes, str, dict]] = {}
_INDEX_LOCK = threading.RLock()


def _invalidate_note(relative: str) -> None:
    with _INDEX_LOCK:
        _INDEX_CACHE.pop(relative, None)


def _cached_note(path: Path, rel: str):
    """Read a note through a per-path cache validated by (mtime_ns, size).

    Any external edit changes the stat stamp, so the next index rebuild re-reads
    the file; deleted notes are dropped by _index()."""
    try:
        st = path.stat()
    except OSError:
        _invalidate_note(rel)
        raise HTTPException(status_code=404, detail="Note not found")
    with _INDEX_LOCK:
        entry = _INDEX_CACHE.get(rel)
        if entry and entry[0] == st.st_mtime_ns and entry[1] == st.st_size:
            return entry[2], entry[3], entry[4]
    data = _read_bytes(path); text = _decode(data); meta = _metadata(text)
    with _INDEX_LOCK:
        _INDEX_CACHE[rel] = (st.st_mtime_ns, st.st_size, data, text, meta)
    return data, text, meta


def _index():
    files = _note_files(); rels = [p.relative_to(BRAIN_ROOT).as_posix() for p in files]
    by_path = {r.casefold(): r for r in rels}; by_stem: dict[str, list[str]] = defaultdict(list); by_alias: dict[str, list[str]] = defaultdict(list)
    texts: dict[str, str] = {}; metas: dict[str, dict] = {}
    for path, rel in zip(files, rels):
        try: _, text, meta = _cached_note(path, rel)
        except HTTPException: continue
        texts[rel] = text; metas[rel] = meta; by_stem[path.stem.casefold()].append(rel)
        for alias in meta["aliases"]: by_alias[alias.casefold()].append(rel)
    with _INDEX_LOCK:
        for stale in [key for key in _INDEX_CACHE if key not in set(rels)]:
            _INDEX_CACHE.pop(stale, None)
    return files, rels, by_path, by_stem, by_alias, texts, metas


def _resolve_target(source_rel: str, raw: str, by_path: dict[str, str], by_stem: dict[str, list[str]], by_alias: dict[str, list[str]] | None = None):
    target = _normalise_target(raw)
    if not target or URI_RE.match(target) or target.startswith(("/", "#")): return None
    pure = PurePosixPath(target)
    if pure.suffix.lower() != ".md": pure = PurePosixPath(str(pure) + ".md")
    # Relative links resolve beside the source before considering a vault-root path.
    for candidate in (PurePosixPath(source_rel).parent / pure, pure):
        parts: list[str] = []; escaped = False
        for part in candidate.parts:
            if part in ("", "."): continue
            if part == "..":
                if not parts: escaped = True; break
                parts.pop()
            else: parts.append(part)
        key = "/".join(parts).casefold()
        if not escaped and key in by_path: return by_path[key]
    if "/" not in target:
        stem_matches = by_stem.get(PurePosixPath(target).stem.casefold(), [])
        if len(stem_matches) == 1: return stem_matches[0]
        alias_matches = (by_alias or {}).get(target.casefold(), [])
        if len(alias_matches) == 1: return alias_matches[0]
    return None


def resolve_link(source_rel: str, raw: str) -> dict:
    """Resolve a preview link with the same path/stem/alias semantics as indexing."""
    source = _validate_relative(source_rel); _safe_note_path(source)
    target = _normalise_target(raw)
    if not target or URI_RE.match(target) or target.startswith(("/", "#")):
        return {"status": "unresolved", "target": None, "candidates": []}
    index = _index(); by_path, by_stem, by_alias = index[2], index[3], index[4]
    resolved = _resolve_target(source, target, by_path, by_stem, by_alias)
    if resolved:
        return {"status": "resolved", "target": resolved, "candidates": [resolved]}
    candidates: set[str] = set()
    if "/" not in target:
        candidates.update(by_stem.get(PurePosixPath(target).stem.casefold(), []))
        candidates.update(by_alias.get(target.casefold(), []))
    ordered = sorted(candidates, key=str.casefold)
    return {"status": "ambiguous" if len(ordered) > 1 else "unresolved", "target": None, "candidates": ordered}


def _links_for(rel: str, text: str, index):
    _, _, by_path, by_stem, by_alias, _, _ = index; links = []
    for embed, raw, anchor, label in WIKILINK_RE.findall(text):
        links.append({"raw": raw, "target": _resolve_target(rel, raw, by_path, by_stem, by_alias), "kind": "wiki", "embed": bool(embed), "anchor": anchor or "", "label": label or ""})
    for raw in MARKDOWN_LINK_RE.findall(text):
        links.append({"raw": raw, "target": _resolve_target(rel, raw, by_path, by_stem, by_alias), "kind": "markdown", "embed": False, "anchor": "", "label": ""})
    return links


def _code_ranges(text: str) -> list[tuple[int, int]]:
    """Character ranges of fenced code blocks and inline code spans.

    Link-looking text inside code is documentation, not navigation, so link
    rewriting must never touch it."""
    ranges: list[tuple[int, int]] = []
    fence = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$", re.M | re.S)
    for match in fence.finditer(text):
        ranges.append((match.start(), match.end()))
    for match in re.finditer(r"`[^`\n]+`", text):
        if not any(start <= match.start() < end for start, end in ranges):
            ranges.append((match.start(), match.end()))
    return sorted(ranges)


def _link_tokens(text: str) -> list[tuple[int, int, str, str]]:
    """(raw_start, raw_end, raw_target, kind) for every link token in a note.

    raw_start/raw_end delimit just the target characters, so replacing them
    preserves aliases, anchors, labels and link titles byte for byte."""
    tokens = []
    for match in WIKILINK_RE.finditer(text):
        tokens.append((match.start(2), match.end(2), match.group(2), "wiki"))
    for match in MARKDOWN_LINK_RE.finditer(text):
        tokens.append((match.start(1), match.end(1), match.group(1), "markdown"))
    return sorted(tokens)


def _virtual_index(index, old_rel: str, new_rel: str):
    """Resolution maps as they will exist after the move (used to verify rewrites)."""
    _, rels, _, _, _, _, metas = index
    v_rels = [r for r in rels if r != old_rel] + [new_rel]
    v_by_path = {r.casefold(): r for r in v_rels}
    v_by_stem: dict[str, list[str]] = defaultdict(list); v_by_alias: dict[str, list[str]] = defaultdict(list)
    for rel in v_rels:
        meta = metas.get(rel, {}) if rel != old_rel else metas.get(old_rel, {})
        v_by_stem[PurePosixPath(rel).stem.casefold()].append(rel)
        for alias in meta.get("aliases", []):
            v_by_alias[alias.casefold()].append(rel)
    return v_by_path, v_by_stem, v_by_alias


def _replacement_target(kind: str, raw: str, source_rel: str, new_rel: str, vmaps) -> str | None:
    """Pick a replacement link target that provably resolves to new_rel.

    Style is preserved exactly: bare targets stay bare, path targets become the
    vault-rooted path form, and .md presence is kept. Every candidate is
    verified against the post-move resolution maps; unverifiable candidates
    fall back to the vault-rooted form and, failing that, are skipped."""
    v_by_path, v_by_stem, v_by_alias = vmaps
    clean = raw.strip(); has_ext = clean.lower().endswith(".md")
    new_rel_md = new_rel if new_rel.lower().endswith(".md") else new_rel + ".md"
    suffix = ".md" if has_ext else ""
    bare_form = PurePosixPath(new_rel_md).stem + suffix
    path_form = new_rel_md[:-3] + suffix
    style = path_form if "/" in clean else bare_form
    fallback = new_rel_md[:-3] if kind == "wiki" else new_rel_md
    for candidate in dict.fromkeys([style, fallback]):
        if _resolve_target(source_rel, candidate, v_by_path, v_by_stem, v_by_alias) == new_rel_md:
            return candidate
    return None


def _apply_token_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    out = []; at = 0
    for start, end, replacement in sorted(edits):
        out.append(text[at:start]); out.append(replacement); at = end
    out.append(text[at:])
    return "".join(out)


def _rewrite_plan(old_rel: str, new_rel: str, index) -> tuple[list[tuple[str, bytes, bytes, str]], dict]:
    """Plan inbound-link rewrites for a move. Never touches code or external links.

    Only link tokens that genuinely reference the moved note are considered:
    tokens resolving to old_rel (in code or not) and external URLs that name the
    moved note. Everything else in the vault is ignored. Each plan entry carries
    the snapshot bytes and version it was computed from."""
    _, _, by_path, by_stem, by_alias, texts, _ = index
    vmaps = _virtual_index(index, old_rel, new_rel)
    old_stem = PurePosixPath(old_rel).stem.casefold()
    plan: list[tuple[str, bytes, bytes, str]] = []
    stats = {"rewritten_links": 0, "notes_rewritten": 0, "skipped_code": 0, "skipped_external": 0, "skipped_unresolved": 0, "inbound_links": 0}
    for rel, text in texts.items():
        if rel == old_rel: continue
        ranges = _code_ranges(text); edits = []
        for start, end, raw, kind in _link_tokens(text):
            target = raw.strip()
            if not target: continue
            in_code = any(a <= start < b for a, b in ranges)
            external = kind == "markdown" and (URI_RE.match(target) or target.startswith("#"))
            if external:
                if old_stem and old_stem in target.casefold(): stats["skipped_external"] += 1
                continue
            resolves = _resolve_target(rel, raw, by_path, by_stem, by_alias) == old_rel
            if not resolves: continue
            if in_code: stats["skipped_code"] += 1; continue
            stats["inbound_links"] += 1
            replacement = _replacement_target(kind, raw, rel, new_rel, vmaps)
            if replacement is None: stats["skipped_unresolved"] += 1; continue
            edits.append((start, end, replacement))
        if not edits: continue
        # Expected version and rollback bytes come from the same snapshot the plan
        # was computed from, so external edits between plan and write are caught.
        try: original, _, _ = _cached_note(BRAIN_ROOT / rel, rel)
        except HTTPException: stats["skipped_unresolved"] += len(edits); continue
        plan.append((rel, original, _encode_content(_apply_token_edits(text, edits)), _version_bytes(original)))
        stats["rewritten_links"] += len(edits); stats["notes_rewritten"] += 1
    return plan, stats


def build_graph(scope="global", path=None, tag=None, folder=None) -> dict:
    index = _index(); files, rels, _, _, _, texts, metas = index; chosen = set(rels)
    if folder:
        clean = folder.replace("\\", "/").strip("/")
        if not clean or ".." in PurePosixPath(clean).parts or ".trash" in (p.casefold() for p in PurePosixPath(clean).parts): _fail(400, "Invalid folder")
        chosen = {r for r in chosen if r.startswith(clean + "/")}
    if tag: chosen = {r for r in chosen if tag.lstrip("#") in metas.get(r, {}).get("tags", [])}
    all_edges = set(); unresolved = []; edge_truncated = False; unresolved_truncated = False
    for rel, text in texts.items():
        for link in _links_for(rel, text, index):
            if link["target"] and link["target"] != rel:
                if len(all_edges) < MAX_EDGES: all_edges.add((rel, link["target"], link["kind"]))
                else: edge_truncated = True
            elif link["raw"]:
                if len(unresolved) < MAX_UNRESOLVED: unresolved.append({"source": rel, "target": link["raw"]})
                else: unresolved_truncated = True
    if scope == "local":
        if not path: _fail(400, "Local graph requires a path")
        rel = _validate_relative(path); _safe_note_path(rel)
        neighbours = {rel}
        for source, target, _ in all_edges:
            if source == rel: neighbours.add(target)
            if target == rel: neighbours.add(source)
        chosen &= neighbours
    selected_files = [(p, r) for p, r in zip(files, rels) if r in chosen]
    incoming = defaultdict(int); outgoing = defaultdict(int); edges = []
    for source, target, kind in sorted(all_edges):
        if source in chosen and target in chosen:
            outgoing[source] += 1; incoming[target] += 1; edges.append({"id": len(edges), "source": source, "target": target, "kind": kind})
    nodes = []
    for p, rel in selected_files:
        st = p.stat(); meta = metas.get(rel, {})
        nodes.append({"id": rel, "title": p.stem, "path": rel, "directory": PurePosixPath(rel).parent.as_posix() if "/" in rel else "", "size": st.st_size, "modified": int(st.st_mtime), "outgoing": outgoing[rel], "incoming": incoming[rel], "degree": outgoing[rel] + incoming[rel], "tags": meta.get("tags", [])})
    return {"nodes": nodes, "edges": edges, "unresolved": unresolved, "revision": str(max((int(p.stat().st_mtime_ns) for p in files), default=0)), "stats": {"notes": len(nodes), "edges": len(edges), "indexed_notes": len(texts), "skipped_notes": len(rels)-len(texts), "notes_truncated": len(files) >= MAX_NOTES, "edges_truncated": edge_truncated, "unresolved_truncated": unresolved_truncated}}


def read_note(relative: str) -> dict:
    path = _safe_note_path(relative); data = _read_bytes(path); text = _decode(data); rel = path.relative_to(BRAIN_ROOT).as_posix(); index = _index(); outgoing = _links_for(rel, text, index)
    backlinks = [{"path": other, "title": Path(other).stem} for other, other_text in index[5].items() if other != rel and any(link["target"] == rel for link in _links_for(other, other_text, index))]
    st = path.stat()
    return {"path": rel, "title": path.stem, "content": text, "version": _version_bytes(data), "size": len(data), "modified": int(st.st_mtime), "metadata": _metadata(text), "outgoing": outgoing, "backlinks": backlinks}


def _atomic_write_bytes(path: Path, data: bytes, expected_version: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle: handle.write(data); handle.flush(); os.fsync(handle.fileno())
        # Recheck immediately before replacement while cooperating plugin writers are locked.
        if _version(path) != expected_version: _fail(409, "Note changed on disk; reload before saving")
        os.replace(temp_name, path)
        try:
            dir_fd = os.open(path.parent, os.O_DIRECTORY); os.fsync(dir_fd); os.close(dir_fd)
        except (AttributeError, OSError): pass
    finally:
        if os.path.exists(temp_name): os.unlink(temp_name)


def _force_write_bytes(path: Path, data: bytes) -> None:
    """Best-effort rollback write (no version gate); used only to undo partial work."""
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle: handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name): os.unlink(temp_name)


def atomic_write(relative: str, content: str, expected_version: str) -> dict:
    data = _encode_content(content)
    with _mutation_guard():
        path = _safe_note_path(relative)
        if _version(path) != expected_version: _fail(409, "Note changed on disk; reload before saving")
        _atomic_write_bytes(path, data, expected_version)
        _invalidate_note(path.relative_to(BRAIN_ROOT).as_posix())
    return read_note(relative)


def _prepare_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); _reject_symlink_components(path, include_leaf=False)
    if not _inside_root(path.parent.resolve()): _fail(400, "Invalid destination")


def create_note(relative: str, content: str = "") -> dict:
    data = _encode_content(content)
    with _mutation_guard():
        path = _safe_note_path(relative, must_exist=False); _prepare_parent(path)
        try:
            with path.open("xb") as handle: handle.write(data); handle.flush(); os.fsync(handle.fileno())
        except FileExistsError: _fail(409, "A note already exists at that path")
    return read_note(relative)


def _exclusive_move(source: Path, target: Path) -> None:
    _prepare_parent(target)
    try: os.link(source, target)
    except FileExistsError: _fail(409, "Destination already exists")
    except OSError as exc: _fail(500, f"Could not move note safely: {exc}")
    try: source.unlink()
    except OSError as exc:
        try: target.unlink()
        except OSError: pass
        _fail(500, f"Could not complete move: {exc}")


def move_note(source: str, target: str, expected_version: str, rewrite_links: bool = False) -> dict:
    """Move/rename a note; optionally rewrite inbound links atomically.

    With rewrite_links=True every inbound link that resolves to the old path is
    rewritten in place (wikilink or markdown form preserved, aliases/anchors/
    labels untouched). Code spans, code blocks and external links are never
    touched, and each replacement is verified to resolve to the new path. All
    rewrites plus the move happen under one mutation lock; any failure rolls
    back every file already written and leaves the vault exactly as it was.
    """
    with _mutation_guard():
        old = _safe_note_path(source); new = _safe_note_path(target, must_exist=False)
        if old == new: return read_note(source)
        if _version(old) != expected_version: _fail(409, "Note changed on disk; reload before moving")
        old_rel = old.relative_to(BRAIN_ROOT).as_posix(); new_rel = new.relative_to(BRAIN_ROOT).as_posix()
        index = _index()
        backlink_count = sum(
            1 for rel, text in index[5].items()
            if rel != old_rel and any(_resolve_target(rel, raw, index[2], index[3], index[4]) == old_rel for _, _, raw, _ in _link_tokens(text))
        )
        impact = {"backlinks": backlink_count, "links_rewritten": False}
        plan: list[tuple[str, bytes, bytes, str]] = []
        if rewrite_links:
            plan, stats = _rewrite_plan(old_rel, new_rel, index)
            impact = {"backlinks": backlink_count, "links_rewritten": stats["rewritten_links"] > 0, "notes_rewritten": stats["notes_rewritten"],
                      "rewritten_links": stats["rewritten_links"], "inbound_links": stats["inbound_links"],
                      "skipped_code": stats["skipped_code"], "skipped_external": stats["skipped_external"], "skipped_unresolved": stats["skipped_unresolved"]}
        if _version(old) != expected_version: _fail(409, "Note changed on disk; reload before moving")
        written: list[tuple[Path, bytes]] = []
        try:
            for rel, original, new_bytes, entry_version in plan:
                path = BRAIN_ROOT / rel
                if path.is_symlink() or not _inside_root(path.resolve(strict=False)): _fail(400, "Invalid note path")
                _atomic_write_bytes(path, new_bytes, entry_version)
                written.append((path, original))
            if written and _version(old) != expected_version: _fail(409, "Note changed on disk; reload before moving")
            _exclusive_move(old, new)
        except BaseException:
            for path, original in reversed(written):
                try: _force_write_bytes(path, original)
                except OSError: pass
                _invalidate_note(path.relative_to(BRAIN_ROOT).as_posix())
            raise
        for path, _ in written: _invalidate_note(path.relative_to(BRAIN_ROOT).as_posix())
        _invalidate_note(old_rel); _invalidate_note(new_rel)
    result = read_note(target); result["link_impact"] = impact; return result


def trash_note(relative: str, expected_version: str, confirmed: bool) -> dict:
    if confirmed is not True: _fail(400, "Deletion requires confirmed=true")
    with _mutation_guard():
        path = _safe_note_path(relative)
        if _version(path) != expected_version: _fail(409, "Note changed on disk; reload before deleting")
        rel = path.relative_to(BRAIN_ROOT); trash_id = f"{int(time.time()*1000)}-{uuid.uuid4().hex[:10]}-{rel.name}"; target = BRAIN_ROOT / ".trash" / trash_id
        if _version(path) != expected_version: _fail(409, "Note changed on disk; reload before deleting")
        _exclusive_move(path, target)
    return {"trashed": True, "path": rel.as_posix(), "trash_id": trash_id, "version": _version(target)}


def _trash_item(trash_id: str) -> Path:
    if not isinstance(trash_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+\.md", trash_id or "") or ".." in trash_id or "\x00" in trash_id: _fail(400, "Invalid trash item")
    path = BRAIN_ROOT / ".trash" / trash_id
    if path.is_symlink(): _fail(400, "Invalid trash item")
    if not path.is_file(): _fail(404, "Trash item not found")
    return path


def list_trash() -> list[dict]:
    root = BRAIN_ROOT / ".trash"; results = []
    if not root.is_dir() or root.is_symlink(): return results
    for path in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".md":
            data = _read_bytes(path); results.append({"trash_id": path.name, "name": path.name.split("-", 2)[-1], "size": len(data), "modified": int(path.stat().st_mtime), "version": _version_bytes(data)})
            if len(results) >= MAX_SEARCH_RESULTS: break
    return results


def restore_note(trash_id: str, target: str, expected_version: str) -> dict:
    with _mutation_guard():
        source = _trash_item(trash_id); destination = _safe_note_path(target, must_exist=False)
        if _version(source) != expected_version: _fail(409, "Trash item changed; refresh before restoring")
        _exclusive_move(source, destination)
    return read_note(target)


def search_notes(query: str, limit: int = 50) -> dict:
    """Ranked substring search: title matches outrank path matches outrank body hits."""
    q = query.strip().casefold()
    if not q: return {"results": [], "total": 0, "truncated": False}
    scored = []
    for rel, text in _index()[5].items():
        hay = text.casefold(); pos = hay.find(q); stem = Path(rel).stem.casefold()
        in_content = pos >= 0; in_path = q in rel.casefold()
        if not in_content and not in_path: continue
        occurrences = hay.count(q) if in_content else 0
        score = (120 if stem == q else 80 if stem.startswith(q) else 60 if q in stem else 30 if in_path else 0) + min(occurrences, 8) * 4
        start = max(0, pos - 60) if in_content else 0; end = min(len(text), pos + len(q) + 100) if in_content else 120
        scored.append((-score, rel.casefold(), {"path": rel, "title": Path(rel).stem, "snippet": re.sub(r"\s+", " ", text[start:end]).strip(), "match": in_content, "matches": max(occurrences, 1 if in_path else 0)}))
    scored.sort(key=lambda item: (item[0], item[1]))
    capped = min(limit, MAX_SEARCH_RESULTS)
    return {"results": [item for _, _, item in scored[:capped]], "total": len(scored), "truncated": len(scored) > capped}


@router.get("/graph")
async def graph(scope: str = "global", path: str | None = None, tag: str | None = None, folder: str | None = None):
    if scope not in ("global", "local"): _fail(400, "Invalid graph scope")
    return build_graph(scope, path, tag, folder)

@router.get("/tree")
async def tree():
    return {"notes": [{"path": p.relative_to(BRAIN_ROOT).as_posix(), "title": p.stem, "directory": p.relative_to(BRAIN_ROOT).parent.as_posix() if p.parent != BRAIN_ROOT else "", "modified": int(p.stat().st_mtime), "size": p.stat().st_size} for p in _note_files()]}

@router.get("/note")
async def note(path: str = Query(..., min_length=1, max_length=1000)): return read_note(path)

@router.put("/note")
async def save_note(payload: dict = Body(...)): return atomic_write(payload.get("path", ""), payload.get("content"), payload.get("expected_version", ""))

@router.post("/note")
async def new_note(payload: dict = Body(...)): return create_note(payload.get("path", ""), payload.get("content", ""))

@router.post("/move")
async def move(payload: dict = Body(...)): return move_note(payload.get("from", ""), payload.get("to", ""), payload.get("expected_version", ""), payload.get("rewrite_links") is True)

@router.post("/delete")
async def delete(payload: dict = Body(...)): return trash_note(payload.get("path", ""), payload.get("expected_version", ""), payload.get("confirmed") is True)

@router.get("/trash")
async def trash(): return {"items": list_trash()}

@router.post("/restore")
async def restore(payload: dict = Body(...)): return restore_note(payload.get("trash_id", ""), payload.get("path", ""), payload.get("expected_version", ""))

@router.get("/search")
async def search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(50, ge=1, le=MAX_SEARCH_RESULTS)): return search_notes(q, limit)

@router.get("/complete-link")
async def complete_link(q: str = "", limit: int = Query(20, ge=1, le=100)):
    needle = q.casefold(); results = []
    for p in _note_files():
        rel = p.relative_to(BRAIN_ROOT).as_posix()
        if not needle or needle in rel.casefold() or needle in p.stem.casefold(): results.append({"path": rel, "title": p.stem, "insert": f"[[{rel[:-3]}]]"})
        if len(results) >= limit: break
    return {"results": results}


@router.get("/resolve-link")
async def resolve_preview_link(source: str = Query(..., min_length=1, max_length=1000), target: str = Query(..., min_length=1, max_length=1000)):
    return resolve_link(source, target)
