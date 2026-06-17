# MediaVault v2

A local desktop media backup application with verified file transfers, crash recovery, and duplicate detection.

## Architecture

MediaVault uses a two-hop pipeline to ensure data integrity:

```
Source Files -> [Hop 1: Cache] -> [Hop 2: Archive] -> Verified Backup
```

| Hop | Source | Destination | Purpose |
|-----|--------|-------------|---------|
| 1 | Original files | Local cache (`.partial` -> rename) | Stream-copy + BLAKE3 hash verification |
| 2 | Verified cache | Archive directory | Final placement with hash re-verification |

**Key Design Decisions:**
- **SQLite WAL mode** for concurrent reads during writes
- **BLAKE3 hashing** (SHA-256 fallback) computed during copy, not after
- **Atomic writes** via `.partial` suffix + rename on verified hash match
- **Crash recovery** handles interrupted LOADING and ARCHIVED batch states
- **Live Photo detection** groups HEIC+MOV pairs by matching stems (case-insensitive)

---

## Quick Download & Local Deployment Guide

Get MediaVault running on your machine in under two minutes.

### 1. Clone the Repository

```bash
git clone https://github.com/your-username/MediaVault.git
cd MediaVault
```

### 2. Install System Requirements

MediaVault requires two runtimes. Check that both are installed:

```bash
python --version   # Must be 3.12 or higher
node --version     # Must be 18 or higher
```

If you don't have them yet:
- **Python** — https://www.python.org/downloads/ (check "Add to PATH" during install)
- **Node.js** — https://nodejs.org/ (LTS version recommended)

### 3. Start Everything with One Command

```bash
python run.py
```

That's it. On first run, the script will automatically:
- Create a Python virtual environment and install all backend dependencies
- Install frontend npm packages
- Launch the FastAPI backend server (port 47821)
- Launch the Vite development server (port 5173)

Open your browser to **http://127.0.0.1:5173** to use MediaVault.

Press **Ctrl+C** in the terminal to shut down all services cleanly.

### Troubleshooting

- **ExifTool** — MediaVault includes an automated ExifTool manager. If the binary isn't found on your system, the app will download it automatically on first launch. No manual installation needed.
- **Port already in use** — If port 47821 or 5173 is occupied, stop the other process first or reboot.
- **Permission errors on Windows** — Run your terminal as Administrator if `npm install` fails.

---

## Prerequisites

- **Python 3.12+** with `pip`
- **Node.js 18+** with `npm`
- **ExifTool** (optional, for metadata extraction) - install and add to PATH

## Local Development

### Quick Start (single command)

```bash
python run.py
```

This automatically:
1. Creates the backend virtual environment and installs Python dependencies (first run only)
2. Installs frontend `node_modules` (first run only)
3. Starts the FastAPI backend on `http://127.0.0.1:47821`
4. Starts the Vite dev server on `http://127.0.0.1:5173`

Press **Ctrl+C** to gracefully shut down both services.

**Flags:**
- `--backend` — start backend only
- `--frontend` — start frontend only
- `--skip-deps` — skip dependency checks

### Manual Setup

#### Backend

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 47821
```

#### Frontend

```bash
cd frontend
npm install
npm run dev
```

The frontend dev server runs on `http://127.0.0.1:5173` and proxies API requests to the backend.

## Running Tests

```bash
# Backend unit tests (246 tests total)
python -m backend.test_db_core           # 36 tests - DB, hashing, config
python -m backend.test_scanner           # 28 tests - Scanner, Live Photos
python -m backend.test_pipeline          # 25 tests - Pipeline interruption
python -m backend.test_organizer         # 33 tests - Organizer, duplicates
python -m backend.test_crash_recovery    # 53 tests - HEIC crash, Live Photo bundles
python -m backend.test_integration       # 42 tests - HTTP endpoints
python -m backend.test_smoke             #  9 tests - End-to-end smoke test
python -m backend.test_exiftool_bootstrapper  # 20 tests - ExifTool bootstrapper
```

## Production Build

### Backend (standalone)

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 47821
```

### Frontend (Electron installer)

```bash
cd frontend
npm install
npm run build
npx electron-builder --win
```

Output: `frontend/release/MediaVault-Setup-2.0.0.exe`

## Project Structure

```
MediaVault/
  run.py               # Single-command development stack orchestrator
  backend/
    api/
      routes.py          # HTTP endpoints (health, scan, sessions, media, duplicates)
      schemas.py         # Pydantic request/response models
      websocket.py       # WebSocket connection manager with 30s keepalive
    database/
      manager.py         # Async SQLAlchemy engine with WAL + FK pragmas
      models.py          # MediaItem, TransferSession, TransferBatch tables
    engines/
      batch_manager.py   # 100-file batch chunking and status tracking
      cache_manager.py   # Hop 1: source -> cache with streaming hash
      duplicate_detector.py  # Exact + potential duplicate detection
      importer.py        # Hop 2: cache -> destination with .partial writes
      metadata_extractor.py  # ExifTool integration with filesystem fallback
      organizer.py       # YYYY/MM/DD path resolution, conflict handling
      recovery.py        # Crash recovery for LOADING/ARCHIVED states
      scanner.py         # Recursive walker with Live Photo grouping
    utils/
      hashing.py         # BLAKE3/SHA-256 streaming hash with async variant
    config.py            # Single source of truth for all constants
    main.py              # FastAPI app with lifespan management
  frontend/
    electron/
      main.ts            # Electron main process, FastAPI subprocess lifecycle
      preload.ts         # Secure IPC bridge (contextIsolation)
    src/
      store/transfer.ts  # Zustand store with 15-event WS reducer
      hooks/             # WebSocket connection, React Query hooks
      pages/             # Dashboard, DeviceSetup, Transfer, Library
      components/        # DuplicateModal
      lib/               # API client, query hooks, utils
      types/             # TypeScript types mirroring backend schemas
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check with DB status |
| GET | `/api/config` | Server configuration (extensions, batch size) |
| POST | `/api/scan` | Start source directory scan |
| GET | `/api/sessions` | List all sessions with pagination |
| POST | `/api/sessions` | Create new transfer session |
| POST | `/api/sessions/{id}/start` | Start/resume transfer |
| POST | `/api/sessions/{id}/pause` | Pause active transfer |
| POST | `/api/sessions/{id}/cancel` | Cancel transfer |
| POST | `/api/duplicates/check` | Check batch for duplicates |
| GET | `/api/media` | Query media library with filters |
| POST | `/api/recovery` | Trigger crash recovery |

## WebSocket Events

15 real-time events broadcast to connected clients:

- `scan_progress`, `scan_complete`
- `batch_created`, `batch_processing`, `batch_complete`
- `hop1_progress`, `hop1_complete`
- `hop2_progress`, `hop2_complete`
- `duplicates_detected`, `duplicates_resolved`
- `session_started`, `session_paused`, `session_complete`
- `error`

## License

MIT License - see [LICENSE](LICENSE) for details.
