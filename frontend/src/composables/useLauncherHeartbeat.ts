import { onBeforeUnmount, onMounted } from 'vue'

import { api } from '@/api/client'

const DISCOVERY_INTERVAL_MS = 5_000

export function useLauncherHeartbeat() {
  let mounted = false
  let sessionId: string | null = null
  let discoveryTimer: number | null = null
  let heartbeatTimer: number | null = null
  let discovering = false

  const stopDiscovery = () => {
    if (discoveryTimer !== null) {
      window.clearInterval(discoveryTimer)
      discoveryTimer = null
    }
  }

  const sendHeartbeat = async () => {
    if (!mounted || !sessionId) return
    try {
      await api.sendLauncherHeartbeat(sessionId)
    } catch {
      // The launcher treats missed heartbeats as a normal disconnect signal.
    }
  }

  const discoverSession = async () => {
    if (!mounted || sessionId || discovering) return
    discovering = true
    try {
      const launcher = await api.getLauncherSession()
      if (!launcher.managed || !launcher.session_id) {
        stopDiscovery()
        return
      }
      sessionId = launcher.session_id
      stopDiscovery()
      await sendHeartbeat()
      if (mounted && sessionId) {
        heartbeatTimer = window.setInterval(
          () => void sendHeartbeat(),
          Math.max(1, launcher.heartbeat_interval_seconds) * 1_000,
        )
      }
    } catch {
      // Retry discovery while the local backend is still coming up.
    } finally {
      discovering = false
    }
  }

  const sendHeartbeatWhenVisible = () => {
    if (document.visibilityState === 'visible') void sendHeartbeat()
  }

  onMounted(() => {
    mounted = true
    void discoverSession()
    discoveryTimer = window.setInterval(() => void discoverSession(), DISCOVERY_INTERVAL_MS)
    document.addEventListener('visibilitychange', sendHeartbeatWhenVisible)
  })

  onBeforeUnmount(() => {
    mounted = false
    stopDiscovery()
    if (heartbeatTimer !== null) {
      window.clearInterval(heartbeatTimer)
      heartbeatTimer = null
    }
    document.removeEventListener('visibilitychange', sendHeartbeatWhenVisible)
  })
}
