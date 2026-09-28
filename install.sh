#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

copy_file() {
  local src="$1"
  local dest="$2"
  mkdir -p "$(dirname "$dest")"
  cp "$src" "$dest"
}

copy_file "$ROOT/catalog/desktop/plugin.js" \
  "$HERMES_HOME/desktop-plugins/brain-graph/plugin.js"

copy_file "$ROOT/catalog/plugin.yaml" \
  "$HERMES_HOME/plugins/brain-graph/plugin.yaml"

mkdir -p "$HERMES_HOME/plugins/brain-graph/dashboard"
copy_file "$ROOT/catalog/dashboard/manifest.json" \
  "$HERMES_HOME/plugins/brain-graph/dashboard/manifest.json"
copy_file "$ROOT/catalog/dashboard/plugin_api.py" \
  "$HERMES_HOME/plugins/brain-graph/dashboard/plugin_api.py"
: > "$HERMES_HOME/plugins/brain-graph/dashboard/__init__.py"

echo "Installed into $HERMES_HOME"
echo "Vault root: \${HERMES_BRAIN_ROOT:-$HOME/brain}"
echo "Enable: hermes plugins enable brain-graph --no-allow-tool-override"
echo "Then remount the dashboard so the API loads, and Ctrl/Cmd+K → Reload desktop plugins."
echo "If the desktop app is on another machine, copy catalog/desktop/plugin.js there (see README)."
