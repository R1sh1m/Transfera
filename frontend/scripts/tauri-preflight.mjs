// Transfera v2 — Tauri preflight gate (cross-platform, Node only).
// Runs BEFORE the slow Rust compile in `npm run tauri:build` and fails fast
// when staged files are missing, instead of failing 9 minutes later at
// bundle time. Two real incidents motivated this:
//   1. `resources/exiftool_files` absent  -> build-script glob failure.
//   2. `binaries/transfera-engine-*-msvc.exe` absent (gnu toolchain staged
//      only the gnu name) -> NSIS bundle failure after a full release build.
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

import { execFileSync } from "node:child_process";
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
const BIN = join(SRC_TAURI, "binaries");
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
function rustHostTriple() {
  try {
    const out = execFileSync("rustc", ["-vV"], { encoding: "utf8" });
    const line = out.split("\n").find((l) => l.startsWith("host:"));
    return line ? line.split(":")[1].trim() : null;
  } catch {
    return null;
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

// -- 3. sidecar (never faked; triple-aware) ---------------------------------
const triple = rustHostTriple();
if (!triple) {
  fail(
    "Could not determine Rust host triple (`rustc -vV` failed) — install Rust via https://rustup.rs.",
  );
} else {
  const ext = IS_WIN ? ".exe" : "";
  const names = IS_WIN
    ? // The NSIS bundler resolves externalBin to the msvc name even when the
      // active toolchain is gnu — require both so neither stage can fail.
      [
        `transfera-engine-x86_64-pc-windows-msvc.exe`,
        `transfera-engine-${triple}.exe`,
      ]
    : [`transfera-engine-${triple}`];
  const sidecarFix = IS_WIN
    ? "powershell -ExecutionPolicy Bypass -File scripts/build-sidecar.ps1  (from repo root)"
    : "bash scripts/build-sidecar.sh  (from repo root)";
  for (const n of new Set(names)) {
    const p = join(BIN, n);
    const size = sizeOf(p);
    if (size <= 0) {
      fail(
        `binaries/${n} ${size === 0 ? "is a 0-byte placeholder" : "missing"} — bundle step will fail.`,
        sidecarFix,
      );
    } else {
      ok(`binaries/${n} (${(size / 1048576).toFixed(1)} MB).`);
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
