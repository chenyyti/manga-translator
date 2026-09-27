import { defineComponent, h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'

import { useLauncherHeartbeat } from './useLauncherHeartbeat'

vi.mock('@/api/client', () => ({
  api: {
    getLauncherSession: vi.fn(),
    sendLauncherHeartbeat: vi.fn(),
  },
}))

const Harness = defineComponent({
  setup() {
    useLauncherHeartbeat()
    return () => h('div')
  },
})

describe('useLauncherHeartbeat', () => {
  afterEach(() => {
    vi.clearAllMocks()
    vi.useRealTimers()
  })

  it('sends an initial and periodic heartbeat for a managed session', async () => {
    vi.useFakeTimers()
    vi.mocked(api.getLauncherSession).mockResolvedValue({
      managed: true,
      session_id: 'test-session',
      heartbeat_interval_seconds: 10,
      heartbeat_timeout_seconds: 60,
    })
    vi.mocked(api.sendLauncherHeartbeat).mockResolvedValue({ active: true })

    const wrapper = mount(Harness)
    await flushPromises()
    expect(api.sendLauncherHeartbeat).toHaveBeenCalledTimes(1)
    expect(api.sendLauncherHeartbeat).toHaveBeenCalledWith('test-session')

    await vi.advanceTimersByTimeAsync(10_000)
    expect(api.sendLauncherHeartbeat).toHaveBeenCalledTimes(2)

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(api.sendLauncherHeartbeat).toHaveBeenCalledTimes(2)
  })

  it('does not send heartbeats for an unmanaged manual server', async () => {
    vi.useFakeTimers()
    vi.mocked(api.getLauncherSession).mockResolvedValue({
      managed: false,
      session_id: null,
      heartbeat_interval_seconds: 10,
      heartbeat_timeout_seconds: 60,
    })

    const wrapper = mount(Harness)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(20_000)
    expect(api.sendLauncherHeartbeat).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
