<div align="center">

```
__/\\\\\\\\\\\\\\\____/\\\\\\\\\_________/\\\\\\\\\_____/\\\\\_____/\\\_____/\\\\\\\\\\\____/\\\\\\\\\\\\\\\__/\\\\\\\\\\\\\\\____/\\\\\\\\\_________/\\\\\\\\\____
 _\///////\\\/////___/\\\///////\\\_____/\\\\\\\\\\\\\__\/\\\\\\___\/\\\___/\\\/////////\\\_\/\\\///////////__\/\\\///////////___/\\\///////\\\_____/\\\\\\\\\\\\\__
  _______\/\\\_______\/\\\_____\/\\\____/\\\/////////\\\_\/\\\/\\\__\/\\\__\//\\\______\///__\/\\\_____________\/\\\_____________\/\\\_____\/\\\____/\\\/////////\\\_
   _______\/\\\_______\/\\\\\\\\\\\/____\/\\\_______\/\\\_\/\\\//\\\_\/\\\___\////\\\_________\/\\\\\\\\\\\_____\/\\\\\\\\\\\_____\/\\\\\\\\\\\/____\/\\\_______\/\\\_
    _______\/\\\_______\/\\\//////\\\____\/\\\\\\\\\\\\\\\_\/\\\\//\\\\/\\\______\////\\\______\/\\\///////______\/\\\///////______\/\\\//////\\\____\/\\\\\\\\\\\\\\\_
     _______\/\\\_______\/\\\____\//\\\___\/\\\/////////\\\_\/\\\_\//\\\/\\\_________\////\\\___\/\\\_____________\/\\\_____________\/\\\____\//\\\___\/\\\/////////\\\_
      _______\/\\\_______\/\\\_____\//\\\__\/\\\_______\/\\\_\/\\\__\//\\\\\\__/\\\______\//\\\__\/\\\_____________\/\\\_____________\/\\\_____\//\\\__\/\\\_______\/\\\_
       _______\/\\\_______\/\\\______\//\\\_\/\\\_______\/\\\_\/\\\___\//\\\\\_\///\\\\\\\\\\\/___\/\\\_____________\/\\\\\\\\\\\\\\\_\/\\\______\//\\\_\/\\\_______\/\\\_
        _______\///________\///________\///__\///________\///__\///_____\/////____\///////////_____\///______________\///////////////__\///________\///__\///________\///__
```

**Backup and Move files and photos easily.**

Tranfera is a simple utility to transfer files and photos from your phone, camera, or USB drive to your computer.

[![CI](https://github.com/R1sh1m/Transfera/actions/workflows/ci.yml/badge.svg)](https://github.com/R1sh1m/Transfera/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Node.js 20+](https://img.shields.io/badge/node-%3E%3D20-green.svg)](https://nodejs.org/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#-install)

</div>

---

## Features

- **Two-hop verification** — every file is hashed while copying, then re-hashed before landing in your archive. Corruption is impossible.
- **Smart deduplication** — already have it? Transfera skips it.
- **Auto-organizes** by date → `Photos/2026/09-September/IMG_1234.jpg`
- **iPhone & iPad** — plug in, trust, done. No need for iTunes.
- **AI search** — find "sunset beach" or "birthday cake" across your whole library (completely local).
- **Zero cloud** — internet access needed only for downloading helper tools, once.

---

## 🚀 Install

> **Clone once and run the script** It installs dependencies, drivers, builds the app.

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

Installs Python, Node, Rust, and Git via winget, pulls **Apple Mobile Device Support**, builds the C++ iPhone helper, freezes the backend sidecar, and produces a locally-compiled installer.

</td>
</tr>
</table>

First run takes 5–15 minutes (downloads + build). Every subsequent launch starts in seconds.

#### Optional flags (both scripts)

| Flag | Effect |
|---|---|
| `--skip-driver` | Skip Apple device support install |
| `--skip-native` | Skip C++ helper build (folder backup still works) |
| `--yes` | Non-interactive — assume yes to all prompts |

**Windows only:** add `-Silent` to run the produced installer with zero click-through.

---

## 📸 Your first backup 

1. **Open Transfera** — you land on the **Dashboard**.
2. Hit **Start New Backup** (or **Setup** in the sidebar).
3. **Source** — where are your photos?
   - *This PC / external drive:* click **Browse**, pick the folder, preview the grid, tick what you want.
   - *iPhone / iPad:* plug in via USB, unlock, tap **Trust** on the phone. It appears under **Connected devices** instantly.
4. **Destination** — click **Browse**, pick or create your archive folder (e.g., `D:\Photos` or `~/Photos`).
5. **Mode** — leave it on **Backup (Copy)**. Your originals are never touched. Switch to **Space Saver (Move)** only if you want originals deleted after a verified copy.
6. Press **Start**. Watch live progress, speeds, and thumbnails on the **Transfer** page.
7. Open **Library** — searchable by date, content (AI), duplicates, trash, and more.


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

### How it works (60 seconds)

Every file travels two verified hops:

```
Source ──[stream + BLAKE3 hash]──► .partial in cache
                                        │  hash match?
                                        ▼
Archive ◄──[re-verify + atomic move]── YYYY/MM/DD/filename.jpg
```

Thumbnails, EXIF dates, duplicate detection, and crash recovery all hang off that pipeline. On-board AI (MobileCLIP, ONNX, CPU-only) ships in the base install — only the model *weights* (~207 MB) download once, when you first press **Get AI models**.

---

## 📄 License

**AGPL-3.0-or-later** — see [LICENSE](LICENSE).  
Free for personal, academic, and commercial *use*. If you modify or re-host Transfera (including as a network service), keep the copyright notice, state your changes, and share modified source under the same terms. The "Transfera" name and artwork are reserved.

Copyright © 2026 Rishi Misra
