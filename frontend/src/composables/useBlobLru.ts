export class BlobLruCache {
  private readonly entries = new Map<string, string>()
  private readonly pending = new Map<string, Promise<string>>()
  private readonly generations = new Map<string, number>()
  private epoch = 0

  constructor(private readonly capacity = 8) {}

  get size(): number {
    return this.entries.size
  }

  async get(key: string, loader: () => Promise<Blob>): Promise<string> {
    const cached = this.entries.get(key)
    if (cached) {
      this.entries.delete(key)
      this.entries.set(key, cached)
      return cached
    }
    const inFlight = this.pending.get(key)
    if (inFlight) return inFlight
    const requestEpoch = this.epoch
    const generation = this.generations.get(key) ?? 0
    const operation = loader().then((blob) => {
      const url = URL.createObjectURL(blob)
      if (requestEpoch !== this.epoch || generation !== (this.generations.get(key) ?? 0)) {
        URL.revokeObjectURL(url)
        throw new Error('图片请求已经过期')
      }
      this.entries.set(key, url)
      this.evict()
      return url
    })
    this.pending.set(key, operation)
    try {
      return await operation
    } finally {
      // A clear() or a newer request may have installed a different
      // promise for the same key while this operation was in flight.  Do not
      // remove that newer entry when the stale promise settles.
      if (this.pending.get(key) === operation) this.pending.delete(key)
    }
  }

  delete(key: string): boolean {
    const url = this.entries.get(key)
    const hadPending = this.pending.delete(key)
    this.generations.set(key, (this.generations.get(key) ?? 0) + 1)
    if (!url) return hadPending
    this.entries.delete(key)
    URL.revokeObjectURL(url)
    return true
  }

  clear(): void {
    this.epoch += 1
    for (const url of this.entries.values()) URL.revokeObjectURL(url)
    this.entries.clear()
    this.pending.clear()
  }

  private evict(): void {
    while (this.entries.size > this.capacity) {
      const oldest = this.entries.entries().next().value as [string, string] | undefined
      if (!oldest) return
      this.entries.delete(oldest[0])
      URL.revokeObjectURL(oldest[1])
    }
  }
}
