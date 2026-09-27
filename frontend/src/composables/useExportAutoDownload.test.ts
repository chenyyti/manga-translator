import { describe, expect, it, vi } from 'vitest'

import type { ExportArtifact } from '@/types'

import { downloadExportInBrowser, useExportAutoDownload } from './useExportAutoDownload'

function artifact(id: string, status: string, format: 'zip' | 'epub' = 'zip'): ExportArtifact {
  return {
    id,
    project_id: 'project-1',
    task_id: `task-${id}`,
    format,
    status,
    filename: `book.${format}`,
    size: 100,
    sha256: null,
    page_count: 1,
    error_code: null,
    error_message: null,
    created_at: '',
    updated_at: '',
    download_url: status === 'completed' ? `/api/exports/${id}/download` : null,
  }
}

function memoryStorage() {
  const items = new Map<string, string>()
  return {
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => {
      items.set(key, value)
    },
  }
}

describe('useExportAutoDownload', () => {
  it('starts a browser download with the archive filename', () => {
    const clicked: HTMLAnchorElement[] = []
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push(this)
    })
    try {
      downloadExportInBrowser(artifact('zip', 'completed'))
      expect(clicked[0]?.getAttribute('href')).toBe('/api/exports/zip/download')
      expect(clicked[0]?.download).toBe('book.zip')
      expect(document.body.contains(clicked[0]!)).toBe(false)
    } finally {
      click.mockRestore()
    }
  })

  it('waits for a running export and downloads when its task completes', async () => {
    let status = 'running'
    const download = vi.fn()
    const tracker = useExportAutoDownload({
      currentProjectId: () => 'project-1',
      getExport: vi.fn(async () => artifact('zip', status)),
      download,
      onFailure: vi.fn(),
      storage: memoryStorage(),
    })
    tracker.start()
    tracker.track({ projectId: 'project-1', exportId: 'zip', taskId: 'task-zip' })
    await vi.waitFor(async () => {
      await tracker.check()
      expect(download).not.toHaveBeenCalled()
    })

    status = 'completed'
    await vi.waitFor(async () => {
      await tracker.check('task-zip')
      expect(download).toHaveBeenCalledTimes(1)
    })
    tracker.stop()
  })

  it('downloads completed ZIP and EPUB exports only once', async () => {
    const storage = memoryStorage()
    const download = vi.fn()
    const getExport = vi.fn(async (id: string) =>
      artifact(id, 'completed', id === 'epub' ? 'epub' : 'zip'),
    )
    const tracker = useExportAutoDownload({
      currentProjectId: () => 'project-1',
      getExport,
      download,
      onFailure: vi.fn(),
      storage,
    })
    tracker.start()
    tracker.track({ projectId: 'project-1', exportId: 'zip', taskId: 'task-zip' })
    tracker.track({ projectId: 'project-1', exportId: 'epub', taskId: 'task-epub' })

    await vi.waitFor(() => expect(download).toHaveBeenCalledTimes(2))
    await tracker.check('task-zip')
    await tracker.check('task-epub')
    expect(download.mock.calls.map(([item]) => item.format).sort()).toEqual(['epub', 'zip'])
    expect(getExport).toHaveBeenCalledTimes(2)
    tracker.stop()
  })

  it('resumes a pending download after leaving and returning to the project', async () => {
    const storage = memoryStorage()
    const first = useExportAutoDownload({
      currentProjectId: () => 'project-1',
      getExport: vi.fn(async () => artifact('zip', 'running')),
      download: vi.fn(),
      onFailure: vi.fn(),
      storage,
    })
    first.track({ projectId: 'project-1', exportId: 'zip', taskId: 'task-zip' })
    first.start()
    await first.check()
    first.stop()

    const download = vi.fn()
    const resumed = useExportAutoDownload({
      currentProjectId: () => 'project-1',
      getExport: vi.fn(async () => artifact('zip', 'completed')),
      download,
      onFailure: vi.fn(),
      storage,
    })
    resumed.start()
    await vi.waitFor(() => expect(download).toHaveBeenCalledTimes(1))
    await resumed.check()
    expect(download).toHaveBeenCalledTimes(1)
    resumed.stop()
  })

  it('clears failed and cancelled exports without downloading', async () => {
    const download = vi.fn()
    const onFailure = vi.fn()
    const getExport = vi.fn(async (id: string) =>
      artifact(id, id === 'failed' ? 'failed' : 'cancelled'),
    )
    const tracker = useExportAutoDownload({
      currentProjectId: () => 'project-1',
      getExport,
      download,
      onFailure,
      storage: memoryStorage(),
    })
    tracker.start()
    tracker.track({ projectId: 'project-1', exportId: 'failed', taskId: 'task-failed' })
    tracker.track({ projectId: 'project-1', exportId: 'cancelled', taskId: 'task-cancelled' })
    await vi.waitFor(() => expect(onFailure).toHaveBeenCalledTimes(2))
    await tracker.check()
    expect(download).not.toHaveBeenCalled()
    expect(getExport).toHaveBeenCalledTimes(2)
    tracker.stop()
  })

  it('keeps an export queued after a temporary request failure', async () => {
    const download = vi.fn()
    const getExport = vi
      .fn<(_id: string) => Promise<ExportArtifact>>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(artifact('zip', 'completed'))
    const tracker = useExportAutoDownload({
      currentProjectId: () => 'project-1',
      getExport,
      download,
      onFailure: vi.fn(),
      storage: memoryStorage(),
    })
    tracker.start()
    tracker.track({ projectId: 'project-1', exportId: 'zip', taskId: 'task-zip' })
    await vi.waitFor(() => expect(getExport).toHaveBeenCalledTimes(1))
    await vi.waitFor(async () => {
      await tracker.check()
      expect(download).toHaveBeenCalledTimes(1)
    })
    tracker.stop()
  })
})
