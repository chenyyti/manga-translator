import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useProjectsStore } from './projects'
import type { ProjectSummary } from '@/types'

const project = (id: string): ProjectSummary => ({
  id,
  name: `项目 ${id}`,
  source_language: 'ja',
  target_language: 'zh-CN',
  translation_mode: 'quick',
  ocr_provider: 'mangaocr',
  status: 'ready',
  total_pages: 1,
  cover_page_id: null,
  cover_thumbnail_url: null,
  active_task_id: null,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
})

describe('projects store', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('deduplicates paginated project summaries', () => {
    const store = useProjectsStore()
    store.replace([project('a')], 2)
    store.append([project('a'), project('b')], 2)
    expect(store.items.map((item) => item.id)).toEqual(['a', 'b'])
  })

  it('keeps only lightweight navigation state', () => {
    const store = useProjectsStore()
    store.open('project-id', 19)
    expect(store.currentProjectId).toBe('project-id')
    expect(store.currentPageIndex).toBe(19)
  })
})
