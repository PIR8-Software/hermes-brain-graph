# Brain Workspace for Hermes

A Hermes Agent plugin for a **local Markdown vault**: edit notes, follow `[[wikilinks]]`, search, trash/restore, and explore an interactive graph.

The plugin never uploads your vault. It reads and writes Markdown files on the machine running the Hermes dashboard.

**Version 2.0.1**

## Features

- **Notes** — tabbed editor, split/preview, autosave, optimistic concurrency (409 if the file changed on disk)
- **Wikilinks** — `[[Note]]`, aliases, anchors, and Markdown links; unresolved/ambiguous link feedback
- **Link completion** — caret-aware insert with keyboard (Arrow / Enter / Escape)
- **Search** — ranked substring search with result counts
- **Move / rename** — optional inbound-link rewrite (skips code fences, inline code, and external URLs; rolls back on failure)
- **Recycle bin** — trash and restore with version checks
- **Graph** — global/local scope, folder colours + legend, zoom/fit, label density, filter with match count, selection/pinning, keyboard navigation
- **Safety** — fail-closed path validation (no `..`, no symlinks, no `.trash` edits), size limits, atomic writes under a vault lock

## Requirements

- [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) with the desktop app
- A local folder of `.md` files (default `~/brain`)

## Install

**Linux / macOS (gateway host):**

```bash
git clone https://github.com/PIR8-Software/hermes-brain-graph.git
cd hermes-brain-graph
./install.sh
hermes plugins enable brain-graph --no-allow-tool-override
```

Then remount the dashboard so the Python API loads, and in the desktop app: **Ctrl/Cmd+K → Reload desktop plugins**.

**Windows (desktop app):**

```powershell
git clone https://github.com/PIR8-Software/hermes-brain-graph.git
cd hermes-brain-graph
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Then **Ctrl+K → Reload desktop plugins**. Run `install.sh` on the gateway host for the API.

`$HERMES_HOME` defaults to `~/.hermes`. On Windows, `install.ps1` always writes `plugin.js` to `%LOCALAPPDATA%\hermes\desktop-plugins\brain-graph\` and installs the API only if a Hermes home exists on that machine.

Do **not** look under `C:\Users\<user>\.hermes\desktop-plugins` for the live desktop copy — that path is not the scan directory.

### Vault location

| Variable | Default |
|----------|---------|
| `HERMES_BRAIN_ROOT` | `~/brain` |

Set it on the **gateway/dashboard** process, then remount the dashboard.

```bash
export HERMES_BRAIN_ROOT=/path/to/your/vault
```

### Split desktop / gateway

1. Run `install.sh` on the **gateway** host (API).
2. Run `install.ps1` on the **Windows desktop** (or copy `plugin.js` to `%LOCALAPPDATA%\hermes\desktop-plugins\brain-graph\`).

Python API changes need a dashboard remount on the gateway host. Do not restart the gateway from inside a live gateway chat.

## Use

1. Open the sidebar **Brain** page (or command palette → **Open Brain Workspace**).
2. Edit notes in **Notes**, or switch to **Graph**.
3. Filter, zoom, and click a node to open it. **P** pins, **F** fits, **Enter** opens, **Escape** clears selection.

Rename with **Rewrite inbound links** checked if you want `[[old]]` references updated.

## Architecture

```
Desktop UI  $HERMES_HOME/desktop-plugins/brain-graph/plugin.js
        ↓  /api/plugins/brain-graph/
Backend     $HERMES_HOME/plugins/brain-graph/dashboard/plugin_api.py
  GET  /graph            nodes, edges, unresolved, stats (scope=global|local)
  GET  /tree             vault tree
  GET  /note             content + version + links + metadata
  PUT  /note             save (409 on stale version)
  POST /note             create (409 on duplicate)
  POST /move             rename/move; optional inbound rewrite
  POST /delete           trash (requires confirmed=true)
  GET  /trash            trash listing
  POST /restore          restore from trash
  GET  /search           ranked search
  GET  /complete-link    wikilink completion
  GET  /resolve-link     resolved | ambiguous | unresolved
        ↓ files
Vault       $HERMES_BRAIN_ROOT  (default ~/brain)
```

## Files

| Path | Role |
|------|------|
| `catalog/desktop/plugin.js` | Desktop UI (loaded as-is; no build step) |
| `src/desktop-plugin/plugin.src.js` | Same UI kept as the editable source |
| `catalog/` | Installable plugin (`plugin.yaml`, `desktop/plugin.js`, `dashboard/`) |
| `tests/test_plugin_api.py` | Isolated vault tests (no real notes) |

## Develop

```bash
uv run --with-requirements requirements-dev.txt ruff check .
uv run --with-requirements requirements-dev.txt pytest -q
node src/desktop-plugin/regression-check.mjs
node src/desktop-plugin/zoom-regression.mjs
node src/desktop-plugin/component-layout.test.mjs
```

Tests use disposable temp vaults. They do not touch `~/brain`.

## License

MIT
