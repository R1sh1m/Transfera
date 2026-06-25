export interface ThumbQueue {
  request(onGranted: () => void): void
  release(): void
  reset(): void
}

export function createThumbQueue(maxConcurrent = 4): ThumbQueue {
  let active = 0
  const queue: Array<() => void> = []

  return {
    request(onGranted) {
      if (active < maxConcurrent) {
        active++
        onGranted()
      } else {
        queue.push(onGranted)
      }
    },
    release() {
      if (active > 0) active--
      if (queue.length > 0 && active < maxConcurrent) {
        const next = queue.shift()!
        active++
        next()
      }
    },
    reset() {
      queue.length = 0
      active = 0
    },
  }
}
