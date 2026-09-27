/// <reference types="vite/client" />

declare module 'vue-virtual-scroller' {
  import type { DefineComponent } from 'vue'
  export const RecycleScroller: DefineComponent
}

interface Document {
  readonly modelContext?: {
    registerTool(tool: {
      name: string
      description: string
      inputSchema: Record<string, unknown>
      execute(input: unknown): Promise<unknown> | unknown
    }): void
  }
}
