// Transfera v2 — Tauri preflight gate (cross-platform, Node only).
// Runs BEFORE the slow Rust compile in `npm run tauri:build` and fails fast
// when staged files are missing, instead of failing 9 minutes later at
// bundle time. Three real incidents motivated this:
//   1. `resources/exiftool_files` absent  -> build-script glob failure.
//   2. One-dir sidecar shipped as externalBin (exe only, no _internal/) ->
//      installed engine dies with "Failed to load Python DLL". The bundle
//      must ship whole as resources/transfera-engine/.
//   3. `Copy-Item exiftool_files <dest>` nested the runtime tree one level
//      too deep -> ExifTool stub reports missing perl5*.dll.
//
// Policy:
//   - Windows: resources must be REAL (size > 0). 0-byte placeholders exist
//     so `cargo check` passes, but they would ship a broken installer.
//   - macOS/Linux: wpd_helper/exiftool are Windows-only; missing resource
//     files are auto-created as 0-byte placeholders (the backend ignores
//     0-byte files and resolves the system ExifTool via PATH). The sidecar
//     is still required and never faked.
//   - `frontend/dist` missing is a WARNING only: tauri's beforeBuildCommand
//     (`npm run build`) creates it after this script runs.

import {
  existsSync,
  mkdirSync,
  readdirSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SRC_TAURI = join(ROOT, "frontend", "src-tauri");
const RES = join(SRC_TAURI, "resources");
const IS_WIN = process.platform === "win32";

const failures = [];
const warnings = [];
const ok = (m) => console.log(`  [OK] ${m}`);
const warn = (m) => {
  warnings.push(m);
  console.log(`  [WARN] ${m}`);
};
const fail = (m, fix) => {
  failures.push(m);
  console.log(`  [FAIL] ${m}`);
  if (fix) console.log(`         fix: ${fix}`);
};

function sizeOf(p) {
  try {
    return statSync(p).size;
  } catch {
    return -1;
  }
}

// -- 1. frontend dist (warning only; beforeBuildCommand creates it) --------
if (!existsSync(join(ROOT, "frontend", "dist", "index.html"))) {
  warn(
    "frontend/dist/index.html missing — `npm run build` (beforeBuildCommand) will create it.",
  );
} else {
  ok("frontend/dist present.");
}

// -- 2. staged resources ----------------------------------------------------
for (const name of ["wpd_helper.exe", "exiftool.exe"]) {
  const p = join(RES, name);
  const size = sizeOf(p);
  const stageSrc =
    name === "exiftool.exe"
      ? "backend/bin/exiftool/exiftool.exe"
      : `backend/bin/${name}`;
  if (size < 0) {
    if (IS_WIN) {
      fail(
        `resources/${name} missing — Tauri build-script will fail.`,
        `Copy-Item ${stageSrc} frontend/src-tauri/resources/${name} -Force`,
      );
    } else {
      writeFileSync(p, "");
      warn(
        `resources/${name} missing — created 0-byte placeholder (Windows-only helper; backend ignores empty files).`,
      );
    }
  } else if (size === 0 && IS_WIN) {
    fail(
      `resources/${name} is a 0-byte placeholder — staging was skipped; installer would be broken.`,
      `Copy-Item ${stageSrc} frontend/src-tauri/resources/${name} -Force`,
    );
  } else {
    ok(`resources/${name} (${size} bytes).`);
  }
}

// -- 2b. VC++ redistributable (Windows/NSIS only) ------------------------------
// Without it, machines lacking any VS/redist software show the bootloader's
// "Failed to load Python DLL" dialog on first launch. The NSIS post-install
// hook consumes it; dmg/deb installers ignore it (0-byte placeholder here
// is fine — same policy as the helpers above).
{
  const vcName = "VC_redist.x64.exe";
  const p = join(RES, vcName);
  const size = sizeOf(p);
  const vcFix =
    "powershell -ExecutionPolicy Bypass -File scripts/build-sidecar.ps1  (from repo root; downloads it) or https://aka.ms/vs/17/release/vc_redist.x64.exe";
  if (size < 0) {
    if (IS_WIN) {
      fail(
        `resources/${vcName} missing — end-user machines without the VC++ runtime would hit "Failed to load Python DLL".`,
        vcFix,
      );
    } else {
      writeFileSync(p, "");
      warn(
        `resources/${vcName} missing — created 0-byte placeholder (NSIS-only; ignored on this OS).`,
      );
    }
  } else if (size === 0 && IS_WIN) {
    fail(
      `resources/${vcName} is a 0-byte placeholder — run the build-sidecar download so the NSIS hook has a real runtime to install.`,
      vcFix,
    );
  } else {
    ok(`resources/${vcName} (${size} bytes).`);
  }
}

const exifDir = join(RES, "exiftool_files");
let exifCount = -1;
try {
  exifCount = readdirSync(exifDir).length;
} catch {
  exifCount = -1;
}
if (exifCount < 0) {
  if (IS_WIN) {
    fail(
      "resources/exiftool_files/ missing — Tauri build-script will fail.",
      "New-Item -ItemType Directory -Path frontend/src-tauri/resources/exiftool_files -Force | Out-Null; Copy-Item backend/bin/exiftool/exiftool_files/* frontend/src-tauri/resources/exiftool_files -Recurse -Force",
    );
  } else {
    mkdirSync(exifDir, { recursive: true });
    warn(
      "resources/exiftool_files/ missing — created empty dir (ExifTool resolves via system PATH on this OS).",
    );
  }
} else if (IS_WIN && exifCount <= 1) {
  // Only .gitkeep (or empty): the stub exe cannot run without its Perl tree.
  fail(
    `resources/exiftool_files/ has no runtime tree (${exifCount} entries) — ExifTool would be broken in the installer.`,
    "Copy-Item backend/bin/exiftool/exiftool_files/* frontend/src-tauri/resources/exiftool_files -Recurse -Force",
  );
} else {
  ok(`resources/exiftool_files/ (${exifCount} entries).`);
}

// -- 3. sidecar one-dir folder (never faked) ----------------------------------
// The PyInstaller one-dir bundle must ship WHOLE as a resource: the exe
// alone cannot start without its _internal/ runtime (bootloader dies with
// "Failed to load Python DLL"). A real bundle has hundreds of files; only
// .gitkeep (or nothing) means build-sidecar never ran.
const engineDir = join(RES, "transfera-engine");
const engineExeName = IS_WIN ? "transfera-engine.exe" : "transfera-engine";
const engineExe = join(engineDir, engineExeName);
const sidecarFix = IS_WIN
  ? "powershell -ExecutionPolicy Bypass -File scripts/build-sidecar.ps1  (from repo root)"
  : "bash scripts/build-sidecar.sh  (from repo root)";
const engineSize = sizeOf(engineExe);
let internalCount = -1;
try {
  internalCount = readdirSync(join(engineDir, "_internal")).length;
} catch {
  internalCount = -1;
}
if (engineSize <= 0) {
  fail(
    `resources/transfera-engine/${engineExeName} ${engineSize === 0 ? "is a 0-byte placeholder" : "missing"} — the installed engine cannot start.`,
    sidecarFix,
  );
} else {
  ok(
    `resources/transfera-engine/${engineExeName} (${(engineSize / 1048576).toFixed(1)} MB).`,
  );
}
if (internalCount < 10) {
  fail(
    `resources/transfera-engine/_internal/ has no runtime tree (${internalCount} entries) — the installed engine would die with "Failed to load Python DLL".`,
    sidecarFix,
  );
} else {
  ok(`resources/transfera-engine/_internal/ (${internalCount} entries).`);
}

// -- 4. NSIS branding images (Windows only) ----------------------------------
if (IS_WIN) {
  const ICONS = join(SRC_TAURI, "icons");
  for (const bmpName of ["nsis-header.bmp", "nsis-sidebar.bmp"]) {
    const p = join(ICONS, bmpName);
    const size = sizeOf(p);
    if (size <= 0) {
      fail(
        `icons/${bmpName} ${size === 0 ? "is 0 bytes" : "missing"} — NSIS installer will use the default logo.`,
        "python scripts/generate-nsis-images.py  (from repo root)",
      );
    } else {
      ok(`icons/${bmpName} (${size} bytes).`);
    }
  }
}

// -- verdict ----------------------------------------------------------------
console.log(
  `\ntauri-preflight: ${failures.length} failure(s), ${warnings.length} warning(s).`,
);
if (failures.length > 0) {
  console.log("Fix the items above, then re-run `npm run tauri:build`.");
  process.exit(1);
}
