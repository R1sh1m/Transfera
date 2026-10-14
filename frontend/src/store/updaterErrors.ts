// ---------------------------------------------------------------------------
// Transfera v2 — Updater error copy mapping (pure, zero-dependency)
// Maps raw updater-plugin / reqwest / fetch errors to user-friendly copy.
// Raw detail stays in console (callers log `err`); the store/UI only ever
// receives the friendly text so long URLs never render in the banner.
// ---------------------------------------------------------------------------

export function updaterErrorMessage(err: unknown, fallback: string): string {
  let raw: string | null = null;
  if (err instanceof Error && err.message) raw = err.message;
  else if (typeof err === "string" && err) raw = err;
  else {
    try {
      const s = JSON.stringify(err);
      if (s && s !== "{}" && s !== "null") raw = s;
    } catch {
      /* fall through */
    }
  }
  if (raw) {
    const lower = raw.toLowerCase();
    // Map the updater-plugin ordering error to friendly copy.
    // Narrow match: broader "download" text would mislabel check/network
    // failures (which trigger no retry) as a re-download in progress.
    if (lower.includes("install called before")) {
      return "The update wasn't fully downloaded. Trying the download again.";
    }
    // Map updater-manifest deserialization failures (e.g. serde
    // "invalid type: sequence, expected a string" when notes is a JSON
    // array) to friendly copy.
    if (lower.includes("invalid type") || lower.includes("expected a string")) {
      return "Couldn't read the update info from the release server. Please try again later.";
    }
    // Map network / connectivity failures (reqwest, fetch, DNS, timeouts) to
    // friendly copy so raw request URLs never render in the UI.
    if (
      lower.includes("error sending request") ||
      lower.includes("failed to fetch") ||
      lower.includes("network") ||
      lower.includes("timeout") ||
      lower.includes("timed out") ||
      lower.includes("dns") ||
      lower.includes("connection") ||
      lower.includes("offline") ||
      lower.includes("http://") ||
      lower.includes("https://")
    ) {
      return "Couldn't check for updates. Check your internet connection and try again.";
    }
    return raw;
  }
  return fallback;
}
