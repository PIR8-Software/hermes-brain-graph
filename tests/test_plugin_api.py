"""Isolated Brain Workspace backend tests; pytest optional, direct invocation supported."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "catalog" / "dashboard"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

import plugin_api


def raises(status, fn, *args, **kwargs):
    try: fn(*args, **kwargs)
    except HTTPException as exc: assert exc.status_code == status, exc
    else: raise AssertionError(f"expected HTTP {status}")


@contextmanager
def vault():
    temp = tempfile.TemporaryDirectory(); root = Path(temp.name); old = plugin_api.BRAIN_ROOT
    try:
        plugin_api.BRAIN_ROOT = root.resolve(); (root / "folder").mkdir()
        (root / "Alpha.md").write_text("---\ntags: [one, two]\naliases: [First]\n---\n# Alpha\nLinks [[folder/Beta#part|Beta label]], ![[Asset]], [relative](folder/Beta.md?q=1#part), [web](https://example.com). #inline\n", encoding="utf-8")
        (root / "folder" / "Beta.md").write_text("# Beta\nBack to [[../Alpha]]. Search needle here.\n", encoding="utf-8")
        (root / "Disconnected.md").write_text("# Alone\n", encoding="utf-8")
        yield root
    finally:
        plugin_api.BRAIN_ROOT = old; temp.cleanup()


def test_path_safety_read_limits_and_roundtrip():
    with vault() as root:
        bad = ["../../escape.md", "%2e%2e/escape.md", "%252e%252e/escape.md", "..\\escape.md", "/tmp/escape.md", "C:\\escape.md", "Alpha.txt", "x.md\x00", ".trash/x.md", "x/.TRASH/y.md"]
        for value in bad: raises(400, plugin_api._safe_note_path, value)
        outside = root.parent / "outside.md"; outside.write_text("secret", encoding="utf-8")
        (root / "linked.md").symlink_to(outside); raises(400, plugin_api._safe_note_path, "linked.md")
        outside_dir = root.parent / "outside-dir"; outside_dir.mkdir(exist_ok=True); (outside_dir / "secret.md").write_text("secret", encoding="utf-8")
        (root / "escape").symlink_to(outside_dir, target_is_directory=True); raises(400, plugin_api._safe_note_path, "escape/secret.md")
        binary = root / "binary.md"; binary.write_bytes(b"\xff\xfe"); raises(422, plugin_api.read_note, "binary.md")
        huge = root / "huge.md"; huge.write_bytes(b"x" * (plugin_api.MAX_NOTE_BYTES + 1)); raises(413, plugin_api.read_note, "huge.md")
        note = plugin_api.read_note("Alpha.md")
        assert "absolute_path" not in note
        saved = plugin_api.atomic_write("Alpha.md", note["content"] + "exact ✓\n", note["version"])
        assert plugin_api.read_note("Alpha.md")["content"] == saved["content"]
        raises(422, plugin_api.atomic_write, "Alpha.md", None, saved["version"])
        raises(413, plugin_api.atomic_write, "Alpha.md", "x" * (plugin_api.MAX_NOTE_BYTES + 1), saved["version"])


def test_crud_conflicts_collisions_trash_restore():
    with vault() as root:
        original = plugin_api.read_note("Alpha.md"); (root / "Alpha.md").write_text("external", encoding="utf-8")
        raises(409, plugin_api.atomic_write, "Alpha.md", "overwrite", original["version"])
        raises(409, plugin_api.move_note, "Alpha.md", "Moved.md", original["version"])
        raises(409, plugin_api.trash_note, "Alpha.md", original["version"], True)
        created = plugin_api.create_note("new/N.md", "# New\n")
        assert created["path"] == "new/N.md"; raises(409, plugin_api.create_note, "new/N.md", "duplicate")
        raises(400, plugin_api.trash_note, "new/N.md", created["version"], False)
        moved = plugin_api.move_note("new/N.md", "moved/Renamed.md", created["version"])
        assert moved["path"] == "moved/Renamed.md" and moved["link_impact"]["links_rewritten"] is False
        plugin_api.create_note("collision.md", "x"); raises(409, plugin_api.move_note, "moved/Renamed.md", "collision.md", moved["version"])
        trashed = plugin_api.trash_note("moved/Renamed.md", moved["version"], True)
        items = plugin_api.list_trash(); item = next(x for x in items if x["trash_id"] == trashed["trash_id"])
        raises(409, plugin_api.restore_note, item["trash_id"], "collision.md", item["version"])
        raises(409, plugin_api.restore_note, item["trash_id"], "restored.md", "stale")
        restored = plugin_api.restore_note(item["trash_id"], "restored.md", item["version"])
        assert restored["content"] == "# New\n" and not plugin_api.list_trash()
        raises(400, plugin_api._trash_item, "../evil.md")


def test_links_alias_ambiguity_search_and_graph_bounds():
    with vault() as root:
        (root / "Beta.md").write_text("# Root Beta\n", encoding="utf-8")
        index = plugin_api._index(); alpha = plugin_api.read_note("Alpha.md")
        assert alpha["metadata"]["tags"] == ["inline", "one", "two"]
        beta_links = [x for x in alpha["outgoing"] if x["target"] == "folder/Beta.md"]
        assert {x["kind"] for x in beta_links} == {"wiki", "markdown"}
        wiki = next(x for x in beta_links if x["kind"] == "wiki"); assert wiki["anchor"] == "#part" and wiki["label"] == "Beta label"
        assert any(x["embed"] and x["target"] is None for x in alpha["outgoing"])
        assert any(back["path"] == "folder/Beta.md" for back in alpha["backlinks"])
        assert plugin_api._resolve_target("folder/Source.md", "Beta", index[2], index[3], index[4]) == "folder/Beta.md"
        assert plugin_api._resolve_target("Alpha.md", "First", index[2], index[3], index[4]) == "Alpha.md"
        (root / "Beta.md").unlink()
        (root / "dupe").mkdir(); (root / "dupe" / "Beta.md").write_text("---\nalias: First\n---\n", encoding="utf-8")
        index = plugin_api._index()
        assert plugin_api._resolve_target("Alpha.md", "Beta", index[2], index[3], index[4]) is None
        assert plugin_api._resolve_target("Alpha.md", "First", index[2], index[3], index[4]) is None
        resolved = plugin_api.resolve_link("folder/Beta.md", "../Alpha")
        assert resolved == {"status": "resolved", "target": "Alpha.md", "candidates": ["Alpha.md"]}
        ambiguous = plugin_api.resolve_link("Alpha.md", "Beta")
        assert ambiguous["status"] == "ambiguous" and ambiguous["target"] is None
        assert ambiguous["candidates"] == ["dupe/Beta.md", "folder/Beta.md"]
        unresolved = plugin_api.resolve_link("Alpha.md", "Missing")
        assert unresolved == {"status": "unresolved", "target": None, "candidates": []}
        results = plugin_api.search_notes("needle")["results"]; assert results[0]["path"] == "folder/Beta.md" and "needle" in results[0]["snippet"].lower()
        graph = plugin_api.build_graph(); assert "root" not in graph and all("content" not in n and "absolute_path" not in n for n in graph["nodes"])
        assert "Disconnected.md" in {n["path"] for n in graph["nodes"]}
        local = plugin_api.build_graph("local", "Alpha.md"); assert "Alpha.md" in {n["path"] for n in local["nodes"]}
        raises(404, plugin_api.build_graph, "local", "missing.md")
        old_edges, old_unresolved = plugin_api.MAX_EDGES, plugin_api.MAX_UNRESOLVED
        try:
            plugin_api.MAX_EDGES = 1; plugin_api.MAX_UNRESOLVED = 1
            bounded = plugin_api.build_graph(); assert len(bounded["edges"]) <= 1 and len(bounded["unresolved"]) <= 1
        finally: plugin_api.MAX_EDGES, plugin_api.MAX_UNRESOLVED = old_edges, old_unresolved


def test_http_routes_and_validation():
    with vault():
        app = FastAPI(); app.include_router(plugin_api.router, prefix="/api/plugins/brain-graph"); client = TestClient(app); base = "/api/plugins/brain-graph"
        assert client.get(base + "/tree").status_code == 200
        assert client.get(base + "/note", params={"path": "../x.md"}).status_code == 400
        assert client.get(base + "/graph", params={"scope": "bad"}).status_code == 400
        assert client.get(base + "/graph", params={"scope": "local"}).status_code == 400
        created = client.post(base + "/note", json={"path": "HTTP.md", "content": "# HTTP"}); assert created.status_code == 200
        note = created.json(); saved = client.put(base + "/note", json={"path": "HTTP.md", "content": "# Saved", "expected_version": note["version"]}); assert saved.status_code == 200
        stale = client.put(base + "/note", json={"path": "HTTP.md", "content": "bad", "expected_version": note["version"]}); assert stale.status_code == 409
        moved = client.post(base + "/move", json={"from": "HTTP.md", "to": "MovedHTTP.md", "expected_version": saved.json()["version"]}); assert moved.status_code == 200
        deleted = client.post(base + "/delete", json={"path": "MovedHTTP.md", "expected_version": moved.json()["version"], "confirmed": True}); assert deleted.status_code == 200
        trash = client.get(base + "/trash"); assert trash.status_code == 200 and trash.json()["items"]
        item = trash.json()["items"][0]; restored = client.post(base + "/restore", json={"trash_id": item["trash_id"], "path": "RestoredHTTP.md", "expected_version": item["version"]}); assert restored.status_code == 200
        assert client.get(base + "/search", params={"q": "Saved"}).status_code == 200
        assert client.get(base + "/complete-link", params={"q": "Rest"}).status_code == 200
        link = client.get(base + "/resolve-link", params={"source": "Alpha.md", "target": "folder/Beta"})
        assert link.status_code == 200 and link.json()["target"] == "folder/Beta.md"


def test_mutations_are_serialized_and_recheck_version_inside_lock():
    with vault() as root:
        original = plugin_api.read_note("Alpha.md")
        entered = threading.Event(); release = threading.Event(); original_replace = os.replace
        def paused_replace(source, target):
            entered.set(); assert release.wait(2); return original_replace(source, target)
        plugin_api.os.replace = paused_replace
        outcomes = []
        def first():
            try: outcomes.append(("first", plugin_api.atomic_write("Alpha.md", "first", original["version"])["content"]))
            except HTTPException as exc: outcomes.append(("first-error", exc.status_code))
        def second():
            try: outcomes.append(("second", plugin_api.atomic_write("Alpha.md", "second", original["version"])["content"]))
            except HTTPException as exc: outcomes.append(("second-error", exc.status_code))
        try:
            one = threading.Thread(target=first); two = threading.Thread(target=second)
            one.start(); assert entered.wait(2); two.start(); time.sleep(.1)
            assert two.is_alive(), "second mutation must wait for the first mutation lock"
            release.set(); one.join(2); two.join(2)
        finally: plugin_api.os.replace = original_replace
        assert sorted(outcomes) == [("first", "first"), ("second-error", 409)]
        assert (root / "Alpha.md").read_text(encoding="utf-8") == "first"


def test_frontend_release_blocker_guards_are_present():
    source = Path(os.environ.get("BRAIN_UI_PLUGIN", ROOT / "catalog" / "desktop" / "plugin.js")).read_text(encoding="utf-8")
    assert "editRevision" in source and "snapshotRevision" in source and "snapshotContent" in source
    assert "kind:'mutationGuard'" in source
    assert "initialRestore" in source and "validTabs" in source
    assert "/resolve-link?source=" in source
    assert "Unresolved link" in source and "Ambiguous link" in source


def test_move_rewrite_links_preserves_syntax_and_skips_code():
    with vault() as root:
        (root / "Target.md").write_text("# Target\n", encoding="utf-8")
        (root / "Linker.md").write_text(
            "# Linker\n"
            "Wiki bare [[Target]] and alias [[Target|The Target]] and anchor [[Target#sec]].\n"
            "Path form [[Target.md]] and embed ![[Target]].\n"
            "Markdown [label](Target.md) and [label2](Target) and [ext](https://example.com/Target.md).\n"
            "Inline code `[[Target]]` here.\n"
            "```md\n[[Target]]\n[fenced](Target.md)\n```\n"
            "Also [site](https://example.com/Target.md) untouched.\n", encoding="utf-8")
        (root / "Other.md").write_text("# Other\nSee [[Target]].\n", encoding="utf-8")
        (root / "deep").mkdir(); (root / "deep" / "Deep.md").write_text("# Deep\nFrom deep [[../Target]] and [rel](../Target.md).\n", encoding="utf-8")
        version = plugin_api.read_note("Target.md")["version"]
        moved = plugin_api.move_note("Target.md", "archive/Renamed.md", version, rewrite_links=True)
        impact = moved["link_impact"]
        assert impact["backlinks"] == 3 and impact["links_rewritten"] is True
        assert impact["notes_rewritten"] == 3 and impact["rewritten_links"] == 10
        assert impact["skipped_code"] == 3 and impact["skipped_external"] == 2 and impact["skipped_unresolved"] == 0
        linker = (root / "Linker.md").read_text(encoding="utf-8")
        # syntax preserved: aliases, anchors, labels, embeds, bare-vs-path style, .md presence
        assert "[[Renamed]]" in linker and "[[Renamed|The Target]]" in linker and "[[Renamed#sec]]" in linker
        assert "[[Renamed.md]]" in linker and "![[Renamed]]" in linker
        assert "[label](Renamed.md)" in linker and "[label2](Renamed)" in linker
        # code and external links untouched
        assert "`[[Target]]`" in linker and "[[Target]]\n[fenced](Target.md)" in linker
        assert "[ext](https://example.com/Target.md)" in linker and "[site](https://example.com/Target.md)" in linker
        assert "[[Renamed]]" in (root / "Other.md").read_text(encoding="utf-8")
        deep = (root / "deep" / "Deep.md").read_text(encoding="utf-8")
        assert "[[archive/Renamed]]" in deep and "[rel](archive/Renamed.md)" in deep
        # rewritten links resolve to the moved note
        assert plugin_api.resolve_link("Linker.md", "Renamed")["target"] == "archive/Renamed.md"
        assert plugin_api.resolve_link("Linker.md", "Renamed.md")["target"] == "archive/Renamed.md"
        assert plugin_api.resolve_link("deep/Deep.md", "archive/Renamed")["target"] == "archive/Renamed.md"
        edges = plugin_api.build_graph()["edges"]
        assert any(e["source"] == "Linker.md" and e["target"] == "archive/Renamed.md" for e in edges)
        assert any(e["source"] == "deep/Deep.md" and e["target"] == "archive/Renamed.md" for e in edges)
        assert not any(e["target"] == "Target.md" for e in edges)


def test_move_without_rewrite_keeps_backlinks_explicit():
    with vault() as root:
        (root / "Target.md").write_text("# Target\n", encoding="utf-8")
        (root / "Linker.md").write_text("See [[Target]].\n", encoding="utf-8")
        version = plugin_api.read_note("Target.md")["version"]
        moved = plugin_api.move_note("Target.md", "Renamed.md", version, rewrite_links=False)
        assert moved["link_impact"]["links_rewritten"] is False and moved["link_impact"]["backlinks"] == 1
        assert "[[Target]]" in (root / "Linker.md").read_text(encoding="utf-8")


def test_move_rewrite_rolls_back_everything_on_failure():
    with vault() as root:
        (root / "Target.md").write_text("# Target\n", encoding="utf-8")
        (root / "Linker1.md").write_text("A [[Target]]\n", encoding="utf-8")
        (root / "Linker2.md").write_text("B [[Target]]\n", encoding="utf-8")
        originals = {name: (root / name).read_bytes() for name in ("Target.md", "Linker1.md", "Linker2.md")}
        version = plugin_api.read_note("Target.md")["version"]
        original_write = plugin_api._atomic_write_bytes
        calls = []
        def failing(path, data, expected_version):
            calls.append(Path(path).name)
            if len(calls) == 2: plugin_api._fail(409, "Note changed on disk; reload before saving")
            return original_write(path, data, expected_version)
        plugin_api._atomic_write_bytes = failing
        try:
            raises(409, plugin_api.move_note, "Target.md", "Moved.md", version, True)
        finally:
            plugin_api._atomic_write_bytes = original_write
        assert calls == ["Linker1.md", "Linker2.md"]
        for name, data in originals.items(): assert (root / name).read_bytes() == data, name
        assert not (root / "Moved.md").exists() and (root / "Target.md").exists()


def test_move_rewrite_detects_external_edits_and_rolls_back():
    with vault() as root:
        (root / "Target.md").write_text("# Target\n", encoding="utf-8")
        (root / "Linker1.md").write_text("A [[Target]]\n", encoding="utf-8")
        (root / "Linker2.md").write_text("B [[Target]]\n", encoding="utf-8")
        originals = {name: (root / name).read_bytes() for name in ("Target.md", "Linker1.md", "Linker2.md")}
        version = plugin_api.read_note("Target.md")["version"]
        original_write = plugin_api._atomic_write_bytes
        calls = []
        def racing(path, data, expected_version):
            calls.append(Path(path).name)
            if len(calls) == 1:
                (root / "Linker2.md").write_text("external editor wins\n", encoding="utf-8")
            return original_write(path, data, expected_version)
        plugin_api._atomic_write_bytes = racing
        try:
            raises(409, plugin_api.move_note, "Target.md", "Moved.md", version, True)
        finally:
            plugin_api._atomic_write_bytes = original_write
        # Linker2 kept the external content; everything the move had touched is restored
        assert (root / "Linker2.md").read_bytes() == b"external editor wins\n"
        assert (root / "Linker1.md").read_bytes() == originals["Linker1.md"]
        assert (root / "Target.md").read_bytes() == originals["Target.md"]
        assert not (root / "Moved.md").exists()


def test_index_cache_hits_and_invalidates_on_external_edit():
    with vault() as root:
        reads = []
        original_read = plugin_api._read_bytes
        def counting(path):
            reads.append(Path(path).name); return original_read(path)
        plugin_api._read_bytes = counting
        try:
            plugin_api.build_graph(); first = len(reads)
            plugin_api.build_graph(); assert len(reads) == first, "unchanged vault must be served from the index cache"
            (root / "Alpha.md").write_text("changed externally needle2\n", encoding="utf-8")
            graph = plugin_api.build_graph(); assert len(reads) > first, "external edit must re-read the changed note"
        finally:
            plugin_api._read_bytes = original_read
        assert "needle2" in plugin_api.read_note("Alpha.md")["content"]
        assert plugin_api.search_notes("needle2")["total"] == 1
        (root / "Alpha.md").unlink()
        assert "Alpha.md" not in {n["path"] for n in plugin_api.build_graph()["nodes"]}


def test_search_ranking_total_and_truncation():
    with vault() as root:
        (root / "needle.md").write_text("# needle\n", encoding="utf-8")
        (root / "dir").mkdir(); (root / "dir" / "needle-notes.md").write_text("x\n", encoding="utf-8")
        (root / "body.md").write_text("mentions needle needle needle here\n", encoding="utf-8")
        result = plugin_api.search_notes("needle")
        order = [r["path"] for r in result["results"]]
        assert order[0] == "needle.md", order
        assert order[1] == "dir/needle-notes.md", order
        assert "folder/Beta.md" in order and "body.md" in order
        assert result["total"] == len(order) == 4 and result["truncated"] is False
        capped = plugin_api.search_notes("needle", limit=2)
        assert len(capped["results"]) == 2 and capped["total"] == 4 and capped["truncated"] is True
        assert plugin_api.search_notes("   ") == {"results": [], "total": 0, "truncated": False}


def test_http_move_rewrite_and_search_shape():
    with vault() as root:
        (root / "Target.md").write_text("# Target\n", encoding="utf-8")
        (root / "Linker.md").write_text("See [[Target]].\n", encoding="utf-8")
        app = FastAPI(); app.include_router(plugin_api.router, prefix="/api/plugins/brain-graph"); client = TestClient(app); base = "/api/plugins/brain-graph"
        version = client.get(base + "/note", params={"path": "Target.md"}).json()["version"]
        moved = client.post(base + "/move", json={"from": "Target.md", "to": "Renamed.md", "expected_version": version, "rewrite_links": True})
        assert moved.status_code == 200 and moved.json()["link_impact"]["rewritten_links"] == 1
        assert "[[Renamed]]" in (root / "Linker.md").read_text(encoding="utf-8")
        found = client.get(base + "/search", params={"q": "Renamed"}); assert found.status_code == 200
        body = found.json(); assert set(body) == {"results", "total", "truncated"} and body["total"] >= 1


TESTS = [test_path_safety_read_limits_and_roundtrip, test_crud_conflicts_collisions_trash_restore, test_links_alias_ambiguity_search_and_graph_bounds, test_http_routes_and_validation, test_mutations_are_serialized_and_recheck_version_inside_lock, test_frontend_release_blocker_guards_are_present, test_move_rewrite_links_preserves_syntax_and_skips_code, test_move_without_rewrite_keeps_backlinks_explicit, test_move_rewrite_rolls_back_everything_on_failure, test_move_rewrite_detects_external_edits_and_rolls_back, test_index_cache_hits_and_invalidates_on_external_edit, test_search_ranking_total_and_truncation, test_http_move_rewrite_and_search_shape]
if __name__ == "__main__":
    for test in TESTS: test(); print("PASS", test.__name__)
