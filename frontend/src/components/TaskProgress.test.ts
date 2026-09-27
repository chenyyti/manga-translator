import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import TaskProgress from './TaskProgress.vue'
import type { TaskSnapshot } from '@/types'

const task: TaskSnapshot = {
  id: 'task',
  project_id: 'project',
  task_type: 'ocr',
  status: 'running',
  stage: 'recognizing',
  total: 4,
  completed: 1,
  failed: 1,
  skipped: 1,
  current_page_id: 'page',
  cancel_requested: false,
  error_code: null,
  error_message: null,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
}

describe('TaskProgress', () => {
  it('counts skipped OCR regions in progress', () => {
    const wrapper = mount(TaskProgress, {
      props: { task },
      global: {
        stubs: {
          ElProgress: { template: '<div />' },
          ElButton: { template: '<button><slot /></button>' },
        },
      },
    })
    expect(wrapper.text()).toContain('OCR 任务')
    expect(wrapper.text()).toContain('识别原文')
    expect(wrapper.text()).toContain('3 / 4')
    expect(wrapper.text()).toContain('跳过 1')
  })
})
