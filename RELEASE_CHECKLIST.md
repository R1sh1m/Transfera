# Transfera Release Checklist

Run through this matrix before publishing a GitHub release tag (`v*`).
The `release.yml` workflow gates on `frontend/package.json` ↔ `pyproject.toml` ↔
`winget/Transfera.Transfera.yaml` (`PackageVersion`) all matching the tag.

## Pre-flight

- [ ] `frontend/package.json`, `pyproject.toml`, `winget/Transfera.Transfera.yaml` versions all equal the tag (without the leading `v`).
- [ ] `python run.py` boots clean on a dev machine (backend `:47821` healthy, Electron window opens).
- [ ] `SHA256SUMS.txt` generated and verified in CI for every shipped `.exe`.

## Test matrix

| # | Scenario | Steps | Expected result |
|---|----------|-------|-----------------|
| 1 | Fresh install | Remove `%APPDATA%/Transfera`, data dir, and `.venv`; run installer/portable, launch | First-launch bootstrap completes (pip + npm + ExifTool download, frontend build); app reaches Dashboard with no errors |
| 2 | Upgrade | Install previous release, add media + sessions, install new version over it | Library, sessions, and settings preserved; migration runner applies cleanly; no duplicate re-imports |
| 3 | Offline first launch | Clean machine with no network, launch packaged app | App starts but shows clear errors for the online-only steps (dependency install, ExifTool bootstrap); no silent corruption or half-written state. Note: full offline first-launch is NOT supported — Python/ExifTool/npm artefacts must be fetched once while online |
| 4 | 10k-file library | Point source at a ~10,000-file fixture, run full transfer | All batches complete (`BATCH_SIZE` 100), thumbnails generate, library scrolls without jank, DB stays in WAL mode with no lock errors |
| 5 | iPhone without Apple driver | Connect iPhone on a machine without the Apple Mobile Device driver / MSVC-built `wpd_helper.exe` | App runs normally for local-folder backup; device list shows iPhone as unavailable with guidance (install driver / build helper) instead of crashing |
| 6 | Port occupied | Occupy `127.0.0.1:47821` (or `:5173` for `--frontend`) with another process, run `python run.py` | Orchestrator exits before spawning, naming the blocked port and owner, with the `netstat -ano \| findstr :<port>` / `taskkill /F /PID <pid>` remediation |
| 7 | AV-locked exe | Run with antivirus real-time protection on (or simulate a lock on `backend/bin/wpd_helper.exe` during build) | Build surfaces the `LNK1104` guidance (close backend, retry, check AV/file-sync locks) instead of a raw linker dump; freshly-written binaries are not quarantined on launch |

## Sign-off

- [ ] All matrix rows pass (or failures filed as issues with scenario # referenced).
- [ ] Release notes generated; `Transfera-Setup-*`, `Transfera-Portable-*`, `SHA256SUMS.txt` attached.
