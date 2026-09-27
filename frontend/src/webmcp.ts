import type { Router } from 'vue-router'

export function registerWebMcpTools(router: Router): void {
  if (!document.modelContext) return
  document.modelContext.registerTool({
    name: 'manga_open_new_project',
    description: 'Open the new manga project workflow in this local application.',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    async execute() {
      await router.push({ name: 'new-project' })
      return { opened: true }
    },
  })
  document.modelContext.registerTool({
    name: 'manga_open_project',
    description: 'Open an existing manga translation project by its project id.',
    inputSchema: {
      type: 'object',
      properties: { projectId: { type: 'string', minLength: 1 } },
      required: ['projectId'],
      additionalProperties: false,
    },
    async execute(input: unknown) {
      const projectId = (input as { projectId?: unknown })?.projectId
      if (typeof projectId !== 'string' || !projectId) throw new Error('projectId is required')
      await router.push({ name: 'project', params: { id: projectId } })
      return { opened: projectId }
    },
  })
}
