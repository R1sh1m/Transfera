```
=============================================================================================
=        ==       ======  =====  =======  ===      ===        ==        ==       ======  ====
====  =====  ====  ====    ====   ======  ==  ====  ==  ========  ========  ====  ====    ===
====  =====  ====  ===  ==  ===    =====  ==  ====  ==  ========  ========  ====  ===  ==  ==
====  =====  ===   ==  ====  ==  ==  ===  ===  =======  ========  ========  ===   ==  ====  =
====  =====      ====  ====  ==  ===  ==  =====  =====      ====      ====      ====  ====  =
====  =====  ====  ==        ==  ====  =  =======  ===  ========  ========  ====  ==        =
====  =====  ====  ==  ====  ==  =====    ==  ====  ==  ========  ========  ====  ==  ====  =
====  =====  ====  ==  ====  ==  ======   ==  ====  ==  ========  ========  ====  ==  ====  =
====  =====  ====  ==  ====  ==  =======  ===      ===  ========        ==  ====  ==  ====  =
=============================================================================================
```

**Two-Stage Verified Media Vaulting Engine**   FastAPI · Electron · React · SQLite

[![CI](https://github.com/R1sh1m/Transfera/actions/workflows/ci.yml/badge.svg)](https://github.com/R1sh1m/Transfera/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Node.js 20+](https://img.shields.io/badge/node-%3E%3D20-green.svg)](https://nodejs.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Platform: Windows 11](https://img.shields.io/badge/platform-Windows%2011-lightgrey.svg)](#)

---

## What is Transfera?

Transfera is a local desktop application for **cryptographically verified media backup**. It transfers photos, videos, and audio from any source (local folders, iPhone, iPad, or USB-attached cameras) to an organised archive destination, whilst verifying every byte in transit before committing the files.

Transfera's **Two-Stage Verified Pipeline** ensures that no silent corruption ever reaches the destination:

```
Source Files ──▶ [Hop 1: Cache] ──▶ [Hop 2: Archive] ──▶ Verified Backup
                  BLAKE3 hash            re-verify
                  .partial write         atomic rename
```

| Hop | From | To | Guarantee |
|-----|------|----|-----------|
| **Hop 1** | Original source files | Local staging cache | Stream-copied with concurrent BLAKE3 hash; only renamed from `.partial` on hash match |
| **Hop 2** | Verified cache copy | Final archive directory | Hash re-verified before atomic placement into organised `YYYY/MM/DD` folder structure |

---

## Key Features

- **BLAKE3 hashing** (SHA-256 fallback) computed *during* the copy stream, requiring no second reads
- **Atomic writes** via `.partial` staging, ensuring a corrupt or interrupted file never lands in the final archive
- **iPhone & iPad support** via native WPD driver integration (optional WSL2 bridge for Tier 2)
- **Live Photo detection** pairs HEIC + MOV files by matching filename, preserving them together
- **Duplicate detection** using exact (hash-based) and near-duplicate (metadata similarity) resolution with per-file controls
- **Crash recovery** interrupted `LOADING` and `ARCHIVED` batch states are automatically resumed on next launch
- **Real-time transfer monitor** WebSocket-driven progress with per-hop bars, ETA, speed, and media thumbnail preview
- **Media library** masonry/list/history views of every archived file, with infinite scroll and thumbnail regeneration
- **SQLite WAL mode** safe concurrent reads during active writes with no database lock contention
- **ExifTool auto-bootstrap** downloads and manages ExifTool automatically with no manual installation needed

---

## Prerequisites

Only **two tools** need to be on the system before running Transfera. Everything else like Python virtual environment, pip packages, npm packages, native build tools, ExifTool, etc is handled automatically on first launch.

On Windows, install both prerequisites in a single command using `winget`:

```powershell
winget install -e --id Python.Python.3.12 ; winget install -e --id OpenJS.NodeJS.LTS
```

---

### 1. Python 3.12

Transfera requires Python **3.12.x** specifically. This version is enforced because several core dependencies (`blake3`, `pillow-heif`) only ship pre-compiled wheels for 3.12, avoiding any need for local compilation.

Install Python 3.12 using the following `winget` command:

```powershell
winget install -e --id Python.Python.3.12
```

*(Or download the installer from [python.org/downloads](https://www.python.org/downloads/) and check **"Add python.exe to PATH"** during setup).*

Verify after installation:

```powershell
python --version
# Python 3.12.x
```

> If multiple Python versions are installed, the `py` launcher (`py -3.12`) is also supported, the run script probes for it automatically.

### 2. Node.js v20 or later

The Electron shell and Vite build pipeline require Node **v20 LTS** or newer.

Install Node.js v20 using the following `winget` command:

```powershell
winget install -e --id OpenJS.NodeJS.LTS
```

*(Or download the installer from [nodejs.org](https://nodejs.org/) — choose the LTS release).*

Verify after installation:

```powershell
node --version
# v20.x.x or higher
```

### That's it

## Quickstart

```powershell
git clone https://github.com/R1sh1m/Transfera.git
cd Transfera
python run.py
```

The orchestrator runs through a self-bootstrapping sequence on first launch (takes 2–4 minutes on a clean machine):

```
[PYTHON]   Locating Python 3.12 interpreter
[STEP 1]   Creating .venv and installing backend dependencies
[STEP 2]   Installing frontend npm packages
[STEP 2.5] Compiling React frontend (Vite)
[STEP 2.6] Building native WPD device helper (requires MSVC — see below)
[STEP 3]   Launching FastAPI backend on :47821
[STEP 3]   Launching Electron + Vite dev shell
```

Subsequent launches skip all setup steps automatically (only runs again when `requirements.txt` or `package.json` change).

Press **Ctrl+C** at any time for a clean teardown of all processes.

---

## iPhone & iPad Support — Native Helper

Transfera's iOS device support is powered by a native C++ helper compiled against the Windows Portable Devices (WPD) API. The run script builds it automatically (requires Visual Studio Build Tools), but **requires Microsoft's C++ compiler (MSVC)** to be present on the system.

Install one of:

- **Visual Studio 2022 Build Tools** via the following command:
  ```powershell
  winget install --id Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive"
  ```
- **Visual Studio 2022** (any edition, free Community edition works) with the **"Desktop development with C++"** workload.

The build script locates the compiler via `vswhere.exe` automatically, no PATH configuration needed.

> **If you skip this step**, Transfera still runs fully. Local folder and network path backups work without the native helper. iPhone/iPad detection simply won't be available until MSVC is installed and the helper is built.

---

## Runner Reference

```powershell
python run.py               # Start everything (recommended)
python run.py --backend     # Backend API only (no Electron window)
python run.py --frontend    # Electron + Vite dev shell only
python run.py --skip-deps   # Skip all setup checks (fast relaunch)
```

The full dev stack runs two processes:

| Process | URL | Description |
|---------|-----|-------------|
| FastAPI backend | `http://127.0.0.1:47821` | REST API + WebSocket + static frontend server |
| Electron (Vite HMR) | `http://127.0.0.1:5173` | Dev shell with hot-module reload |

---

## Project Structure

```
Transfera/
│
├── run.py                            # ← Start here. One-command orchestrator.
│
├── backend/                          # Python 3.12 · FastAPI · SQLAlchemy · SQLite
│   ├── main.py                       # App entrypoint, lifespan, startup hooks
│   ├── config.py                     # Ports, paths, media extensions
│   ├── requirements.txt              # Python dependencies
│   │
│   ├── api/
│   │   ├── routes.py                 # All HTTP endpoints (health, sessions, media, thumbnails)
│   │   ├── schemas.py                # Pydantic request/response models
│   │   ├── websocket.py              # WebSocket manager with 15-event protocol + keepalive
│   │   ├── device_preview.py         # Source folder browsing endpoint
│   │   └── tier2_routes.py           # WSL2 bridge routes (Tier 2 iOS backend)
│   │
│   ├── database/
│   │   ├── manager.py                # Async SQLAlchemy engine (WAL mode, FK pragmas)
│   │   ├── models.py                 # MediaItem, TransferSession, TransferBatch ORM models
│   │   └── migrations.py             # Schema migration runner (21 migrations)
│   │
│   ├── engines/
│   │   ├── scanner.py                # Recursive walker + Live Photo HEIC/MOV grouping
│   │   ├── cache_manager.py          # Hop 1: source → cache (streaming BLAKE3, .partial)
│   │   ├── importer.py               # Hop 2: cache → archive (re-verify, atomic rename)
│   │   ├── batch_manager.py          # 100-file batch chunking and status tracking
│   │   ├── duplicate_detector.py     # Exact (hash) + potential (metadata) duplicate detection
│   │   ├── metadata_extractor.py     # ExifTool stay-open session + filesystem fallback
│   │   ├── organizer.py              # Archive path resolution (YYYY/MM/DD layouts)
│   │   ├── recovery.py               # Crash recovery for interrupted LOADING/ARCHIVED batches
│   │   ├── reporter.py               # JSON + HTML transfer reports
│   │   ├── thumbnailer.py            # JPEG thumbnail generation (ExifTool/Pillow/ffmpeg)
│   │   ├── thumbnail_cache.py        # Bounded in-memory LRU thumbnail cache (50 MB cap)
│   │   └── thumbnail_ops.py          # Thumbnail DB status helpers
│   │
│   ├── utils/
│   │   └── hashing.py                # BLAKE3 / SHA-256 streaming hash implementation
│   │
│   ├── bin/                          # Auto-managed binaries (git-ignored)
│   │   ├── exiftool/                 # ExifTool binary (auto-downloaded on first run)
│   │   └── wpd_helper.exe            # Native WPD device helper (auto-built if MSVC present)
│   │
│   ├── data/                         # Runtime data (git-ignored)
│   │   ├── db/                       # transfera.db — SQLite database
│   │   ├── cache/                    # Hop 1 staging area (.partial files)
│   │   ├── exports/                  # Generated session reports (JSON + HTML)
│   │   └── logs/                     # Application logs
│   │
│   └── tests/                        # pytest suite (16 modules, ~200 tests)
│
├── frontend/                         # Electron · React 18 · Vite · TypeScript · Tailwind
│   ├── electron/
│   │   ├── main.ts                   # Electron main process (IPC, tray, native notifications)
│   │   └── preload.ts                # Secure contextBridge IPC surface
│   │
│   └── src/
│       ├── App.tsx                   # App shell: sidebar, router, toast, error boundaries
│       ├── pages/
│       │   ├── DashboardPage.tsx     # Session history, statistics, device status
│       │   ├── DeviceSetupPage.tsx   # Source/destination picker, preflight validation
│       │   ├── TransferPage.tsx      # Live transfer monitor with WebSocket + polling
│       │   └── LibraryPage.tsx       # Masonry/list/history browser with infinite scroll
│       ├── store/
│       │   └── transfer.ts           # Zustand store — 15-event WebSocket reducer
│       ├── lib/
│       │   ├── queries.ts            # TanStack Query hooks for all API endpoints
│       │   ├── api-client.ts         # Axios instance with local auth token
│       │   └── thumbnail-fetch.ts    # Thumbnail fetch with negative-cache deduplication
│       └── hooks/
│           └── use-transfer-ws.ts    # WebSocket connection lifecycle hook
│
├── native/
│   └── wpd_helper/
│       ├── wpd_helper.cpp            # WPD COM API device driver (Windows Portable Devices)
│       └── build.bat                 # MSVC build script (vswhere auto-discovery)
│
└── .github/
    └── workflows/ci.yml              # CI: backend pytest + frontend typecheck + lint
```

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Desktop shell | Electron 33 |
| Frontend | React 18, TypeScript, Vite 6, Tailwind CSS 4 |
| State management | Zustand 5 (persisted preferences), TanStack Query 5 |
| Backend | Python 3.12, FastAPI, Uvicorn |
| Database | SQLite (via SQLAlchemy 2 async + aiosqlite), WAL mode |
| Hashing | BLAKE3 (primary), SHA-256 (fallback) |
| Metadata | ExifTool (stay-open session), Pillow, pillow-heif |
| iOS/WPD | Windows Portable Devices COM API (native C++), pymobiledevice3 |
| Real-time | WebSocket (15-event protocol), REST polling fallback |

---

## Troubleshooting

| Symptom | Resolution |
|---------|------------|
| `Python 3.12 not found` | Ensure Python 3.12 is installed and `python --version` returns `3.12.x`. The `py -3.12` launcher is also probed automatically. |
| `Port 47821 already in use` | A previous run may not have shut down cleanly. The orchestrator auto-sweeps stray processes on startup; if it still fails, kill the process occupying the port manually. |
| `npm install` permission error | Run your terminal as Administrator (right-click → Run as administrator). |
| iPhone not detected | Ensure MSVC is installed and `npm run build:native` has completed (look for `wpd_helper.exe` in `backend/bin/`). Trust the computer on your iPhone when prompted. |
| Blank page after navigating | Known bug — fixed in the current branch. See [issue tracker](https://github.com/R1sh1m/Transfera/issues). |
| Thumbnails showing wrong images | Known bug — fixed in the current branch. Caused by a stale negative-cache between sessions. |
| ExifTool not found | First-run auto-bootstrap handles this. If it fails, check internet connectivity; ExifTool is downloaded from `exiftool.org` on first launch. |
| `wpd_helper build failed: LNK1104` | The `.exe` is locked by a running Transfera backend. Fully close the app (`Ctrl+C` in the terminal) and retry. |

---

## Running Tests

```powershell
cd backend
.venv\Scripts\python -m pytest tests/ -v
```

The test suite covers pipeline integrity, crash recovery, schema migrations, organiser logic, duplicate detection, and API endpoint smoke tests.

---

## License

MIT License — see [LICENSE](LICENSE) for full terms.

Copyright © 2026 Rishi Misra