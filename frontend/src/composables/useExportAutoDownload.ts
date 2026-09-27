import type { ExportArtifact } from '@/types'

const STORAGE_KEY = 'manga-translator:pending-export-downloads'
const POLL_INTERVAL_MS = 3000

interface PendingExport {
  projectId: string
  exportId: string
  taskId: string
}

interface ExportAutoDownloadOptions {
  currentProjectId: () => string
  getExport: (exportId: string) => Promise<ExportArtifact>
  download: (artifact: ExportArtifact) => void
  onFailure: (message: string) => void
  storage?: Pick<Storage, 'getItem' | 'setItem'>
}

export function downloadExportInBrowser(artifact: ExportArtifact): void {
  if (!artifact.download_url) throw new Error('导出文件尚未就绪')
  const link = document.createElement('a')
  link.href = artifact.download_url
  link.download = artifact.filename
  link.hidden = true
  document.body.appendChild(link)
  try {
    link.click()
  } finally {
    link.remove()
  }
}

export function useExportAutoDownload(options: ExportAutoDownloadOptions) {
  const pending = new Map<string, PendingExport>()
  const checking = new Set<string>()
  let timer: ReturnType<typeof setInterval> | null = null
  let active = false

  function storage(): Pick<Storage, 'getItem' | 'setItem'> | null {
    try {
      return options.storage ?? window.sessionStorage
    } catch {
      return null
    }
  }

  function persist(): void {
    try {
      storage()?.setItem(STORAGE_KEY, JSON.stringify([...pending.values()]))
    } catch {
      // A blocked sessionStorage must not prevent downloads in the open page.
    }
  }

  try {
    const saved = JSON.parse(storage()?.getItem(STORAGE_KEY) ?? '[]') as unknown
    if (Array.isArray(saved)) {
      for (const item of saved) {
        if (
          item &&
          typeof item.projectId === 'string' &&
          typeof item.exportId === 'string' &&
          typeof item.taskId === 'string'
        ) {
          pending.set(item.exportId, item as PendingExport)
        }
      }
    }
  } catch {
    // Ignore invalid or unavailable session data.
  }

  async function check(taskId?: string): Promise<void> {
    if (!active) return
    const projectId = options.currentProjectId()
    await Promise.all(
      [...pending.values()]
        .filter((item) => item.projectId === projectId && (!taskId || item.taskId === taskId))
        .map(async (item) => {
          if (checking.has(item.exportId)) return
          checking.add(item.exportId)
          try {
            const artifact = await options.getExport(item.exportId)
            if (!active || options.currentProjectId() !== projectId || !pending.has(item.exportId))
              return
            if (artifact.project_id !== item.projectId) return
            if (artifact.status === 'completed' && artifact.download_url) {
              pending.delete(item.exportId)
              persist()
              try {
                options.download(artifact)
              } catch {
                pending.set(item.exportId, item)
                persist()
              }
            } else if (['failed', 'cancelled', 'deleted'].includes(artifact.status)) {
              pending.delete(item.exportId)
              persist()
              options.onFailure(
                artifact.error_message ??
                  (artifact.status === 'cancelled' ? '导出已取消' : '项目导出失败'),
              )
            }
          } catch {
            // Retain the export and retry after a transient request failure.
          } finally {
            checking.delete(item.exportId)
          }
        }),
    )
  }

  function track(item: PendingExport): void {
    pending.set(item.exportId, item)
    persist()
    void check(item.taskId)
  }

  function start(): void {
    if (active) return
    active = true
    timer = setInterval(() => void check(), POLL_INTERVAL_MS)
    void check()
  }

  function stop(): void {
    active = false
    if (timer !== null) clearInterval(timer)
    timer = null
  }

  return { track, check, start, stop }
}
