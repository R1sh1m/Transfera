// ---------------------------------------------------------------------------
// Transfera v2 — Thumbnail Fetch Utility
// Module-level negative cache to prevent endless 404 retry loops.
// ---------------------------------------------------------------------------

const _thumbFailCache = new Map<number, number>()
const THUMB_RETRY_DELAY = 60_000

export async function fetchThumbnail(
  mediaId: number,
  signal?: AbortSignal,
): Promise<string | null> {
  const lastFail = _thumbFailCache.get(mediaId)
  if (lastFail && Date.now() - lastFail < THUMB_RETRY_DELAY) {
    return null
  }

  try {
    const res = await fetch(`/api/media/${mediaId}/thumbnail`, { signal })
    if (!res.ok) {
      _thumbFailCache.set(mediaId, Date.now())
      return null
    }
    _thumbFailCache.delete(mediaId)
    const blob = await res.blob()
    return URL.createObjectURL(blob)
  } catch {
    _thumbFailCache.set(mediaId, Date.now())
    return null
  }
}
