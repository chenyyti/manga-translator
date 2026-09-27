import { computed, ref } from 'vue'

import type { DetectionRegion } from '@/types'

function copy(value: DetectionRegion[]): DetectionRegion[] {
  return value.map((region) => ({ ...region }))
}

export function useRegionHistory(limit = 50) {
  const current = ref<DetectionRegion[]>([])
  const undoStack = ref<DetectionRegion[][]>([])
  const redoStack = ref<DetectionRegion[][]>([])

  function reset(value: DetectionRegion[]): void {
    current.value = copy(value)
    undoStack.value = []
    redoStack.value = []
  }

  function commit(value: DetectionRegion[]): void {
    undoStack.value.push(copy(current.value))
    if (undoStack.value.length > limit) undoStack.value.shift()
    current.value = copy(value)
    redoStack.value = []
  }

  function undo(): void {
    const previous = undoStack.value.pop()
    if (!previous) return
    redoStack.value.push(copy(current.value))
    current.value = previous
  }

  function redo(): void {
    const next = redoStack.value.pop()
    if (!next) return
    undoStack.value.push(copy(current.value))
    current.value = next
  }

  return {
    current,
    canUndo: computed(() => undoStack.value.length > 0),
    canRedo: computed(() => redoStack.value.length > 0),
    reset,
    commit,
    undo,
    redo,
  }
}
