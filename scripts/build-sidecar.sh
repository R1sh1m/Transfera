#!/usr/bin/env bash
# Transfera v2 — build the frozen Python sidecar for the Tauri shell (macOS/Linux).
# Usage (from repo root):
#   bash scripts/build-sidecar.sh
#
# Steps: ensure .venv (Python 3.12) -> pip install pyinstaller ->
# pyinstaller transfera-engine.spec -> stage the one-dir binary as
# frontend/src-tauri/binaries/transfera-engine-<triple> (Tauri externalBin
# layout; triple matches the Rust target so `cargo check` and `tauri build`
# resolve the binary; no .exe suffix off Windows).
#
# Windows users: use scripts/build-sidecar.ps1 instead (same contract).
set -euo pipefail

if [ "$(uname -s)" = "Windows_NT" ] || [ -n "${WINDIR:-}" ]; then
  echo "This script is for macOS/Linux. On Windows use: powershell -ExecutionPolicy Bypass -File scripts/build-sidecar.ps1" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
OUT_DIR="$ROOT/frontend/src-tauri/binaries"

[ -x "$VENV_PY" ] || { echo "Missing .venv Python at $VENV_PY — run 'bash scripts/install.sh' once first." >&2; exit 1; }
command -v rustc >/dev/null 2>&1 || { echo "rustc not on PATH — install Rust via https://rustup.rs first." >&2; exit 1; }

"$VENV_PY" -m pip install --upgrade pyinstaller

pushd "$ROOT" >/dev/null
"$VENV_PY" -m PyInstaller --noconfirm transfera-engine.spec
popd >/dev/null

BUILT="$ROOT/dist/transfera-engine/transfera-engine"
[ -f "$BUILT" ] || { echo "Expected output missing: $BUILT" >&2; exit 1; }

TRIPLE="$(rustc -vV | awk '/^host:/ {print $2}')"
[ -n "$TRIPLE" ] || { echo "Could not determine Rust host triple from 'rustc -vV'." >&2; exit 1; }

mkdir -p "$OUT_DIR"
DEST="$OUT_DIR/transfera-engine-$TRIPLE"
cp -f "$BUILT" "$DEST"
chmod +x "$DEST"
echo "Sidecar staged: $DEST"
du -h "$DEST" | cut -f1 | xargs echo "binary size:"
