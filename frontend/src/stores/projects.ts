import { defineStore } from 'pinia'

import type { ProjectSummary } from '@/types'

export const useProjectsStore = defineStore('projects', {
  state: () => ({
    items: [] as ProjectSummary[],
    total: 0,
    loaded: false,
    currentProjectId: null as string | null,
    currentPageIndex: 1,
  }),
  actions: {
    replace(items: ProjectSummary[], total: number) {
      this.items = items
      this.total = total
      this.loaded = true
    },
    append(items: ProjectSummary[], total: number) {
      const known = new Set(this.items.map((item) => item.id))
      this.items.push(...items.filter((item) => !known.has(item.id)))
      this.total = total
    },
    remove(projectId: string) {
      this.items = this.items.filter((item) => item.id !== projectId)
      this.total = Math.max(0, this.total - 1)
    },
    open(projectId: string, pageIndex = 1) {
      this.currentProjectId = projectId
      this.currentPageIndex = pageIndex
    },
  },
})
