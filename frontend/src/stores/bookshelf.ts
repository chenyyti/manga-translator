import { defineStore } from 'pinia'

import type { ProjectSummary } from '@/types'

export const useBookshelfStore = defineStore('bookshelf', {
  state: () => ({
    items: [] as ProjectSummary[],
    total: 0,
    loaded: false,
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
  },
})
