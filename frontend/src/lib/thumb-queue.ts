export interface ThumbQueue {
  /** Enqueue `onGranted`. Returns a cancel function that removes the
   *  pending grant (no-op if already granted). Grant fires at most
   *  `maxConcurrent` at a time — the granted callback is where the
   *  network fetch must start, so concurrency actually gates I/O. */
  request(onGranted: () => void): () => void;
  release(): void;
  reset(): void;
  getEpoch(): number;
  setMaxConcurrent(n: number): void;
}

export function createThumbQueue(maxConcurrent = 4): ThumbQueue {
  let max = Math.max(1, maxConcurrent);
  let active = 0;
  const queue: Array<{ run: () => void; cancelled: boolean }> = [];
  let epoch = 0;

  function drain() {
    while (queue.length > 0 && active < max) {
      const next = queue.shift()!;
      if (next.cancelled) continue;
      active++;
      next.run();
    }
  }

  return {
    request(onGranted) {
      if (active < max) {
        active++;
        onGranted();
        return () => {};
      }
      const entry = { run: onGranted, cancelled: false };
      queue.push(entry);
      return () => {
        entry.cancelled = true;
      };
    },
    release() {
      if (active > 0) active--;
      drain();
    },
    reset() {
      epoch++;
      for (const e of queue) e.cancelled = true;
      queue.length = 0;
      active = 0;
    },
    getEpoch() {
      return epoch;
    },
    setMaxConcurrent(n: number) {
      max = Math.max(1, n);
      drain();
    },
  };
}
