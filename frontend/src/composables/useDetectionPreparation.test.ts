import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { useDetectionPreparation } from './useDetectionPreparation'

vi.mock('@/api/client', () => ({ api: { getStartupState: vi.fn() } }))
afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})

it('polls during preparation, refreshes once and stops when ready', async () => {
  vi.useFakeTimers()
  const refresh = vi.fn().mockResolvedValue(undefined)
  vi.mocked(api.getStartupState).mockResolvedValue({
    core_status: 'ready',
    yolo: { status: 'ready', error: null },
    timings: {},
  })
  const wrapper = mount(
    defineComponent({
      setup() {
        const { preparing, track } = useDetectionPreparation(refresh)
        track({ status: 'preparing' })
        return () => h('span', String(preparing.value))
      },
    }),
  )
  expect(wrapper.text()).toBe('true')
  await vi.advanceTimersByTimeAsync(800)
  expect(wrapper.text()).toBe('false')
  expect(refresh).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(5000)
  expect(api.getStartupState).toHaveBeenCalledTimes(1)
  wrapper.unmount()
})

it('does not poll after leaving the page', async () => {
  vi.useFakeTimers()
  const wrapper = mount(
    defineComponent({
      setup() {
        useDetectionPreparation(vi.fn()).track({ status: 'preparing' })
        return () => h('span')
      },
    }),
  )
  wrapper.unmount()
  await vi.advanceTimersByTimeAsync(5000)
  expect(api.getStartupState).not.toHaveBeenCalled()
})
