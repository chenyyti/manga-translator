<script setup lang="ts">
import Konva from 'konva'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { DetectionRegion } from '@/types'

const props = defineProps<{
  imageUrl: string
  imageWidth: number
  imageHeight: number
  regions: DetectionRegion[]
  selectedIds: string[]
  drawClassId: number
  drawClassName: string
  mode: 'select' | 'draw'
}>()
const emit = defineEmits<{
  change: [regions: DetectionRegion[]]
  select: [ids: string[]]
}>()

const container = ref<HTMLElement | null>(null)
const stageRef = ref<{ getNode: () => Konva.Stage } | null>(null)
const transformerRef = ref<{ getNode: () => Konva.Transformer } | null>(null)
const stageSize = ref({ width: 900, height: 680 })
const image = ref<HTMLImageElement | null>(null)
const zoom = ref(1)
const position = ref({ x: 0, y: 0 })
const spacePressed = ref(false)
const draft = ref<{ x1: number; y1: number; x2: number; y2: number } | null>(null)
let observer: ResizeObserver | null = null
let drawing = false

const fitScale = computed(() =>
  Math.min(stageSize.value.width / props.imageWidth, stageSize.value.height / props.imageHeight),
)
const scale = computed(() => fitScale.value * zoom.value)
const stageConfig = computed(() => ({
  width: stageSize.value.width,
  height: stageSize.value.height,
  scaleX: scale.value,
  scaleY: scale.value,
  x: position.value.x,
  y: position.value.y,
  draggable: spacePressed.value,
}))
const draftConfig = computed(() =>
  draft.value
    ? {
        x: Math.min(draft.value.x1, draft.value.x2),
        y: Math.min(draft.value.y1, draft.value.y2),
        width: Math.abs(draft.value.x2 - draft.value.x1),
        height: Math.abs(draft.value.y2 - draft.value.y1),
        stroke: '#e8492d',
        strokeWidth: 2 / scale.value,
        dash: [8 / scale.value, 5 / scale.value],
      }
    : null,
)

function cloneRegions(): DetectionRegion[] {
  return props.regions.map((item) => ({ ...item }))
}

function fit(): void {
  zoom.value = 1
  position.value = {
    x: (stageSize.value.width - props.imageWidth * fitScale.value) / 2,
    y: (stageSize.value.height - props.imageHeight * fitScale.value) / 2,
  }
}

function pointerInImage(): { x: number; y: number } | null {
  const stage = stageRef.value?.getNode()
  const pointer = stage?.getPointerPosition()
  if (!stage || !pointer) return null
  return {
    x: Math.max(0, Math.min(props.imageWidth, (pointer.x - stage.x()) / stage.scaleX())),
    y: Math.max(0, Math.min(props.imageHeight, (pointer.y - stage.y()) / stage.scaleY())),
  }
}

function onPointerDown(event: Konva.KonvaEventObject<PointerEvent>): void {
  if (props.mode !== 'draw' || spacePressed.value || event.target !== event.target.getStage())
    return
  const pointer = pointerInImage()
  if (!pointer) return
  drawing = true
  draft.value = { x1: pointer.x, y1: pointer.y, x2: pointer.x, y2: pointer.y }
  emit('select', [])
}

function onPointerMove(): void {
  if (!drawing || !draft.value) return
  const pointer = pointerInImage()
  if (pointer) draft.value = { ...draft.value, x2: pointer.x, y2: pointer.y }
}

function onPointerUp(): void {
  if (!drawing || !draft.value) return
  drawing = false
  const value = draft.value
  draft.value = null
  const x1 = Math.min(value.x1, value.x2)
  const y1 = Math.min(value.y1, value.y2)
  const x2 = Math.max(value.x1, value.x2)
  const y2 = Math.max(value.y1, value.y2)
  if (x2 - x1 < 4 || y2 - y1 < 4) return
  const created: DetectionRegion = {
    id: crypto.randomUUID(),
    class_id: props.drawClassId,
    class_name: props.drawClassName,
    confidence: null,
    x1,
    y1,
    x2,
    y2,
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
  emit('change', [...cloneRegions(), created])
  emit('select', [created.id])
}

function selectRegion(event: Konva.KonvaEventObject<MouseEvent>, id: string): void {
  if (props.mode === 'draw') return
  const additive = event.evt.ctrlKey || event.evt.metaKey || event.evt.shiftKey
  if (!additive) emit('select', [id])
  else if (props.selectedIds.includes(id))
    emit(
      'select',
      props.selectedIds.filter((item) => item !== id),
    )
  else emit('select', [...props.selectedIds, id])
}

function onDragEnd(event: Konva.KonvaEventObject<DragEvent>, id: string): void {
  const node = event.target
  const regions = cloneRegions()
  const region = regions.find((item) => item.id === id)
  if (!region) return
  const width = region.x2 - region.x1
  const height = region.y2 - region.y1
  region.x1 = Math.max(0, Math.min(props.imageWidth - width, node.x()))
  region.y1 = Math.max(0, Math.min(props.imageHeight - height, node.y()))
  region.x2 = region.x1 + width
  region.y2 = region.y1 + height
  region.is_manual_edited = true
  emit('change', regions)
}

function onTransformEnd(event: Konva.KonvaEventObject<Event>, id: string): void {
  const node = event.target
  const regions = cloneRegions()
  const region = regions.find((item) => item.id === id)
  if (!region) return
  const width = Math.max(4, node.width() * node.scaleX())
  const height = Math.max(4, node.height() * node.scaleY())
  node.scaleX(1)
  node.scaleY(1)
  region.x1 = Math.max(0, node.x())
  region.y1 = Math.max(0, node.y())
  region.x2 = Math.min(props.imageWidth, region.x1 + width)
  region.y2 = Math.min(props.imageHeight, region.y1 + height)
  region.is_manual_edited = true
  emit('change', regions)
}

function onWheel(event: Konva.KonvaEventObject<WheelEvent>): void {
  event.evt.preventDefault()
  const stage = stageRef.value?.getNode()
  const pointer = stage?.getPointerPosition()
  if (!stage || !pointer) return
  const oldScale = scale.value
  const point = { x: (pointer.x - stage.x()) / oldScale, y: (pointer.y - stage.y()) / oldScale }
  zoom.value = Math.max(0.5, Math.min(6, zoom.value * (event.evt.deltaY > 0 ? 0.9 : 1.1)))
  const newScale = fitScale.value * zoom.value
  position.value = { x: pointer.x - point.x * newScale, y: pointer.y - point.y * newScale }
}

function onStageDragEnd(event: Konva.KonvaEventObject<DragEvent>): void {
  position.value = { x: event.target.x(), y: event.target.y() }
}

function transformerBoundBox(oldBox: Konva.Box, newBox: Konva.Box): Konva.Box {
  return Math.abs(newBox.width) < 4 || Math.abs(newBox.height) < 4 ? oldBox : newBox
}

function attachTransformer(): void {
  const stage = stageRef.value?.getNode()
  const transformer = transformerRef.value?.getNode()
  if (!stage || !transformer) return
  transformer.nodes(
    props.selectedIds.map((id) => stage.findOne(`#region-${id}`)).filter(Boolean) as Konva.Node[],
  )
  transformer.getLayer()?.batchDraw()
}

function keyDown(event: KeyboardEvent): void {
  if (event.code === 'Space') spacePressed.value = true
}
function keyUp(event: KeyboardEvent): void {
  if (event.code === 'Space') spacePressed.value = false
}

watch(
  () => props.imageUrl,
  (url) => {
    const loaded = new Image()
    loaded.onload = () => {
      image.value = loaded
      fit()
    }
    loaded.src = url
  },
  { immediate: true },
)
watch(
  () => props.selectedIds,
  () => void nextTick(attachTransformer),
  { deep: true },
)
watch(
  () => props.regions,
  () => void nextTick(attachTransformer),
  { deep: true },
)

onMounted(() => {
  observer = new ResizeObserver(([entry]) => {
    stageSize.value = {
      width: Math.max(320, entry.contentRect.width),
      height: Math.max(420, entry.contentRect.height),
    }
    fit()
  })
  if (container.value) observer.observe(container.value)
  window.addEventListener('keydown', keyDown)
  window.addEventListener('keyup', keyUp)
})
onBeforeUnmount(() => {
  observer?.disconnect()
  window.removeEventListener('keydown', keyDown)
  window.removeEventListener('keyup', keyUp)
})

defineExpose({ fit })
</script>

<template>
  <div
    ref="container"
    class="detection-editor"
    :class="{ drawing: mode === 'draw', panning: spacePressed }"
  >
    <v-stage
      ref="stageRef"
      :config="stageConfig"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @wheel="onWheel"
      @dragend="onStageDragEnd"
    >
      <v-layer>
        <v-image :config="{ image, width: imageWidth, height: imageHeight, listening: false }" />
        <v-rect
          v-for="region in regions"
          :key="region.id"
          :config="{
            id: `region-${region.id}`,
            x: region.x1,
            y: region.y1,
            width: region.x2 - region.x1,
            height: region.y2 - region.y1,
            stroke: selectedIds.includes(region.id) ? '#e8492d' : '#18a572',
            strokeWidth: (selectedIds.includes(region.id) ? 3 : 2) / scale,
            fill: selectedIds.includes(region.id) ? 'rgba(232,73,45,.10)' : 'rgba(24,165,114,.06)',
            draggable: mode === 'select' && !spacePressed,
          }"
          @click="selectRegion($event, region.id)"
          @tap="selectRegion($event, region.id)"
          @dragend="onDragEnd($event, region.id)"
          @transformend="onTransformEnd($event, region.id)"
        />
        <v-rect v-if="draftConfig" :config="draftConfig" />
        <v-transformer
          ref="transformerRef"
          :config="{
            rotateEnabled: false,
            borderStroke: '#e8492d',
            anchorStroke: '#e8492d',
            anchorSize: 9 / scale,
            boundBoxFunc: transformerBoundBox,
          }"
        />
      </v-layer>
    </v-stage>
    <span class="zoom-readout">{{ Math.round(zoom * 100) }}%</span>
  </div>
</template>
