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
  CY="\033[33m"; CC="\033[36m"; CRED="\033[31m"; CM="\033[35m"
else
  CR=""; CB=""; CG=""; CY=""; CC=""; CRED=""; CM=""
fi

step()  { echo -e "\n${CB}${CC}==> $*${CR}"; }
ok()    { echo -e "  ${CG}[OK]${CR} $*"; }
warn()  { echo -e "  ${CY}[WARN]${CR} $*"; }
fail()  { echo -e "  ${CRED}[FAIL]${CR} $*"; exit 1; }
banner(){ echo -e "${CM}${CB}$*${CR}"; }

# ── ASCII banner ──────────────────────────────────────────────
clear 2>/dev/null || true
echo -e "${CB}${CC}"
cat << 'BANNER'
█████████████████████████████████████████████████████████████████████████████████████████████
█        ██       ██████  █████  ███████  ███      ███        ██        ██       ██████  ████
████  █████  ████  ████    ████   ██████  ██  ████  ██  ████████  ████████  ████  ████    ███
████  █████  ████  ███  ██  ███    █████  ██  ████  ██  ████████  ████████  ████  ███  ██  ██
████  █████  ███   ██  ████  ██  ██  ███  ███  ███████  ████████  ████████  ███   ██  ████  █
████  █████      ████  ████  ██  ███  ██  █████  █████      ████      ████      ████  ████  █
████  █████  ████  ██        ██  ████  █  ███████  ███  ████████  ████████  ████  ██        █
████  █████  ████  ██  ████  ██  █████    ██  ████  ██  ████████  ████████  ████  ██  ████  █
████  █████  ████  ██  ████  ██  ██████   ██  ████  ██  ████████  ████████  ████  ██  ████  █
████  █████  ████  ██  ████  ██  ███████  ███      ███  ████████        ██  ████  ██  ████  █
█████████████████████████████████████████████████████████████████████████████████████████████
BANNER
echo -e "${CR}"
echo -e "  ${CB}Your photos & videos. Your machine. Your rules.${CR}"
echo -e "  ${CY}macOS / Linux installer  —  building everything locally from source${CR}"
echo ""


SKIP_DRIVER=false; SKIP_NATIVE=false; SKIP_AI=false; ASSUME_YES=false
for arg in "$@"; do
  case "$arg" in
    --skip-driver) SKIP_DRIVER=true ;;
    --skip-native) SKIP_NATIVE=true ;;
    --skip-ai)     SKIP_AI=true     ;;
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
echo -e "\n${CB}${CC}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║  📦  Step 1/6 — System Dependencies     ║"
echo "  ╚══════════════════════════════════════════╝${CR}"

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
echo -e "\n${CB}${CC}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║  📱  Step 2/6 — iPhone / Device Support  ║"
echo "  ╚══════════════════════════════════════════╝${CR}"
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
echo -e "\n${CB}${CC}"
echo "  ╔══════════════════════════════════════════════════════════════╗"
echo "  ║  🐍  Step 3/6 — Python Backend (AI runtime & models included) ║"
echo "  ╚══════════════════════════════════════════════════════════════╝${CR}"
cd "$ROOT"
python3.12 -m venv .venv
# shellcheck source=/dev/null
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r backend/requirements.txt
if $SKIP_AI; then
  warn "Skipped (--skip-ai). Semantic search runtime and models not downloaded."
else
  step "Installing on-board AI stack (onnxruntime + tokenizers)..."
  pip install -r backend/requirements-ai.txt -q || warn "AI dependencies install reported issues — continuing."
  step "Downloading MobileCLIP AI models (~207 MB) for Day-1 semantic search..."
  python -c "from backend.engines.clip import ensure_models; ok = ensure_models(); print('AI models ready' if ok else 'Model download skipped')"
fi
ok "Backend venv ready"

# ── 4  Frontend ──────────────────────────────────────────────
echo -e "\n${CB}${CC}"
echo "  ╔═══════════════════════════════════════════════════╗"
echo "  ║  ⚛️   Step 4/6 — Frontend (React + Vite build)    ║"
echo "  ╚═══════════════════════════════════════════════════╝${CR}"
cd "$ROOT/frontend"
npm ci
npm run build
[ -f dist/index.html ] || fail "dist/index.html missing after build."
ok "Frontend built"
cd "$ROOT"

# ── 5  ExifTool + sidecar staging ────────────────────────────
echo -e "\n${CB}${CC}"
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║  🔍  Step 5/6 — ExifTool, Sidecar & Tauri Staging    ║"
echo "  ╚══════════════════════════════════════════════════════╝${CR}"
# System ExifTool (the backend's Windows auto-downloader refuses off
# Windows by design; here the backend resolves via PATH at runtime).
if ! command -v exiftool &>/dev/null; then
  if [ "$PLATFORM" = macos ]; then
    brew install exiftool || warn "brew install exiftool failed."
  else
    case "$PM" in
      apt)    sudo apt-get install -y libimage-exiftool-perl 2>/dev/null ;;
      dnf)    sudo dnf install -y perl-Image-ExifTool 2>/dev/null ;;
      pacman) sudo pacman -S --noconfirm perl-image-exiftool 2>/dev/null ;;
    esac
  fi
fi
command -v exiftool &>/dev/null \
  && ok "System ExifTool: $(exiftool -ver 2>/dev/null || echo present)" \
  || warn "ExifTool not installed — metadata falls back to filesystem timestamps. Install it later (apt: libimage-exiftool-perl, brew: exiftool)."

$SKIP_NATIVE \
  && warn "C++ WPD helper skipped (--skip-native). Windows-only feature; irrelevant on $PLATFORM." \
  || warn "WPD helper is Windows-only. iPhone access on $PLATFORM uses libimobiledevice."

# Frozen Python sidecar -> frontend/src-tauri/resources/transfera-engine/
# (whole one-dir folder: binary + _internal/ runtime, shipped as a Tauri
# resource). Without this, `tauri build` compiles Rust for minutes and the
# preflight gate fails the bundle (fail fast here instead).
step "Building frozen sidecar (PyInstaller — takes a few minutes)..."
bash "$ROOT/scripts/build-sidecar.sh"
ok "Sidecar staged."

# Windows-only Tauri resources must still EXIST or the Rust build-script
# fails on its resources list. They ship as 0-byte placeholders here; the
# backend ignores empty files and uses the system ExifTool / skips WPD.
mkdir -p "$ROOT/frontend/src-tauri/resources/exiftool_files"
for _ph in wpd_helper.exe exiftool.exe; do
  [ -f "$ROOT/frontend/src-tauri/resources/$_ph" ] || : > "$ROOT/frontend/src-tauri/resources/$_ph"
done
ok "Tauri resource placeholders ensured."

# ── 6  Tauri desktop app ─────────────────────────────────────
echo -e "\n${CB}${CC}"
echo "  ╔════════════════════════════════════════════════════════╗"
echo "  ║  🦀  Step 6/6 — Building the Tauri Desktop App (Rust)  ║"
echo "  ║      Grab a coffee ☕  — Rust compile is the slow part  ║"
echo "  ╚════════════════════════════════════════════════════════╝${CR}"
cd "$ROOT/frontend"
# Preflight validates staging BEFORE the slow Rust compile (fails fast with
# fixes). Then build platform bundles explicitly: tauri.conf.json targets
# NSIS (Windows releases) — without --bundles this step would attempt an
# NSIS build on macOS/Linux and fail after compiling.
node scripts/tauri-preflight.mjs
if [ "$PLATFORM" = macos ]; then
  npx tauri build --bundles dmg
else
  npx tauri build --bundles deb appimage
fi
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
echo -e "${CB}${CG}"
cat << 'DONE'
  ╔══════════════════════════════════════════════════════════════╗
  ║                                                              ║
  ║   ✅  ALL DONE!  Transfera is installed & ready to roll.    ║
  ║                                                              ║
  ║   Everything ships on day one:                               ║
  ║     📁  Backup engine + two-hop verification                 ║
  ║     🤖  AI search  (ONNX / CPU, 100% local)                 ║
  ║     🔍  ExifTool metadata extraction                         ║
  ║     📱  iPhone support via libimobiledevice                  ║
  ║                                                              ║
  ╚══════════════════════════════════════════════════════════════╝
DONE
echo -e "${CR}"
echo -e "  App data lives in:"
echo -e "    macOS  →  ${CB}~/Library/Application Support/transfera${CR}"
echo -e "    Linux  →  ${CB}~/.local/share/transfera${CR}"
echo ""
echo -e "  Dev mode: ${CB}python run.py${CR}  (backend + Vite hot-reload)"
