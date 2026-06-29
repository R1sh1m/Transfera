import os
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RELEASE_DIR = ROOT / "frontend" / "release"
CERTS_DIR = ROOT / "frontend" / "release-certs"
SCRIPTS_DIR = ROOT / "frontend" / "scripts"

def zip_dir(src_dir: Path, dest_zip: Path):
    print(f"Compressing {src_dir} to {dest_zip}...")
    count = 0
    with zipfile.ZipFile(dest_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(src_dir):
            for file in files:
                file_path = Path(root) / file
                arcname = file_path.relative_to(src_dir)
                zipf.write(file_path, arcname)
                count += 1
                if count % 1000 == 0:
                    print(f"  Compressed {count} files...")

# 1. Zip win-unpacked
win_unpacked = RELEASE_DIR / "win-unpacked"
portable_zip = RELEASE_DIR / "Transfera-Portable-2.4.0.zip"
if portable_zip.exists():
    portable_zip.unlink()
zip_dir(win_unpacked, portable_zip)
print("Portable ZIP created successfully!")

# 2. Package installer files
installer_pack_dir = RELEASE_DIR / "Transfera-Installer-2.4.0"
if installer_pack_dir.exists():
    shutil.rmtree(installer_pack_dir)
installer_pack_dir.mkdir(exist_ok=True)

shutil.copy(RELEASE_DIR / "Transfera-Setup-2.4.0.exe", installer_pack_dir)
shutil.copy(CERTS_DIR / "transfera-release.cer", installer_pack_dir)
shutil.copy(SCRIPTS_DIR / "trust-and-install.bat", installer_pack_dir)

installer_zip = RELEASE_DIR / "Transfera-Installer-2.4.0.zip"
if installer_zip.exists():
    installer_zip.unlink()
zip_dir(installer_pack_dir, installer_zip)
shutil.rmtree(installer_pack_dir)
print("Installer package ZIP created successfully!")
