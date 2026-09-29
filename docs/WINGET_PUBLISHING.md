# Transfera WinGet Distribution

This directory contains the manifest files required to publish and install **Transfera** via Windows Package Manager (`winget`).

## Manifest Files

- `Transfera.Transfera.yaml` — Root version manifest (PackageIdentifier: `Transfera.Transfera`, Version: `2.6.0`).
- `Transfera.Transfera.installer.yaml` — Installer metadata pointing to the GitHub Release installer binary with SHA-256 integrity hash.
- `Transfera.Transfera.locale.en-US.yaml` — Localized application descriptions, tags, and license metadata.

---

## 🧪 Local Testing

To test the manifest locally before publishing:

### 1. Validate Manifest Schema
```powershell
winget validate --manifest winget/
```

### 2. Test Local Installation
```powershell
winget install --manifest winget/
```

---

## 🚀 Publishing to `microsoft/winget-pkgs`

Once a new release is cut on GitHub (e.g. `v2.6.0`), publish to the official Windows Package Manager repository:

### Method A: Using `wingetcreate` (Automated & Recommended)

1. Install `wingetcreate` if you haven't already:
   ```powershell
   winget install Microsoft.WingetCreate
   ```

2. Submit the package:
   ```powershell
   wingetcreate submit winget/
   ```
   *(Prompts for a GitHub Personal Access Token with `public_repo` scope to automatically fork `microsoft/winget-pkgs` and submit a Pull Request).*

### Method B: Manual PR to `microsoft/winget-pkgs`

1. Fork [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs).
2. Clone your fork and create a branch:
   ```bash
   git checkout -b transfera-2.6.0
   ```
3. Copy the manifest files to:
   `manifests/t/Transfera/Transfera/2.6.0/`
4. Commit and push:
   ```bash
   git commit -m "New version: Transfera.Transfera version 2.6.0"
   git push origin transfera-2.6.0
   ```
5. Open a Pull Request against `microsoft/winget-pkgs:master`.
