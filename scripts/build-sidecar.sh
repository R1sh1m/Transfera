#!/usr/bin/env bash
# Transfera v2 — build the frozen Python sidecar for the Tauri shell (macOS/Linux).
# Usage (from repo root):
#   bash scripts/build-sidecar.sh
#
# Steps: ensure .venv (Python 3.12) -> pip install pyinstaller ->
# pyinstaller transfera-engine.spec -> mirror the whole one-dir folder
# (transfera-engine + _internal/ runtime) into
# frontend/src-tauri/resources/transfera-engine/ (Tauri *resources* layout).
#
# The one-dir folder must ship whole: Tauri externalBin only supports single
# files, so staging just the binary leaves its _internal/ runtime behind and
# the installed engine dies on launch with "Failed to load Python DLL".
#
# Windows users: use scripts/build-sidecar.ps1 instead (same contract).
set -euo pipefail

if [ "$(uname -s)" = "Windows_NT" ] || [ -n "${WINDIR:-}" ]; then
  echo "This script is for macOS/Linux. On Windows use: powershell -ExecutionPolicy Bypass -File scripts/build-sidecar.ps1" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
RES_DIR="$ROOT/frontend/src-tauri/resources/transfera-engine"

[ -x "$VENV_PY" ] || { echo "Missing .venv Python at $VENV_PY — run 'bash scripts/install.sh' once first." >&2; exit 1; }

"$VENV_PY" -m pip install --upgrade pyinstaller

pushd "$ROOT" >/dev/null
"$VENV_PY" -m PyInstaller --noconfirm transfera-engine.spec
popd >/dev/null

BUILT_DIR="$ROOT/dist/transfera-engine"
BUILT="$BUILT_DIR/transfera-engine"
[ -f "$BUILT" ] || { echo "Expected output missing: $BUILT" >&2; exit 1; }

# Mirror the whole one-dir folder into Tauri resources (see header comment).
mkdir -p "$RES_DIR"
find "$RES_DIR" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -exec rm -rf {} +
cp -rf "$BUILT_DIR"/. "$RES_DIR"/
[ -f "$RES_DIR/transfera-engine" ] || { echo "Staging failed: transfera-engine binary missing" >&2; exit 1; }
[ -d "$RES_DIR/_internal" ] || { echo "Staging failed: _internal runtime missing" >&2; exit 1; }
chmod +x "$RES_DIR/transfera-engine"
echo "Sidecar staged: $RES_DIR"
du -sh "$RES_DIR" | cut -f1 | xargs echo "one-dir total:"
