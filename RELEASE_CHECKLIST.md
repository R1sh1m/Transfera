# Transfera Release Checklist

Run through this matrix before publishing a GitHub release tag (`v*`).
The `release.yml` workflow gates on `frontend/package.json` ↔ `pyproject.toml` ↔
`frontend/src-tauri/tauri.conf.json` ↔ `frontend/src-tauri/Cargo.toml` all matching the tag.

## Pre-flight

- [x] `frontend/package.json`, `pyproject.toml`, `src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml` versions all equal the tag (without the leading `v`).
- [ ] `python run.py` boots clean on a dev machine (backend `:47821` healthy, Tauri window opens).
- [x] `SHA256SUMS.txt` generated and verified in CI for the shipped Tauri installer.

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
| 8 | Transfer complete notification | Complete a transfer, click the native OS notification | App opens/focuses and navigates to the HTML report or Dashboard session row |
| 9 | Tauri fresh install | Remove `%APPDATA%/Transfera`, install Tauri NSIS build, launch offline-capable | Dashboard reachable with zero first-run downloads (frozen sidecar + single-file ExifTool bundled); no setup screen for base |
| 10 | Tauri sidecar crash | Kill `transfera-engine` mid-transfer | `backend:down` screen appears; Resume recovers via `recovery.py`, no partials in archive |
| 11 | Second launch while running | Launch Tauri app twice | Single-instance focuses existing window; no second backend on `:47821` |
| 12 | 10k-file scroll (perf) | 10k-file library, scroll vault + resize window | No visible jank; columns update without full-grid flash (memoized cards, bucketed masonry, queued thumbnails) |

## v2.7.0 verification (2026-09-30)

E2E run by the agent before tagging (see release job for v2.7.0):

- [x] Versions match tag (CI version gate passed)
- [x] `SHA256SUMS.txt` generated + verified in CI; `Transfera_2.7.0_x64-setup.exe` published
- [x] Backend: 185 pytest passed, ruff clean; frontend typecheck clean; Rust fmt + clippy clean
- [x] Frozen engine rebuilt + `/api/health` smoke passed (ExifTool Tier-0, Tier 1 init, migrations 19–47)
- [x] Full `tauri:build` → NSIS installed to a clean dir → launched → engine spawned from bundle resources → `/api/health 200`
- [x] #6 port occupied: gate exits 1 with remediation text
- [x] #9 Tauri fresh install: covered by the install + launch probe above
- [ ] `python run.py` full-stack boot with visible Tauri window (backend proven separately; window not observed headless)
- [ ] #2 upgrade, #3 offline first launch, #4/#12 10k-file perf, #5 iPhone without driver, #7 AV lock, #8 notification click, #10 mid-transfer kill screen, #11 second launch — need hardware/manual runs

## Post-2.7.0 installer fix (on main, unreleased)

- [x] `Install-Transfera.ps1` aborted Step 7 on the first vite/rustc/npm stderr warning: with `$ErrorActionPreference = "Stop"`, PS 5.1 turns `2>&1`-merged native stderr into terminating errors. All native pipelines now go through `Invoke-Native` (scoped Continue + explicit exit-code gates); mechanism proven under both 5.1 and 7.x.
- [ ] Re-run the full installer from the main repo checkout (the failing run used the stale `test\Transfera` checkout, which predates the sidecar/resources fixes — always install from the repo with the fixes).

## Post-2.7.0 findings, second pass (unreleased)

- [x] Tier 2 bridge died with rc=2 and unreadable stderr: the bridge-output decoder tried utf-16-le first (never raises on even-length bytes, produced CJK garbage). Now uses the BOM/pattern-sniffing `_decode_wsl_output` helper, plus a distro pre-flight probe (python3 + fastapi/uvicorn) that names the real cause with an actionable message.
- [x] Frozen smoke probe could download ExifTool instead of using Tier-0: helper staging moved before `build-sidecar`, so the gate exercises the exact production resolution path.
- [x] Bare `0` lines in installer output: `Invoke-Native` return values leaked to output on fire-and-forget calls; now `$null =`-assigned.
- [x] Installer mojibake on legacy conhost: UTF-8 output encoding + `chcp 65001` at startup (best-effort); README troubleshooting row added.
- [x] Wrong app-data path in docs/banner (`%APPDATA%\Transfera`): Tauri uses the app identifier — corrected to `%APPDATA%\com.transfera.app` in README + installer banner.
- [x] PyInstaller `pysqlite2`/`MySQLdb`/`coredll.lib` warnings documented as known-benign in the spec (suppressing them would blind us to real bundle gaps).
- [x] Step 5 staleness guard: warns on dirty tree / behind-upstream checkouts (offline-safe, local refs only).
- [x] Installer wizard hang: explicit "complete the installer window" guidance + exit-code check (cancel detected instead of a false ALL DONE).
- [x] Re-run the full installer from the main repo checkout (above) and confirm the NSIS wizard completes.

## Fresh-install verification (main repo, post-ae27fb7)

- [x] Old install uninstalled, `test\Transfera` clone + temp artifacts removed (user DB kept)
- [x] Full 9-step installer from main repo completed (venv reuse skipped recreate; pip/npm/vite/PyInstaller/smoke/Rust/NSIS all green)
- [x] Silent NSIS install to `Desktop\Software\Transfera`, launched → engine spawned from bundle resources → `/api/health` 200
- [x] CORS preflight from `http://tauri.localhost` against the installed engine: 200 + ACAO header (the Engine Unavailable root cause, fixed)
- [ ] Confirm the app window itself leaves the error screen for the dashboard (backend proven; UI state needs eyes)

## Sign-off

- [ ] All matrix rows pass (or failures filed as issues with scenario # referenced).
- [x] Release notes generated; `Transfera-Setup-*`, `Transfera-Portable-*`, `SHA256SUMS.txt` attached.
