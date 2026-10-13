export interface ThumbQueue {
  /** Enqueue `onGranted`. Returns a cancel function that removes the
   *  pending grant (no-op if already granted). Grant fires at most
   *  `maxConcurrent` at a time — the granted callback is where the
   *  network fetch must start, so concurrency actually gates I/O. */
  request(onGranted: () => void): () => void;
  /** Release a previously granted slot. When `epoch` is provided and no
   *  longer matches the current generation (page turned since acquire),
   *  the release is absorbed against the stale generation and never
   *  touches the current generation's accounting. */
  release(epoch?: number): void;
  reset(): void;
  getEpoch(): number;
  setMaxConcurrent(n: number): void;
  /** Current (possibly adapted) concurrency limit. */
  getMaxConcurrent(): number;
  /** Record a successful gated fetch — drives slow recovery back toward
   *  the base limit after a backoff. */
  reportSuccess(): void;
  /** Record a throttled/transient failure (HTTP 503 / Retry-After) —
   *  backs the limit off toward the floor. */
  reportThrottled(retryAfterMs?: number | null): void;
}

// Adaptive-concurrency bounds for the slow extract lane: USB/iOS sources
// back off toward the floor under 503 pressure and recover on success.
const ADAPT_MIN = 2;
const ADAPT_MAX = 6;
// EWMA weight per sample + consecutive successes needed to regain one lane.
const THROTTLE_EWMA_ALPHA = 0.3;
const RECOVER_AFTER_SUCCESSES = 8;

export function createThumbQueue(maxConcurrent = 4): ThumbQueue {
  let max = Math.max(1, maxConcurrent);
  let baseMax = Math.min(ADAPT_MAX, Math.max(ADAPT_MIN, maxConcurrent));
  // EWMA of recent throttle samples (1 = throttled, 0 = success) plus a
  // success streak gating lane recovery. Cheap bookkeeping, no timers.
  let throttleEwma = 0;
  let successStreak = 0;
  // Granted-but-not-released slots per generation epoch. A reset() starts
  // a fresh generation at 0 so in-flight work from a previous page never
  // eats the new page's lanes; stale releases only retire their own
  // generation's count (callers additionally guard with slotEpochRef).
  const activeByEpoch = new Map<number, number>();
  const queue: Array<{ run: () => void; cancelled: boolean; epoch: number }> =
    [];
  let epoch = 0;

  function activeFor(ep: number): number {
    return activeByEpoch.get(ep) ?? 0;
  }

  function drain() {
    while (queue.length > 0 && activeFor(epoch) < max) {
      const next = queue.shift()!;
      // Drop grants from a previous page generation: reset() bumps the
      // epoch, so retries scheduled before a page turn never fire after.
      if (next.cancelled || next.epoch !== epoch) continue;
      activeByEpoch.set(epoch, activeFor(epoch) + 1);
      next.run();
    }
  }

  return {
    request(onGranted) {
      // Capture the generation now; the grant fires only if no reset()
      // happened in between (checked both here and in drain()).
      const ep = epoch;
      const guarded = () => {
        if (ep !== epoch) return;
        onGranted();
      };
      if (activeFor(epoch) < max) {
        activeByEpoch.set(epoch, activeFor(epoch) + 1);
        guarded();
        return () => {};
      }
      const entry = { run: guarded, cancelled: false, epoch: ep };
      queue.push(entry);
      return () => {
        entry.cancelled = true;
      };
    },
    release(ep?: number) {
      const target = ep ?? epoch;
      // Stale-generation release: never touch the current generation's
      // accounting — just retire the old generation's own count (or drop
      // it when already zero). Callers with an epoch mismatch normally
      // skip release() entirely; this is the second layer of defense.
      if (target !== epoch) {
        const c = activeFor(target);
        if (c > 0) activeByEpoch.set(target, c - 1);
        return;
      }
      const c = activeFor(epoch);
      if (c > 0) activeByEpoch.set(epoch, c - 1);
      drain();
    },
    reset() {
      // New generation: cancel queued (not-yet-granted) work only. Slots
      // held by in-flight fetches stay charged against the OLD epoch, and
      // the new epoch starts with a full lane budget — paging can never
      // stall on negative/clamped accounting from unmounted cells.
      epoch++;
      for (const e of queue) e.cancelled = true;
      queue.length = 0;
      if (!activeByEpoch.has(epoch)) activeByEpoch.set(epoch, 0);
      // Bound memory: drop counts for generations older than the previous.
      for (const k of [...activeByEpoch.keys()]) {
        if (k < epoch - 1) activeByEpoch.delete(k);
      }
    },
    getEpoch() {
      return epoch;
    },
    setMaxConcurrent(n: number) {
      max = Math.max(1, n);
      baseMax = Math.min(ADAPT_MAX, Math.max(ADAPT_MIN, n));
      successStreak = 0;
      drain();
    },
    getMaxConcurrent() {
      return max;
    },
    reportSuccess() {
      throttleEwma = (1 - THROTTLE_EWMA_ALPHA) * throttleEwma;
      successStreak += 1;
      // Regain lanes slowly: one lane per RECOVER_AFTER_SUCCESSES
      // consecutive successes, never above the base limit.
      if (successStreak >= RECOVER_AFTER_SUCCESSES && max < baseMax) {
        successStreak = 0;
        max = Math.min(baseMax, max + 1);
        drain();
      }
    },
    reportThrottled(retryAfterMs?: number | null) {
      throttleEwma =
        THROTTLE_EWMA_ALPHA + (1 - THROTTLE_EWMA_ALPHA) * throttleEwma;
      successStreak = 0;
      // An explicit Retry-After means the backend is shedding load — drop
      // two lanes (e.g. 4→2); other transients drop one. Clamped to [2, 6].
      const step = retryAfterMs != null ? 2 : 1;
      const next = Math.min(ADAPT_MAX, Math.max(ADAPT_MIN, max - step));
      if (next !== max) max = next;
    },
  };
}
