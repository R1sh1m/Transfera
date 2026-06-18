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

> **Two-Stage Verified Media Vaulting Engine** &mdash; FastAPI + Electron + React

---

## Project Overview

Transfera is a local desktop media backup application built around a **high-throughput Two-Stage Verified pipeline**. Rather than blindly copying files, every byte passes through two distinct hops with cryptographic verification at each stage.

```
Source Files ──▶ [Hop 1: Cache] ──▶ [Hop 2: Archive] ──▶ Verified Backup
```

| Hop | Source | Destination | Mechanism |
|-----|--------|-------------|-----------|
| 1 | Original media files | Local cache (`.partial` &rarr; rename) | Stream-copy with on-the-fly BLAKE3 hash |
| 2 | Verified cache copy | Final archive directory | Hash re-verification before atomic placement |

**Core design pillars:**

- **BLAKE3 hashing** (SHA-256 fallback) computed _during_ the copy, not after&mdash;eliminating double-reads.
- **Atomic writes** via a `.partial` suffix that is renamed only on a verified hash match.
- **SQLite WAL mode** for concurrent reads during active writes.
- **Crash recovery** that handles interrupted `LOADING` and `ARCHIVED` batch states on restart.
- **Live Photo detection** that groups HEIC+MOV pairs by matching filenames (case-insensitive).
- **Duplicate detection** with exact (hash-based) and potential (metadata-similarity) resolution.

---

## Core Prerequisites

Transfera targets **Windows 11** as its primary platform. Ensure the following are installed before proceeding.

### Python 3.12

Transfera enforces Python **3.12.x** for stable pre-compiled wheel availability (`pillow-heif`, `blake3`).

1. Download from <https://www.python.org/downloads/>
2. During installation, **check "Add python.exe to PATH"**.
3. Verify:

```bash
python --version
# Expected: Python 3.12.x
```

### Node.js v20+

The Electron shell and Vite build pipeline require Node.js **v20 or later**.

1. Download the LTS release from <https://nodejs.org/>
2. Verify:

```bash
node --version
# Expected: v20.x.x or higher
npm --version
```

### Git

```bash
git --version
```

### ExifTool (isolated environment)

Transfera bundles an **automated ExifTool manager** that bootstraps the binary on first launch into `backend/bin/exiftool/`. No manual installation is required.

If you prefer a system-wide installation instead:

1. Download the Windows build from <https://exiftool.org/>
2. Extract `exiftool.exe` and rename it to `exiftool.exe` (if zipped).
3. Add its directory to your system `PATH` environment variable.
4. Verify:

```bash
exiftool -ver
# Expected: 12.x or higher
```

> **Note:** The bundled bootstrapper (`backend/bin/exiftool/`) takes precedence over any system-wide installation.

---

## Developer Installation &amp; Local Boot Loop

Transfera ships with a single-command orchestrator that handles the entire dev environment lifecycle.

### 1. Clone the Repository

```bash
git clone https://github.com/your-username/Transfera.git
cd Transfera
```

### 2. Launch Everything

```bash
python run.py
```

That is the only command you need to remember. On first run the orchestrator automatically:

1. **Locates Python 3.12** on your system (probes common install paths, then the `py` launcher).
2. **Creates a virtual environment** at `.venv/` using the discovered interpreter.
3. **Installs backend dependencies** from `backend/requirements.txt` via pip.
4. **Installs frontend npm packages** (`npm install` in `frontend/`).
5. **Compiles the React frontend** into `frontend/dist/` (`npm run build`).
6. **Spins up the FastAPI backend** on `http://127.0.0.1:47821` (uvicorn).
7. **Launches the Electron dev shell** (Vite HMR on `:5173` + Electron wrapper).

Press **Ctrl+C** at any time to gracefully tear down all processes.

### Runner Flags

```bash
python run.py                  # Full stack (backend + frontend)
python run.py --backend        # Backend only
python run.py --frontend       # Frontend dev server only
python run.py --skip-deps      # Skip dependency checks / venv creation
```

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Port 47821 already in use` | Kill the process occupying the port, or reboot. |
| `npm install` permission errors | Run your terminal as Administrator. |
| Python version mismatch | Ensure `python --version` reports `3.12.x`. The runner enforces this strictly. |

---

## Production Compilation

To bundle the full multi-process system (Electron + FastAPI backend + Python runtime) into a standalone Windows installer:

```bash
cd frontend
npm install
npm run electron:build
```

This executes the following pipeline under the hood:

1. `tsc -b` &mdash; compiles TypeScript (`electron/main.ts`, `electron/preload.ts`).
2. `vite build` &mdash; bundles the React SPA into `frontend/dist/`.
3. `electron-builder --win` &mdash; packages everything into an NSIS installer with the bundled Python backend.

The output artifact is written to:

```
frontend/release/Transfera-Setup-2.0.0.exe
```

The `win-unpacked/` directory alongside it contains the portable (non-installer) build.

### Build Prerequisites

- Python 3.12 must be installed and on PATH (the builder bundles the venv into `resources/backend/venv/`).
- Node.js v20+ with `npm`.
- The backend's `.venv` must already exist (run `python run.py` once, or `cd backend && python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`).

---

## Repository Map Layout

```
Transfera/
│
├── backend/                          # Python FastAPI backend
│   ├── main.py                       # FastAPI app entrypoint + lifespan
│   ├── config.py                     # Central config (ports, paths, extensions)
│   ├── requirements.txt              # Python dependencies
│   │
│   ├── api/
│   │   ├── routes.py                 # HTTP endpoints (health, scan, sessions, media)
│   │   ├── schemas.py                # Pydantic request/response models
│   │   └── websocket.py              # WebSocket manager with 30s keepalive
│   │
│   ├── database/
│   │   ├── manager.py                # Async SQLAlchemy engine (WAL + FK pragmas)
│   │   └── models.py                 # MediaItem, TransferSession, TransferBatch
│   │
│   ├── engines/
│   │   ├── scanner.py                # Recursive walker + Live Photo grouping
│   │   ├── cache_manager.py          # Hop 1: source -> cache (streaming hash)
│   │   ├── importer.py               # Hop 2: cache -> archive (.partial writes)
│   │   ├── duplicate_detector.py     # Exact + potential duplicate detection
│   │   ├── metadata_extractor.py     # ExifTool integration + filesystem fallback
│   │   ├── organizer.py              # YYYY/MM/DD path resolution
│   │   ├── batch_manager.py          # 100-file batch chunking & status tracking
│   │   ├── recovery.py               # Crash recovery for interrupted batches
│   │   └── reporter.py               # Transfer reports and summaries
│   │
│   ├── utils/
│   │   └── hashing.py                # BLAKE3 / SHA-256 streaming hash
│   │
│   ├── bin/
│   │   └── exiftool/                 # Auto-bootstrapped ExifTool binary
│   │
│   ├── data/                         # Runtime data (git-ignored)
│   │   ├── db/                       # SQLite database files
│   │   ├── cache/                    # Hop 1 cache staging area
│   │   ├── exports/                  # Generated reports
│   │   └── logs/                     # Application logs
│   │
│   └── tests/                        # pytest test suite
│       ├── test_db_core.py
│       ├── test_scanner.py
│       ├── test_pipeline.py
│       ├── test_organizer.py
│       ├── test_crash_recovery.py
│       ├── test_integration.py
│       └── test_smoke.py
│
├── frontend/                         # Electron + React + Vite
│   ├── package.json                  # npm scripts & electron-builder config
│   │
│   ├── electron/
│   │   ├── main.ts                   # Electron main process (NativeImage, IPC)
│   │   ├── preload.ts                # Secure contextBridge IPC
│   │   └── tsconfig.json
│   │
│   ├── src/                          # React SPA
│   │   ├── main.tsx                  # React root mount
│   │   ├── App.tsx                   # Router + layout shell
│   │   ├── index.css                 # Tailwind base styles
│   │   │
│   │   ├── pages/
│   │   │   ├── DashboardPage.tsx     # Session overview & statistics
│   │   │   ├── DeviceSetupPage.tsx   # Source / destination picker
│   │   │   ├── TransferPage.tsx      # Active transfer progress
│   │   │   └── LibraryPage.tsx       # Archived media browser
│   │   │
│   │   ├── components/
│   │   │   ├── DuplicateModal.tsx    # Duplicate resolution dialog
│   │   │   ├── ErrorBoundary.tsx     # React error boundary
│   │   │   ├── ModeSelector.tsx      # Transfer mode toggle
│   │   │   └── ThemeToggle.tsx       # Light / dark switch
│   │   │
│   │   ├── hooks/
│   │   │   ├── use-transfer-ws.ts    # WebSocket connection hook
│   │   │   └── use-theme.ts          # Theme persistence hook
│   │   │
│   │   ├── store/
│   │   │   └── transfer.ts           # Zustand store (15-event WS reducer)
│   │   │
│   │   ├── lib/
│   │   │   ├── api-client.ts         # Axios instance configuration
│   │   │   ├── queries.ts            # React Query hooks
│   │   │   └── utils.ts              # Shared helpers
│   │   │
│   │   ├── types/
│   │   │   ├── api.ts                # TypeScript types mirroring backend schemas
│   │   │   └── electron.d.ts         # Electron IPC type declarations
│   │   │
│   │   └── assets/
│   │       └── icon.png              # App icon source asset
│   │
│   ├── build/
│   │   └── icon.png                  # Electron window / taskbar icon
│   │
│   ├── public/                       # Static assets (favicons, manifest)
│   └── dist/                         # Compiled Vite output (git-ignored)
│
├── run.py                            # One-command dev stack orchestrator
├── DESIGN.md                         # Design system tokens & guidelines
├── LICENSE                           # MIT License
└── README.md                         # This file
```

---

## License

MIT License &mdash; see [LICENSE](LICENSE) for details.

Copyright (c) 2026 Rishi Misra
