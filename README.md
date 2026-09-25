# Transfera

**Back up the photos and videos on your phone, camera, or USB drive — safely, privately, on your own PC.**

Transfera copies your pictures and videos into one tidy, organized archive folder. It checks every file twice so nothing corrupt ever slips in, skips files you already backed up, and sorts everything by date. There is no cloud, no account, and no subscription. Your files never leave your computer.

[![CI](https://github.com/R1sh1m/Transfera/actions/workflows/ci.yml/badge.svg)](https://github.com/R1sh1m/Transfera/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Node.js 20+](https://img.shields.io/badge/node-%3E%3D20-green.svg)](https://nodejs.org/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Platform: Windows 11](https://img.shields.io/badge/platform-Windows%2011-lightgrey.svg)](#)

---

## Install (pick one — easiest first)

You need **Windows 10 or 11**. Nothing else to install.

### Option 1: Portable — no installation at all (recommended for testing)

1. Go to **[GitHub Releases](https://github.com/R1sh1m/Transfera/releases)** and download `Transfera-Portable-X.Y.Z.zip`.
2. Right-click it → **Extract All** → open the folder → double-click `Transfera.exe`.

That is everything. No admin rights, no setup wizard. To remove it, just delete the folder.

### Option 2: Setup installer

1. From **[GitHub Releases](https://github.com/R1sh1m/Transfera/releases)**, download `Transfera-Setup-X.Y.Z.exe` and run it.
2. It adds a Start-menu shortcut. To remove it later: Settings → Apps → Transfera → Uninstall.

### Option 3: One command (winget)

```powershell
winget install --id Transfera.Transfera -e
```

### "Windows protected your PC"?

You may see this warning the first time you run Transfera. It appears because the app is new and does not yet have a paid signing certificate — **not** because anything is wrong:

- Transfera is fully open source — every line of code is on GitHub for anyone to inspect.
- Releases are built automatically on GitHub's own servers, never on somebody's laptop.
- You can verify your download yourself. Compare its fingerprint against `SHA256SUMS.txt` from the same release page:

  ```powershell
  certutil -hashfile Transfera-Portable-X.Y.Z.zip SHA256
  ```

  If the long code matches, the file is exactly what GitHub built.

To continue past the warning, click **More info → Run anyway**. Windows remembers your choice.

---

## Your first backup (5 minutes)

1. **Open Transfera.** You land on the **Dashboard**.
2. Click the big blue **Start New Backup** button (or **Setup** in the left sidebar).
3. **Source** — where are your photos?
   - *A folder on this PC:* click **Browse**, pick the folder, and you will instantly see a preview grid of the photos and videos inside. Tick the ones you want (or keep them all).
   - *An iPhone/iPad:* plug it in with a USB cable, unlock it, and tap **Trust** on the phone. Then pick it from **Connected devices**. (First time only, Transfera may offer to install Apple's free driver for you in one click — see [iPhone notes](#iphone--ipad).)
4. **Destination** — click **Browse** and choose (or create) the folder where your archive should live, for example `D:\Photos`.
5. **Transfer Mode** — leave it on **Backup (Copy)**. Your originals stay untouched; Transfera only reads them. (Choose **Space Saver (Move)** only if you want the originals deleted after a verified copy.)
6. Press **Start**. The **Transfer** page shows live progress, speed, and thumbnails as each file lands.
7. When it finishes, open the **Library** page: your archive, searchable, with Timeline, Moments, Duplicates, and Trash sections.

Your archive is organized automatically into folders by date, like `D:\Photos\2026\09-September\photo.jpg`.

### Everyday answers

- **Where are my files?** Exactly where you pointed Destination — plain JPG/MP4 files in date folders. You can open them with any app, even if you delete Transfera.
- **Is anything uploaded?** No. Transfera has no servers. The only internet it ever uses is downloading helper tools (photo-metadata reader, AI search model) once, with your permission.
- **What if I unplug mid-transfer?** Plug back in and press Start again — finished files are skipped, interrupted ones resume. Nothing half-written ever lands in your archive.
- **I pressed the wrong thing — are my originals safe?** Yes, in Copy mode Transfera never writes to, moves, or deletes your source files. Move mode deletes originals only after each file is verified twice.
- **I deleted something in the Library?** It goes to **Trash** first. Emptying Trash permanently deletes those archive copies (your originals elsewhere are never touched).
- **How do I search?** Type in the Library search box. Press **Get AI models** once (~210 MB, one time) and search understands content too — try "sunset" or "dog".
- **How do I update?** Download the new release and run it over the old one. Your library, sessions, and settings are kept.

---

## iPhone & iPad

- **Easiest path:** USB cable + unlock + Trust. Transfera reads your Camera Roll directly — no iTunes needed.
- If Windows is missing Apple's free driver, Transfera shows an **Install Driver** card on the Dashboard. One click installs it via winget (Windows may ask for admin permission once).
- No driver and no admin rights? Transfera automatically falls back to its built-in open-source bridge (WSL2 + usbipd) where available, and plain folder backup always works regardless.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| App window is blank / won't open | Close it fully, wait 10 seconds, open again. Still stuck? Delete `%APPDATA%\Transfera` session files and relaunch. |
| iPhone not listed | Use a data cable (some cables charge only), unlock the phone, tap **Trust**, unplug and replug. Then check the Dashboard driver card. |
| A transfer paused with "duplicates found" | Transfera thinks some files are already archived. Open the popup, choose **Skip** (don't copy again), **Keep both**, or **Overwrite** per file, then Resume. |
| "Session ... is not paused" after resolving | Just press Start/Resume once more — resolving already restarted the transfer in the background. |
| Search finds nothing | Filenames only match by default. Press **Get AI models** in the Library header, wait for the download, press **Index library**, then search again. |
| Antivirus flags a file | Add the Transfera folder to your antivirus exclusions — freshly built helper programs sometimes trip heuristics. |
| Something looks broken | The log file `backend\data\logs\transfera.log` (next to the app) records what happened — attach it when asking for help. |

---

## For developers (building from source)

You need just two tools installed first (everything else — Python packages, npm packages, ExifTool, the device helper — sets itself up on first launch):

```powershell
winget install -e --id Python.Python.3.12 ; winget install -e --id OpenJS.NodeJS.LTS
```

Then:

```powershell
git clone https://github.com/R1sh1m/Transfera.git
cd Transfera
python run.py              # full app (backend + Electron window)
```

| Command | What it does |
|---|---|
| `python run.py` | Start everything (recommended) |
| `python run.py --backend` | API only, on `http://127.0.0.1:47821` |
| `python run.py --frontend` | Electron + Vite dev shell only |
| `python run.py --skip-deps` | Skip setup checks (fast relaunch) |

First launch takes 2–4 minutes (creates `.venv`, installs packages, builds the frontend, downloads ExifTool). Later launches skip finished steps. Press **Ctrl+C** to stop everything cleanly.

Building the iPhone helper from source additionally needs MSVC (Visual Studio 2022 Build Tools with the C++ workload) — without it, folder backup still works fully; only iPhone/WPD detection stays unavailable.

### Checks before you commit or release

```powershell
.venv\Scripts\python -m pytest backend/tests/ -q   # backend suite
.venv\Scripts\python -m ruff check backend/        # backend lint
cd frontend && npm run typecheck                    # frontend types
```

Keep `frontend/package.json`, `pyproject.toml`, and `winget/Transfera.Transfera.yaml` on the same version — the release workflow enforces `v<that-version>` tags against all three.

### How it works (60 seconds)

Every file travels in two verified hops: **source → staging cache** (streamed copy + BLAKE3 hash, kept as `.partial` until the hash matches), then **cache → archive** (hash re-verified, atomic move into `YYYY/MM/DD`, source deleted only in Move mode after verification). Thumbnails, EXIF dates, duplicate detection, and crash recovery all hang off that pipeline. On-board AI (MobileCLIP, ONNX, CPU) is optional and downloads once on request.

```
Transfera/
├── run.py                 # one-command orchestrator — start here
├── backend/               # Python 3.12 + FastAPI + SQLite (WAL)
│   ├── api/               # REST routes, WebSocket, auth, device preview
│   ├── database/          # models, async engine, migrations
│   ├── engines/           # scanner, cache, importer, duplicates, EXIF,
│   │                      # thumbnails, CLIP, organizer, recovery, reports
│   ├── requirements.txt   # backend deps (incl. onnxruntime for AI search)
│   ├── data/              # runtime DB, cache, exports, logs, models (ignored)
│   └── tests/             # pytest suite (isolated temp DBs — never touches yours)
├── frontend/              # Electron 33 + React 18 + Vite + TypeScript + Tailwind
│   ├── electron/          # main process, preload IPC
│   └── src/pages/         # Dashboard, DeviceSetup, Transfer, Library
├── native/wpd_helper/     # C++ WPD helper source + build.bat
└── .github/workflows/     # ci.yml (tests/typecheck/lint) + release.yml (build+sign)
```

---

## License

**AGPL-3.0-or-later** — see [LICENSE](LICENSE). Free for personal, academic, and commercial *use*; if you modify or re-host Transfera (including as a network service), keep Rishi Misra's copyright notice, state your changes, and share your modified source under the same terms. The "Transfera" name and artwork are reserved.

Copyright © 2026 Rishi Misra
