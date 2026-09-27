import { onBeforeUnmount, ref } from 'vue'
import { api } from '@/api/client'

export function useDetectionPreparation(refresh: () => Promise<void>) {
  const preparing = ref(false)
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false
  function track(state?: { status: string }) {
    if (disposed) return
    clearTimeout(timer)
    preparing.value = state?.status === 'preparing'
    if (preparing.value) timer = setTimeout(() => void poll(), 800)
  }
  async function poll() {
    try {
      const state = await api.getStartupState()
      if (disposed) return
      track(state.yolo)
      if (!preparing.value) await refresh()
    } catch {
      if (!disposed) timer = setTimeout(() => void poll(), 2000)
    }
  }
  onBeforeUnmount(() => {
    disposed = true
    clearTimeout(timer)
  })
  return { preparing, track }
}
