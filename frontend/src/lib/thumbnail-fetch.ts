// ---------------------------------------------------------------------------
// Transfera v2 — Thumbnail Fetch Utility
// Module-level negative cache to prevent endless 404 retry loops.
// ---------------------------------------------------------------------------

import { API_BASE_URL } from "./api-client";

const _thumbFailCache = new Map<number, number>();
const THUMB_RETRY_DELAY = 60_000;

/** Live blob URLs by media id. Every URL minted here must be revoked exactly
 *  once — otherwise each re-fetch leaks a blob in renderer memory (tens of KB
 *  per thumbnail, unbounded across a long library session). */
const _liveUrls = new Map<number, string>();

/** Revoke the live blob URL for one item, if any. Safe to call any time. */
export function revokeThumbnail(mediaId: number): void {
  const url = _liveUrls.get(mediaId);
  if (url) {
    _liveUrls.delete(mediaId);
    try {
      URL.revokeObjectURL(url);
    } catch {
      // Already revoked or never valid — ignore.
    }
  }
}

/** Revoke every live blob URL (library cleared / session switched). */
export function revokeAllThumbnails(): void {
  for (const mediaId of [..._liveUrls.keys()]) revokeThumbnail(mediaId);
}

/** Call this when library is cleared or a new session starts, so stale
 *  negative-cache entries from the previous session don't block new fetches. */
export function clearThumbFailCache(): void {
  _thumbFailCache.clear();
  revokeAllThumbnails();
}

export async function fetchThumbnail(
  mediaId: number,
  updatedAtOrSignal?: string | Date | number | AbortSignal,
  signal?: AbortSignal,
): Promise<string | null> {
  let updatedAt: string | Date | number | undefined;
  let activeSignal: AbortSignal | undefined;

  if (updatedAtOrSignal instanceof AbortSignal) {
    activeSignal = updatedAtOrSignal;
  } else {
    updatedAt = updatedAtOrSignal;
    activeSignal = signal;
  }

  const lastFail = _thumbFailCache.get(mediaId);
  if (lastFail && Date.now() - lastFail < THUMB_RETRY_DELAY) {
    return null;
  }

  try {
    const t = updatedAt ? new Date(updatedAt).getTime() : "";
    const url = `${API_BASE_URL}/api/media/${mediaId}/thumbnail${t ? `?t=${t}` : ""}`;
    const res = await fetch(url, { signal: activeSignal });
    if (res.status === 404 || res.status === 204) return null;
    if (!res.ok) {
      _thumbFailCache.set(mediaId, Date.now());
      return null;
    }

    const statusHeader = res.headers.get("X-Thumbnail-Status");
    if (statusHeader === "ready") {
      _thumbFailCache.delete(mediaId);
      const blob = await res.blob();
      // Replace (and revoke) any previous URL for this item so refreshes
      // can't accumulate orphaned blobs.
      revokeThumbnail(mediaId);
      const objectUrl = URL.createObjectURL(blob);
      _liveUrls.set(mediaId, objectUrl);
      return objectUrl;
    } else if (statusHeader === "failed") {
      _thumbFailCache.set(mediaId, Date.now());
      return null;
    } else {
      // 'not_found' or other pending state — do not cache in fail cache
      return null;
    }
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      return null;
    }
    _thumbFailCache.set(mediaId, Date.now());
    return null;
  }
}
