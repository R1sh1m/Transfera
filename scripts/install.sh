#!/usr/bin/env bash
# ============================================================
#  Transfera — macOS & Linux installer
#  Builds every binary locally from source so no download
#  ever triggers OS security warnings.
#
#  Usage (inside the repo):
#    bash scripts/install.sh
#
#  Flags:
#    --skip-driver     Skip Apple device bridge install
#    --skip-native     Skip C++ helper build (folder backup unaffected)
#    --yes             Non-interactive (assume yes)
# ============================================================
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────
if [ -t 1 ]; then
  CR="\033[0m"; CB="\033[1m"; CG="\033[32m"
  CY="\033[33m"; CC="\033[36m"; CRED="\033[31m"
else
  CR=""; CB=""; CG=""; CY=""; CC=""; CRED=""
fi

step()  { echo -e "\n${CB}${CC}==> $*${CR}"; }
ok()    { echo -e "  ${CG}[OK]${CR} $*"; }
warn()  { echo -e "  ${CY}[WARN]${CR} $*"; }
fail()  { echo -e "  ${CRED}[FAIL]${CR} $*"; exit 1; }

# ── Parse flags ──────────────────────────────────────────────
SKIP_DRIVER=false; SKIP_NATIVE=false; ASSUME_YES=false
for arg in "$@"; do
  case "$arg" in
    --skip-driver) SKIP_DRIVER=true ;;
    --skip-native) SKIP_NATIVE=true ;;
    --yes)         ASSUME_YES=true  ;;
  esac
done

confirm() {
  $ASSUME_YES && return 0
  read -rp "$1 [Y/n] " ans
  [[ -z "$ans" || "$ans" =~ ^[Yy] ]]
}

# ── Detect platform ──────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
  Darwin) PLATFORM=macos ;;
  Linux)  PLATFORM=linux ;;
  *) fail "Unsupported OS: $OS. Use scripts/Install-Transfera.ps1 on Windows." ;;
esac
ok "Platform: $PLATFORM"

# ── Locate repo root ─────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if   [ -f "$SCRIPT_DIR/../run.py" ]; then ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [ -f "$(pwd)/run.py" ];         then ROOT="$(pwd)"
else fail "Cannot find run.py. Run this script from inside the Transfera repo."; fi
ok "Repo: $ROOT"

# ── 1  System dependencies ───────────────────────────────────
step "1/6  System dependencies"

if [ "$PLATFORM" = macos ]; then
  command -v brew &>/dev/null || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  [ -f /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)" || true
  ok "Homebrew present"
  for pkg in python@3.12 node git rust; do
    brew list "$pkg" &>/dev/null && ok "$pkg already present" || brew install "$pkg"
  done
  brew link --force python@3.12 2>/dev/null || true

else  # linux
  if   command -v apt-get &>/dev/null; then
    PM=apt; sudo apt-get update -qq
    INS="sudo apt-get install -y"
  elif command -v dnf &>/dev/null; then
    PM=dnf; INS="sudo dnf install -y"
  elif command -v pacman &>/dev/null; then
    PM=pacman; INS="sudo pacman -S --noconfirm"
  else
    fail "No supported package manager (apt/dnf/pacman). Install Python 3.12, Node 20+, git, rust manually."
  fi
  ok "Package manager: $PM"

  # Python 3.12
  if ! command -v python3.12 &>/dev/null; then
    if [ "$PM" = apt ]; then
      sudo apt-get install -y software-properties-common
      sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
      sudo apt-get update -qq
      $INS python3.12 python3.12-venv python3.12-dev
    elif [ "$PM" = dnf ]; then $INS python3.12 python3.12-devel
    elif [ "$PM" = pacman ]; then $INS python; fi
  fi

  # Node 20+
  if ! command -v node &>/dev/null || [ "$(node -e 'process.stdout.write(process.version.slice(1).split(".")[0])')" -lt 20 ]; then
    curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash - 2>/dev/null || true
    $INS nodejs 2>/dev/null || $INS nodejs npm
  fi

  command -v git &>/dev/null || $INS git

  if ! command -v cargo &>/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path
    # shellcheck source=/dev/null
    source "$HOME/.cargo/env"
  fi

  # Tauri system deps (WebKit / GTK)
  if [ "$PM" = apt ]; then
    $INS build-essential libssl-dev libffi-dev pkg-config \
      libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev 2>/dev/null || \
    $INS build-essential libssl-dev libffi-dev pkg-config \
      libwebkit2gtk-4.0-dev libgtk-3-dev libappindicator3-dev librsvg2-dev 2>/dev/null || \
    warn "Some GTK/WebKit libs may be missing — Tauri build may fail."
  fi
fi

for cmd in python3.12 node npm git cargo; do
  command -v "$cmd" &>/dev/null && ok "$cmd: $(command -v "$cmd")" \
    || fail "'$cmd' not on PATH after install. Restart your shell and re-run."
done

# ── 2  iPhone / device support ───────────────────────────────
step "2/6  iPhone support"
if $SKIP_DRIVER; then
  warn "Skipped (--skip-driver)."
elif [ "$PLATFORM" = macos ]; then
  brew list libimobiledevice &>/dev/null || brew install libimobiledevice usbmuxd
  ok "libimobiledevice ready (macOS native driver ships with macOS)"
else
  case "$PM" in
    apt)    sudo apt-get install -y libimobiledevice-utils usbmuxd ifuse 2>/dev/null ;;
    dnf)    sudo dnf install -y libimobiledevice ifuse 2>/dev/null ;;
    pacman) sudo pacman -S --noconfirm libimobiledevice ifuse 2>/dev/null ;;
  esac && ok "libimobiledevice installed" || warn "Could not install libimobiledevice — iPhone access limited."
fi

# ── 3  Python backend venv ───────────────────────────────────
step "3/6  Python backend (venv + all features, AI runtime included)"
cd "$ROOT"
python3.12 -m venv .venv
# shellcheck source=/dev/null
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r backend/requirements.txt
ok "Backend venv ready"

# ── 4  Frontend ──────────────────────────────────────────────
step "4/6  Frontend (npm ci + production build)"
cd "$ROOT/frontend"
npm ci
npm run build
[ -f dist/index.html ] || fail "dist/index.html missing after build."
ok "Frontend built"
cd "$ROOT"

# ── 5  ExifTool + helpers ────────────────────────────────────
step "5/6  ExifTool + metadata helpers"
.venv/bin/python -c "
from backend.engines.metadata_extractor import _download_exiftool
import sys, pathlib
p = _download_exiftool('backend/bin/exiftool')
sys.exit(0 if p and pathlib.Path(str(p)).exists() else 1)
" && ok "ExifTool pre-seeded" || warn "ExifTool pre-seed failed — it will auto-download on first launch."

$SKIP_NATIVE \
  && warn "C++ WPD helper skipped (--skip-native). Windows-only feature; irrelevant on $PLATFORM." \
  || warn "WPD helper is Windows-only. iPhone access on $PLATFORM uses libimobiledevice."

# ── 6  Tauri desktop app ─────────────────────────────────────
step "6/6  Building the Tauri desktop app"
cd "$ROOT/frontend"
npm run tauri:build
ok "Tauri app built."

if [ "$PLATFORM" = macos ]; then
  DMG=$(find "$ROOT/frontend/src-tauri/target/release/bundle/dmg" -name "*.dmg" 2>/dev/null | head -1)
  if [ -n "$DMG" ]; then
    echo -e "\n${CB}${CG}✓ DMG ready: $DMG${CR}"
    confirm "Open the DMG to install into /Applications?" && open "$DMG" || true
  fi
else
  DEB=$(find "$ROOT/frontend/src-tauri/target/release/bundle/deb" -name "*.deb" 2>/dev/null | head -1)
  APPIMAGE=$(find "$ROOT/frontend/src-tauri/target/release/bundle/appimage" -name "*.AppImage" 2>/dev/null | head -1)
  [ -n "$DEB" ] && ok "Debian package: $DEB"
  [ -n "$DEB" ] && confirm "Install the .deb now? (sudo dpkg -i)" && sudo dpkg -i "$DEB" && ok "Installed via dpkg." || true
  [ -n "$APPIMAGE" ] && chmod +x "$APPIMAGE" && ok "AppImage: $APPIMAGE  →  run with: $APPIMAGE"
fi

cd "$ROOT"
echo ""
echo -e "${CB}${CG}Done! Transfera is ready with every feature on day one:${CR}"
echo "  • Backup engine  •  AI search (ONNX/CPU)  •  ExifTool metadata"
echo "  • iPhone support via libimobiledevice"
echo "  • App data: ~/.local/share/transfera  (macOS: ~/Library/Application Support/transfera)"
echo ""
echo -e "  Dev mode: ${CB}python run.py${CR}  (backend + frontend hot-reload)"
