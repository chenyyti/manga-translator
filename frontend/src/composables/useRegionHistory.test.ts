import { describe, expect, it } from 'vitest'

import { useRegionHistory } from './useRegionHistory'
import type { DetectionRegion } from '@/types'

function region(id: string, x1: number): DetectionRegion {
  return {
    id,
    class_id: 0,
    class_name: 'text_region',
    confidence: null,
    x1,
    y1: 2,
    x2: x1 + 10,
    y2: 20,
    source: 'manual',
    is_manual_edited: true,
    source_text: null,
    source_text_origin: null,
    ocr_status: 'pending',
    ocr_provider: null,
    ocr_confidence: null,
    ocr_error: null,
    geometry_revision: 0,
    ocr_revision: 0,
    ocr_updated_at: null,
  }
}

describe('region history', () => {
  it('supports bounded undo, redo and reset', () => {
    const history = useRegionHistory(2)
    history.reset([region('one', 1)])
    history.commit([region('one', 3)])
    history.commit([region('one', 5), region('two', 20)])

    expect(history.canUndo.value).toBe(true)
    history.undo()
    expect(history.current.value[0]?.x1).toBe(3)
    history.redo()
    expect(history.current.value).toHaveLength(2)

    history.reset([])
    expect(history.canUndo.value).toBe(false)
    expect(history.canRedo.value).toBe(false)
  })
})
