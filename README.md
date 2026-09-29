<div align="center">

# 📷 Transfera

**Your photos and videos. Your computer. Your rules.**

Back up every photo and video from your phone, camera, or USB drive — verified twice, sorted by date, stored locally. No cloud. No account. No subscription.

[![CI](https://github.com/R1sh1m/Transfera/actions/workflows/ci.yml/badge.svg)](https://github.com/R1sh1m/Transfera/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Node.js 20+](https://img.shields.io/badge/node-%3E%3D20-green.svg)](https://nodejs.org/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#-install)

</div>

---

## ✨ What it does

- **Two-hop verification** — every file is hashed while copying, then re-hashed before landing in your archive. Corruption is impossible.
- **Smart deduplication** — already have it? Transfera skips it.
- **Auto-organizes** by date → `Photos/2026/09-September/IMG_1234.jpg`
- **iPhone & iPad** — plug in, trust, done. No iTunes. Apple drivers included on day one.
- **AI search** — find "sunset beach" or "birthday cake" across your whole library (downloads once, runs on CPU, completely local).
- **Zero cloud** — the only internet access ever is downloading helper tools, once, with your permission.

---

## 🚀 Install

> **Clone once, run one script.** It figures out your OS and does everything — installs dependencies, downloads Apple drivers, builds the app, ships every feature at day one.

### Step 1 — Clone

```bash
git clone https://github.com/R1sh1m/Transfera.git
cd Transfera
```

### Step 2 — Run the installer for your platform

<table>
<tr>
<th>🍎 macOS &nbsp;/&nbsp; 🐧 Linux</th>
<th>🪟 Windows</th>
</tr>
<tr>
<td>

```bash
bash scripts/install.sh
```

Detects macOS or Linux, installs system deps via Homebrew / apt / dnf / pacman, sets up the Python venv, builds the frontend, installs **libimobiledevice** for iPhone support, and produces a DMG (macOS) or .deb / .AppImage (Linux).

</td>
<td>

```powershell
powershell -ExecutionPolicy Bypass `
  -File scripts\Install-Transfera.ps1
```

Installs Python, Node, Rust, and Git via winget, pulls **Apple Mobile Device Support**, builds the C++ iPhone helper, freezes the backend sidecar, and produces a locally-compiled installer — **no SmartScreen warning**.

</td>
</tr>
</table>

That's it. First run takes 5–15 minutes (downloads + build). Every subsequent launch starts in seconds.

#### Optional flags (both scripts)

| Flag | Effect |
|---|---|
| `--skip-driver` | Skip Apple device support install |
| `--skip-native` | Skip C++ helper build (folder backup still works) |
| `--yes` | Non-interactive — assume yes to all prompts |

**Windows only:** add `-Silent` to run the produced installer with zero click-through.

---

### 🛡️ Why no SmartScreen warning?

The installer compiles Transfera **on your own machine**. Windows SmartScreen only flags files downloaded from the internet — locally built binaries carry no Mark-of-the-Web, so the warning never appears.

**Still want the pre-built `.exe`?**

> Download `Transfera-X.Y.Z-x64-setup.exe` from **[GitHub Releases](https://github.com/R1sh1m/Transfera/releases)**, verify the hash against `SHA256SUMS.txt` on the same page, then click **More info → Run anyway** to dismiss the one-time SmartScreen prompt. Every line of code is on GitHub, open for inspection.
>
> ```powershell
> certutil -hashfile Transfera-X.Y.Z-x64-setup.exe SHA256
> ```

---

## 📸 Your first backup (5 minutes)

1. **Open Transfera** — you land on the **Dashboard**.
2. Hit **Start New Backup** (or **Setup** in the sidebar).
3. **Source** — where are your photos?
   - *This PC / external drive:* click **Browse**, pick the folder, preview the grid, tick what you want.
   - *iPhone / iPad:* plug in via USB, unlock, tap **Trust** on the phone. It appears under **Connected devices** instantly.
4. **Destination** — click **Browse**, pick or create your archive folder (e.g., `D:\Photos` or `~/Photos`).
5. **Mode** — leave it on **Backup (Copy)**. Your originals are never touched. Switch to **Space Saver (Move)** only if you want originals deleted after a verified copy.
6. Press **Start**. Watch live progress, speeds, and thumbnails on the **Transfer** page.
7. Open **Library** — searchable by date, content (AI), duplicates, trash, and more.

### FAQ

| Question | Answer |
|---|---|
| Where are my files? | Exactly where you set Destination — plain JPG/MP4 in date folders. Any app can open them. |
| Is anything uploaded? | No. Zero servers. |
| Unplugged mid-transfer? | Plug back in → press Start → finished files are skipped, interrupted ones resume cleanly. |
| Are my originals safe? | In Copy mode Transfera never writes to, moves, or deletes source files. |
| Deleted something? | Goes to **Trash** first. Emptying Trash only removes archive copies. |
| How do I search by content? | Press **Get AI models** in Library once (~210 MB, CPU-only, fully local). Then search "sunset" or "dog". |
| How do I update? | Download the new release and run it over the old one. Library and settings are kept. |

---

## 📱 iPhone & iPad

- **Easiest path:** USB cable → unlock → tap **Trust**. No iTunes required.
- The installer ships Apple's driver on day one (AMDS on Windows, `libimobiledevice` on macOS/Linux) — nothing extra to do.
- Missed it? Transfera shows an **Install Driver** card on the Dashboard — one click, done.
- No admin rights? Falls back to its open-source usbipd bridge automatically. Folder backup always works regardless.

---

## 🔧 Troubleshooting

| What you see | What to do |
|---|---|
| App window is blank | Close fully, wait 10 s, reopen. Still broken? Delete app data (`%APPDATA%\Transfera` / `~/.local/share/transfera` / `~/Library/Application Support/transfera`) and relaunch. |
| iPhone not listed | Use a data cable (not charge-only), unlock, tap **Trust**, unplug and replug. Check the Dashboard driver card. |
| "duplicates found" paused | Open the popup → **Skip**, **Keep both**, or **Overwrite** → Resume. |
| Search finds nothing | Default is filename-only. Press **Get AI models** in Library, wait, press **Index library**, search again. |
| Antivirus flags a file | Add the Transfera folder to your AV exclusions — freshly compiled helpers sometimes trip heuristics. |
| Something looks broken | Attach `backend/data/logs/transfera.log` when asking for help. |

---

## 🛠️ Developer mode

Need Python 3.12, Node.js 20+, and Git (the installer handles all of this).

```bash
git clone https://github.com/R1sh1m/Transfera.git
cd Transfera
python run.py           # full stack — backend + compiled frontend
```

| Command | What it does |
|---|---|
| `python run.py` | Start everything (recommended) |
| `python run.py --backend` | API only on `http://127.0.0.1:47821` |
| `python run.py --frontend` | Tauri dev shell only (adopts a running backend) |
| `python run.py --tauri` | Backend + Tauri dev shell (WebView2) |
| `python run.py --skip-deps` | Fast relaunch — skip setup checks |

First launch takes 2–4 minutes (creates `.venv`, installs packages, builds frontend, downloads ExifTool). Later launches skip what's already done. `Ctrl+C` stops everything cleanly.

**Before committing:**

```bash
# macOS / Linux
.venv/bin/python -m pytest backend/tests/ -q
.venv/bin/python -m ruff check backend/
cd frontend && npm run typecheck

# Windows
.venv\Scripts\python -m pytest backend/tests/ -q
.venv\Scripts\python -m ruff check backend/
cd frontend; npm run typecheck
```

Keep `frontend/package.json`, `pyproject.toml`, `frontend/src-tauri/tauri.conf.json`, `frontend/src-tauri/Cargo.toml`, and `winget/Transfera.Transfera.yaml` on the same version — the release workflow enforces `v<that-version>` tags against all five.

---

### How it works (60 seconds)

Every file travels two verified hops:

```
Source ──[stream + BLAKE3 hash]──► .partial in cache
                                        │  hash match?
                                        ▼
Archive ◄──[re-verify + atomic move]── YYYY/MM/DD/filename.jpg
```

Thumbnails, EXIF dates, duplicate detection, and crash recovery all hang off that pipeline. On-board AI (MobileCLIP, ONNX, CPU-only) ships in the base install — only the model *weights* (~207 MB) download once, when you first press **Get AI models**.

```
Transfera/
├── run.py                 ← start here
├── backend/               ← Python 3.12 · FastAPI · SQLite WAL
│   ├── api/               ← REST routes, WebSocket, auth
│   ├── engines/           ← scanner, importer, thumbnailer, CLIP, organizer…
│   └── tests/             ← pytest suite (isolated, never touches your library)
├── frontend/              ← Tauri 2 · React 18 · Vite · TypeScript · Tailwind
├── native/wpd_helper/     ← C++ WPD helper (Windows iPhone/WPD detection)
└── scripts/
    ├── install.sh             ← macOS & Linux installer
    └── Install-Transfera.ps1  ← Windows installer
```

---

## 📄 License

**AGPL-3.0-or-later** — see [LICENSE](LICENSE).  
Free for personal, academic, and commercial *use*. If you modify or re-host Transfera (including as a network service), keep the copyright notice, state your changes, and share modified source under the same terms. The "Transfera" name and artwork are reserved.

Copyright © 2026 Rishi Misra
