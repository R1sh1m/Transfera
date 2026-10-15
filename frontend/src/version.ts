// ---------------------------------------------------------------------------
// Transfera v2 — App Version Source of Truth for Frontend
// Reads the single version from package.json (kept in sync across the repo
// by test_version_single_source.py).
// ---------------------------------------------------------------------------

import packageJson from "../package.json";

export const APP_VERSION: string = packageJson.version;
