import { beforeEach, describe, expect, it, vi } from 'vitest'

import { BlobLruCache } from './useBlobLru'

describe('BlobLruCache', () => {
  const revoked: string[] = []
  let sequence = 0

  beforeEach(() => {
    revoked.length = 0
    sequence = 0
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => `blob:test-${++sequence}`),
      revokeObjectURL: vi.fn((url: string) => revoked.push(url)),
    })
  })

  it('evicts the oldest preview and revokes its URL', async () => {
    const cache = new BlobLruCache(2)
    const loader = () => Promise.resolve(new Blob(['page']))
    expect(await cache.get('1', loader)).toBe('blob:test-1')
    expect(await cache.get('2', loader)).toBe('blob:test-2')
    await cache.get('3', loader)
    expect(cache.size).toBe(2)
    expect(revoked).toContain('blob:test-1')
  })

  it('reuses cached URLs and clears all retained URLs', async () => {
    const cache = new BlobLruCache(8)
    const loader = vi.fn(() => Promise.resolve(new Blob(['page'])))
    const first = await cache.get('page', loader)
    expect(await cache.get('page', loader)).toBe(first)
    expect(loader).toHaveBeenCalledTimes(1)
    cache.clear()
    expect(revoked).toEqual([first])
    expect(cache.size).toBe(0)
  })
})
