import { onBeforeUnmount, ref } from 'vue'

import type { TaskSnapshot } from '@/types'

export function useTaskSocket(onSnapshot?: (snapshot: TaskSnapshot) => void) {
  const snapshot = ref<TaskSnapshot | null>(null)
  const connectionError = ref(false)
  let socket: WebSocket | null = null
  let reconnectTimer: number | null = null
  let retries = 0
  let currentTaskId: string | null = null
  let stopped = false

  function connect(taskId: string): void {
    disconnect(false)
    currentTaskId = taskId
    stopped = false
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    socket = new WebSocket(`${protocol}//${window.location.host}/api/ws/tasks/${taskId}`)
    socket.onopen = () => {
      retries = 0
      connectionError.value = false
    }
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data) as { type: string; data?: TaskSnapshot }
      if (message.type !== 'task.snapshot' || !message.data) return
      snapshot.value = message.data
      onSnapshot?.(message.data)
    }
    socket.onerror = () => {
      connectionError.value = true
    }
    socket.onclose = () => {
      socket = null
      const terminal =
        snapshot.value && ['completed', 'failed', 'cancelled'].includes(snapshot.value.status)
      if (stopped || terminal || !currentTaskId) return
      const delay = Math.min(1000 * 2 ** retries, 10_000)
      retries += 1
      reconnectTimer = window.setTimeout(() => currentTaskId && connect(currentTaskId), delay)
    }
  }

  function disconnect(clearTask = true): void {
    stopped = true
    if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
    reconnectTimer = null
    socket?.close()
    socket = null
    if (clearTask) currentTaskId = null
  }

  onBeforeUnmount(() => disconnect())
  return { snapshot, connectionError, connect, disconnect }
}
