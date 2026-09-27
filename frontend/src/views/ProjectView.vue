<script setup lang="ts">
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Delete,
  EditPen,
  Hide,
  Picture,
  RefreshRight,
  View,
  Warning,
  ZoomIn,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'

import { api } from '@/api/client'
import { useDetectionPreparation } from '@/composables/useDetectionPreparation'
import DetectionEditor from '@/components/DetectionEditor.vue'
import StatusPill from '@/components/StatusPill.vue'
import TaskProgress from '@/components/TaskProgress.vue'
import { BlobLruCache } from '@/composables/useBlobLru'
import { downloadExportInBrowser, useExportAutoDownload } from '@/composables/useExportAutoDownload'
import { useRegionHistory } from '@/composables/useRegionHistory'
import { useTaskSocket } from '@/composables/useTaskSocket'
import { useProjectsStore } from '@/stores/projects'
import type {
  DetectionModel,
  DetectionRegion,
  DetectionSettings,
  OCRProviderId,
  OCRProviderInfo,
  LLMProfile,
  TranslationSettings,
  PageDetail,
  PageSummary,
  ProjectDetail,
  TaskSnapshot,
  BatchTaskPreview,
  BatchTaskPreviewRequest,
  RenderState,
  ExportArtifact,
  ExportFormat,
  HealthReport,
} from '@/types'

interface PageSlot {
  key: string
  index: number
  page: PageSummary | null
}
const route = useRoute()
const router = useRouter()
const store = useProjectsStore()
const projectId = computed(() => String(route.params.id))
const project = ref<ProjectDetail | null>(null)
const loading = ref(true)
const loadError = ref('')
const slots = ref<PageSlot[]>([])
const loadedChunks = new Set<number>()
const pendingChunks = new Map<number, Promise<void>>()
const currentIndex = ref(Math.max(1, store.currentPageIndex))
const navigationIndex = ref(currentIndex.value)
const currentDetail = ref<PageDetail | null>(null)
const previewUrl = ref<string | null>(null)
const previewLoading = ref(false)
const previewError = ref('')
const renderState = ref<RenderState | null>(null)
const renderedPreviewUrl = ref<string | null>(null)
const renderedPreviewLoading = ref(false)
const renderedPreviewError = ref('')
const maskPreviewUrl = ref<string | null>(null)
const maskPreviewVisible = ref(false)
const maskPreviewLoading = ref(false)
let maskPreviewRequest = 0
const blobCache = new BlobLruCache(8)
let previewRequest = 0
let renderedPreviewRequest = 0
const models = ref<DetectionModel[]>([])
const detectionSettings = ref<DetectionSettings | null>(null)
const ocrProviders = ref<OCRProviderInfo[]>([])
const llmProfiles = ref<LLMProfile[]>([])
const translationSettings = ref<TranslationSettings | null>(null)
const editor = ref<InstanceType<typeof DetectionEditor> | null>(null)
const thumbnailRail = ref<HTMLElement | null>(null)
const mode = ref<'select' | 'draw'>('select')
const mobilePane = ref<'detection' | 'final'>('detection')
const mobileProjectActionsOpen = ref(false)
const mobileEditToolsOpen = ref(false)
const mobileThumbnailsOpen = ref(false)
const selectedIds = ref<string[]>([])
const boxesVisible = ref(true)
const history = useRegionHistory()
const revision = ref(0)
const dirty = ref(false)
const saveState = ref<'saved' | 'dirty' | 'saving' | 'error'>('saved')
let resetRegions = false
let autosaveTimer: ReturnType<typeof setTimeout> | null = null
const batchDetectionVisible = ref(false)
const batchProcessingVisible = ref(false)
const batchOverwriteModel = ref(false)
const batchOverwriteManualOCR = ref(false)
const batchOverwriteManualTranslation = ref(false)
const batchPreview = ref<BatchTaskPreview | null>(null)
const loadingBatchPreview = ref(false)
const startingBatch = ref(false)
const batchStart = ref(1)
const batchEnd = ref(1)
const batchDetectionOverwrite = ref(false)
const startingDetection = ref(false)
const startingOCR = ref(false)
const startingTranslation = ref(false)
const startingGeneration = ref(false)
const settingCover = ref(false)
const savingTranslationText = ref(false)
const changingLLMProfile = ref(false)
const savingOCRText = ref(false)
const healthVisible = ref(false)
const healthLoading = ref(false)
const healthDeep = ref(false)
const healthReport = ref<HealthReport | null>(null)
const healthIssueOffset = ref(0)
const healthIssuePage = ref(1)
const healthIssueLimit = 20
const exportVisible = ref(false)
const exportLoading = ref(false)
const exports = ref<ExportArtifact[]>([])
const exportFormat = ref<ExportFormat>('zip')
const creatingExport = ref(false)
const refreshingAfterTask = ref(false)
const exportDownloads = useExportAutoDownload({
  currentProjectId: () => projectId.value,
  getExport: (exportId) => api.getExport(exportId),
  download: (artifact) => {
    downloadExportInBrowser(artifact)
    ElMessage.success(`${artifact.format.toUpperCase()} 已生成，浏览器开始下载`)
  },
  onFailure: (message) => ElMessage.error(message),
})
const ocrDraft = ref('')
const translationDraft = ref('')
const currentSlot = computed(() => slots.value[currentIndex.value - 1] ?? null)
const currentPage = computed(() => currentSlot.value?.page ?? null)
const totalPages = computed(() => project.value?.total_pages ?? 0)
const visibleHealthIssues = computed(
  () =>
    healthReport.value?.checks.slice(
      healthIssueOffset.value,
      healthIssueOffset.value + healthIssueLimit,
    ) ?? [],
)
const selectedRegions = computed(() =>
  history.current.value.filter((item) => selectedIds.value.includes(item.id)),
)
const selectedModel = computed(
  () => models.value.find((item) => item.id === detectionSettings.value?.default_model_id) ?? null,
)
const classEntries = computed(() => Object.entries(selectedModel.value?.class_names ?? {}))
const drawClass = computed(
  () =>
    classEntries.value.find(([, name]) =>
      ['text', 'textregion', 'textbox', '文字', '文字框', '文本', '文本框'].includes(
        name.toLocaleLowerCase().replace(/[\s_-]/g, ''),
      ),
    ) ??
    classEntries.value[0] ?? ['0', 'text_region'],
)
const selectedRegion = computed(() =>
  selectedRegions.value.length === 1 ? selectedRegions.value[0]! : null,
)
const detectionPreviewReady = computed(
  () =>
    ['detected', 'reviewed'].includes(currentPage.value?.detection_status ?? '') ||
    history.current.value.some((region) => region.source === 'manual'),
)
const detectionCanvasStyle = computed(() => {
  const width = currentDetail.value?.width
  const height = currentDetail.value?.height
  return width && height ? { aspectRatio: String(width) + ' / ' + String(height) } : undefined
})
const renderedPreviewVisible = computed(
  () => renderState.value?.render_status === 'completed' && !!renderedPreviewUrl.value,
)
const resolvedOCRProviderId = computed<Exclude<OCRProviderId, 'auto'>>(() =>
  project.value?.source_language === 'ja' ? 'mangaocr' : 'paddleocr',
)
const resolvedOCRProvider = computed(() =>
  ocrProviders.value.find((item) => item.id === resolvedOCRProviderId.value),
)
const ocrReady = computed(
  () => !!resolvedOCRProvider.value?.installed && !!resolvedOCRProvider.value?.model_ready,
)
const ocrStatusLabels: Record<string, string> = {
  pending: '待识别',
  processing: '识别中',
  completed: '已识别',
  completed_with_warnings: '完成（有异常）',
  manual: '人工校对',
  failed: '识别失败',
  outdated: 'OCR 已过期',
}
const translationStatusLabels: Record<string, string> = {
  pending: '待翻译',
  processing: '翻译中',
  completed: '已翻译',
  completed_with_warnings: '完成（有异常）',
  manual: '人工译文',
  failed: '翻译失败',
  outdated: '译文已过期',
}
const renderStatusLabels: Record<string, string> = {
  pending: '待生成',
  processing: '生成中',
  completed: '已生成',
  failed: '生成失败',
  outdated: '成品已过期',
}
const selectedProfile = computed(() =>
  llmProfiles.value.find(
    (profile) =>
      profile.id ===
      (project.value?.llm_profile_id ?? translationSettings.value?.default_profile_id),
  ),
)
const sfxOnlyPreserve = computed(
  () =>
    !!history.current.value.length &&
    history.current.value.every(
      (region) =>
        translationSettings.value?.sfx_strategy === 'preserve' &&
        translationSettings.value.sfx_class_names.some(
          (name) => name.toLowerCase() === region.class_name.toLowerCase(),
        ),
    ),
)
const translationReady = computed(
  () =>
    ['detected', 'reviewed'].includes(currentPage.value?.detection_status ?? '') &&
    !!history.current.value.length &&
    history.current.value.every((region) => ['completed', 'manual'].includes(region.ocr_status)) &&
    (sfxOnlyPreserve.value || !!selectedProfile.value?.has_api_key),
)
const renderReady = computed(
  () =>
    ['detected', 'reviewed'].includes(currentPage.value?.detection_status ?? '') &&
    history.current.value.every(
      (region) =>
        region.target_text_origin === 'sfx_preserve' ||
        (region.sfx_strategy === 'preserve' &&
          (region.target_text === null || region.target_text === region.source_text)) ||
        (['completed', 'manual'].includes(region.translation_status ?? 'pending') &&
          !!(region.target_text ?? '').trim()),
    ),
)
const generationReady = computed(() => {
  const selected = selectedRegion.value
  if (!selected || translationDraft.value === (selected.target_text ?? '')) {
    return renderReady.value
  }
  if (!translationDraft.value.trim()) return false
  return (
    ['detected', 'reviewed'].includes(currentPage.value?.detection_status ?? '') &&
    history.current.value.every((region) => {
      if (region.id === selected.id) return true
      return (
        region.target_text_origin === 'sfx_preserve' ||
        (region.sfx_strategy === 'preserve' &&
          (region.target_text === null || region.target_text === region.source_text)) ||
        (['completed', 'manual'].includes(region.translation_status ?? 'pending') &&
          !!(region.target_text ?? '').trim())
      )
    })
  )
})
const coverReady = computed(() => {
  const page = currentPage.value
  return !!page && page.render_status === 'completed' && !!page.rendered_thumbnail_url
})
const selectedTranslationReady = computed(
  () =>
    ['detected', 'reviewed'].includes(currentPage.value?.detection_status ?? '') &&
    !!selectedRegion.value &&
    ['completed', 'manual'].includes(selectedRegion.value.ocr_status) &&
    (translationSettings.value?.sfx_strategy === 'preserve' &&
    translationSettings.value.sfx_class_names.some(
      (name) => name.toLowerCase() === selectedRegion.value!.class_name.toLowerCase(),
    )
      ? true
      : !!selectedProfile.value?.has_api_key),
)
const {
  snapshot: taskSnapshot,
  connectionError,
  connect,
  disconnect,
} = useTaskSocket((snapshot) => {
  void onTaskSnapshot(snapshot)
})
const activeTask = computed(
  () =>
    taskSnapshot.value &&
    ['pending', 'running', 'pausing', 'paused'].includes(taskSnapshot.value.status),
)
const taskBusy = computed(() => !!activeTask.value || refreshingAfterTask.value)
const pipelineStage = computed<'detection' | 'ocr' | 'translation' | 'render' | 'complete'>(() => {
  if (!detectionPreviewReady.value) return 'detection'
  if (history.current.value.some((region) => !['completed', 'manual'].includes(region.ocr_status)))
    return 'ocr'
  if (!renderReady.value) return 'translation'
  if (currentPage.value?.render_status !== 'completed') return 'render'
  return 'complete'
})
const renderedPreviewStatusText = computed(() => {
  if (renderedPreviewLoading.value) return '正在载入成品图'
  if (renderState.value?.render_status === 'outdated') return '成品已过期，请重新生成'
  if (renderState.value?.render_status === 'failed' || renderedPreviewError.value)
    return '最终成图失败'
  if (renderState.value?.render_error && renderState.value.render_status !== 'completed')
    return '最终成图失败'
  if (
    activeTask.value &&
    ['page_render', 'page_repair', 'batch_pipeline'].includes(taskSnapshot.value?.task_type ?? '')
  ) {
    return '最终成图生成中'
  }
  if (
    activeTask.value &&
    ['ocr', 'quick_translation'].includes(taskSnapshot.value?.task_type ?? '')
  ) {
    return '翻译处理中，完成生成后显示成品图'
  }
  if (renderState.value?.render_status === 'processing') return '最终成图生成中'
  if (renderState.value?.render_status === 'failed') return '最终成图失败'
  return '翻译并生成后显示成品图'
})

function resetSlots(total: number): void {
  const previous = slots.value
  slots.value = Array.from({ length: total }, (_, index) => ({
    key: `page-${index + 1}`,
    index: index + 1,
    page: previous[index]?.page ?? null,
  }))
  if (total && currentIndex.value > total) currentIndex.value = total
}
async function loadProject(): Promise<ProjectDetail> {
  project.value = await api.getProject(projectId.value)
  resetSlots(project.value.total_pages)
  store.open(projectId.value, currentIndex.value)
  return project.value
}
async function ensureChunk(pageIndex: number): Promise<void> {
  if (pageIndex < 1 || !totalPages.value) return
  const offset = Math.floor((pageIndex - 1) / 50) * 50
  if (loadedChunks.has(offset)) return
  const existing = pendingChunks.get(offset)
  if (existing) return existing
  const requestedProjectId = projectId.value
  const operation = api
    .listPages(requestedProjectId, offset, 50)
    .then((result) => {
      if (projectId.value !== requestedProjectId) return
      if (result.total !== totalPages.value) {
        if (project.value) project.value.total_pages = result.total
        resetSlots(result.total)
      }
      for (const page of result.items) {
        const slot = slots.value[page.page_index - 1]
        if (slot) slot.page = page
      }
      loadedChunks.add(offset)
    })
    .finally(() => {
      if (pendingChunks.get(offset) === operation) pendingChunks.delete(offset)
    })
  pendingChunks.set(offset, operation)
  return operation
}

async function preloadThumbnailChunks(requestedProjectId: string, total: number): Promise<void> {
  const chunkStarts = Array.from(
    { length: Math.ceil(total / 50) },
    (_, chunkIndex) => chunkIndex * 50 + 1,
  )
  for (let index = 0; index < chunkStarts.length; index += 3) {
    if (projectId.value !== requestedProjectId) return
    await Promise.all(
      chunkStarts
        .slice(index, index + 3)
        .map((pageIndex) => ensureChunk(pageIndex).catch(() => undefined)),
    )
    await nextTick()
  }
}

function clearRenderedPreview(): void {
  maskPreviewRequest += 1
  maskPreviewVisible.value = false
  maskPreviewLoading.value = false
  if (maskPreviewUrl.value) URL.revokeObjectURL(maskPreviewUrl.value)
  maskPreviewUrl.value = null
  renderedPreviewRequest += 1
  renderedPreviewUrl.value = null
  renderedPreviewLoading.value = false
  renderedPreviewError.value = ''
  renderState.value = null
}

async function toggleMaskPreview(): Promise<void> {
  if (maskPreviewVisible.value) {
    maskPreviewVisible.value = false
    return
  }
  if (!renderState.value?.mask_url || renderState.value.repair_status !== 'completed') return
  const token = ++maskPreviewRequest
  maskPreviewLoading.value = true
  try {
    const blob = await api.getPreviewBlob(renderState.value.mask_url)
    if (token !== maskPreviewRequest) return
    if (maskPreviewUrl.value) URL.revokeObjectURL(maskPreviewUrl.value)
    maskPreviewUrl.value = URL.createObjectURL(blob)
    maskPreviewVisible.value = true
  } catch (error) {
    if (token === maskPreviewRequest)
      ElMessage.error(error instanceof Error ? error.message : '掩膜加载失败')
  } finally {
    if (token === maskPreviewRequest) maskPreviewLoading.value = false
  }
}

onBeforeUnmount(() => {
  maskPreviewRequest += 1
  if (maskPreviewUrl.value) URL.revokeObjectURL(maskPreviewUrl.value)
})

async function loadRenderedPreview(pageId: string, pageRequest: number): Promise<void> {
  const requestToken = ++renderedPreviewRequest
  renderedPreviewLoading.value = true
  try {
    const state = await api.getPageRenderState(pageId)
    if (pageRequest !== previewRequest || requestToken !== renderedPreviewRequest) return
    renderState.value = state
    if (state.render_status !== 'completed' || !state.rendered_url) return
    const cacheKey = [
      'rendered',
      pageId,
      state.render_revision,
      state.rendered_sha256 ?? 'unknown',
    ].join(':')
    const url = await blobCache.get(cacheKey, () => api.getPreviewBlob(state.rendered_url!))
    if (pageRequest !== previewRequest || requestToken !== renderedPreviewRequest) return
    renderedPreviewUrl.value = url
  } catch (error) {
    if (pageRequest === previewRequest && requestToken === renderedPreviewRequest) {
      renderedPreviewError.value = error instanceof Error ? error.message : '成品图加载失败'
    }
  } finally {
    if (pageRequest === previewRequest && requestToken === renderedPreviewRequest) {
      renderedPreviewLoading.value = false
    }
  }
}

async function revealCurrentThumbnail(): Promise<void> {
  await nextTick()
  const rail = thumbnailRail.value
  const item = rail?.querySelector<HTMLElement>(`[data-page-index="${currentIndex.value}"]`)
  if (!rail || !item) return
  const itemLeft = item.offsetLeft
  const itemRight = itemLeft + item.offsetWidth
  const visibleLeft = rail.scrollLeft
  const visibleRight = visibleLeft + rail.clientWidth
  if (itemLeft < visibleLeft) {
    rail.scrollTo({ left: itemLeft, behavior: 'smooth' })
  } else if (itemRight > visibleRight) {
    rail.scrollTo({ left: itemRight - rail.clientWidth, behavior: 'smooth' })
  }
}

async function loadPage(index: number): Promise<void> {
  if (!totalPages.value) return
  const clamped = Math.min(Math.max(Math.round(index), 1), totalPages.value)
  if (dirty.value && currentDetail.value) await saveRegions()
  if (saveState.value === 'error') {
    navigationIndex.value = currentIndex.value
    return
  }
  currentIndex.value = clamped
  navigationIndex.value = clamped
  store.open(projectId.value, clamped)
  void revealCurrentThumbnail()
  await ensureChunk(clamped)
  const page = slots.value[clamped - 1]?.page
  const requestId = ++previewRequest
  previewUrl.value = null
  previewLoading.value = false
  currentDetail.value = null
  previewError.value = ''
  clearRenderedPreview()
  selectedIds.value = []
  resetRegions = true
  history.reset([])
  await nextTick()
  resetRegions = false
  dirty.value = false
  saveState.value = 'saved'
  if (!page?.preview_url) {
    return
  }
  previewLoading.value = true
  try {
    const [url, detail, document] = await Promise.all([
      blobCache.get(page.id, () => api.getPreviewBlob(page.preview_url!)),
      api.getPage(page.id),
      api.getPageRegions(page.id),
    ])
    if (requestId !== previewRequest) return
    previewUrl.value = url
    currentDetail.value = detail
    revision.value = document.revision
    resetRegions = true
    history.reset(document.regions)
    dirty.value = false
    saveState.value = 'saved'
    await nextTick()
    resetRegions = false
  } catch (error) {
    if (requestId === previewRequest) {
      previewUrl.value = null
      previewError.value = error instanceof Error ? error.message : '预览加载失败'
    }
  } finally {
    if (requestId === previewRequest) previewLoading.value = false
  }
  if (requestId !== previewRequest) return
  await loadRenderedPreview(page.id, requestId)
  if (requestId !== previewRequest) return
  for (const neighbor of [-2, -1, 1, 2]
    .map((delta) => clamped + delta)
    .filter((value) => value >= 1 && value <= totalPages.value)) {
    await ensureChunk(neighbor)
    const neighborPage = slots.value[neighbor - 1]?.page
    if (neighborPage?.preview_url)
      void blobCache
        .get(neighborPage.id, () => api.getPreviewBlob(neighborPage.preview_url!))
        .catch(() => undefined)
  }
}

function syncCurrentPage(page: PageDetail): void {
  currentDetail.value = page
  if (currentSlot.value) currentSlot.value.page = page
}

function onEditorChange(regions: DetectionRegion[]): void {
  clearRenderedPreview()
  const previous = new Map(history.current.value.map((item) => [item.id, item]))
  history.commit(
    regions.map((item) => {
      const old = previous.get(item.id)
      const geometryChanged =
        old &&
        [old.x1, old.y1, old.x2, old.y2].some(
          (value, index) => value !== [item.x1, item.y1, item.x2, item.y2][index],
        )
      return geometryChanged
        ? {
            ...item,
            ocr_status: old.source_text !== null ? 'outdated' : 'pending',
            ocr_error: null,
            translation_status: old.target_text !== null ? 'outdated' : 'pending',
            translation_error: null,
          }
        : item
    }),
  )
}
function changeSelectedClass(className: string): void {
  clearRenderedPreview()
  const regions = history.current.value.map((item) =>
    item.id === selectedIds.value[0]
      ? { ...item, class_name: className, is_manual_edited: true }
      : item,
  )
  history.commit(regions)
}
function deleteSelected(): void {
  if (!selectedIds.value.length) return
  clearRenderedPreview()
  history.commit(history.current.value.filter((item) => !selectedIds.value.includes(item.id)))
  selectedIds.value = []
}
async function saveRegions(): Promise<void> {
  if (!currentDetail.value || !dirty.value || saveState.value === 'saving') return
  saveState.value = 'saving'
  try {
    const result = await api.updatePageRegions(
      currentDetail.value.id,
      revision.value,
      history.current.value,
    )
    revision.value = result.revision
    resetRegions = true
    history.reset(result.regions)
    await nextTick()
    resetRegions = false
    dirty.value = false
    saveState.value = 'saved'
    if (currentPage.value) {
      currentPage.value.detection_status = result.detection_status
      currentPage.value.ocr_status = result.ocr_status
      currentPage.value.translation_status = result.translation_status ?? 'pending'
    }
  } catch (error) {
    saveState.value = 'error'
    ElMessage.error(error instanceof Error ? error.message : '标注保存失败')
  }
}

async function changeLLMProfile(value: string): Promise<void> {
  if (!project.value) return
  changingLLMProfile.value = true
  try {
    const result = await api.updateProjectLLMProfile(project.value.id, value || null)
    project.value.llm_profile_id = result.llm_profile_id
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '翻译 Profile 更新失败')
  } finally {
    changingLLMProfile.value = false
  }
}

async function startPageOCR(): Promise<void> {
  if (!currentDetail.value) return
  await saveRegions()
  if (!['detected', 'reviewed'].includes(currentPage.value?.detection_status ?? '')) {
    ElMessage.warning('请先完成本页检测')
    return
  }
  if (!ocrReady.value) {
    ElMessage.warning('OCR 依赖或模型未就绪，请前往设置查看准备步骤')
    return
  }
  clearRenderedPreview()
  startingOCR.value = true
  try {
    const result = await api.createPageOCRTask(currentDetail.value.id)
    connect(result.task_id)
    ElMessage.success(`已使用 ${resolvedOCRProvider.value?.name} 创建当前页 OCR 任务`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法创建 OCR 任务')
  } finally {
    startingOCR.value = false
  }
}

async function saveOCRText(): Promise<void> {
  if (!selectedRegion.value) return
  clearRenderedPreview()
  savingOCRText.value = true
  try {
    const saved = await api.updateRegionOCRText(
      selectedRegion.value.id,
      ocrDraft.value,
      selectedRegion.value.ocr_revision,
    )
    resetRegions = true
    history.reset(history.current.value.map((item) => (item.id === saved.id ? saved : item)))
    await nextTick()
    resetRegions = false
    if (currentPage.value) {
      const updated = await api.getPage(currentPage.value.id)
      syncCurrentPage(updated)
    }
    ElMessage.success('人工原文已保存')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '人工原文保存失败')
  } finally {
    savingOCRText.value = false
  }
}

async function retryRegionOCR(): Promise<void> {
  const region = selectedRegion.value
  if (!region) return
  if (dirty.value) await saveRegions()
  if (!['detected', 'reviewed'].includes(currentPage.value?.detection_status ?? '')) {
    ElMessage.warning('请先完成本页检测')
    return
  }
  let overwriteManual = false
  if (region.source_text_origin === 'manual') {
    try {
      await ElMessageBox.confirm('重新识别会覆盖人工校对的原文。', '确认重新 OCR', {
        confirmButtonText: '覆盖并识别',
        cancelButtonText: '取消',
        type: 'warning',
      })
      overwriteManual = true
    } catch {
      return
    }
  }
  clearRenderedPreview()
  startingOCR.value = true
  try {
    const result = await api.createRegionOCRTask(region.id, {
      overwrite_manual: overwriteManual,
      overwrite_completed: true,
      expected_ocr_revision: region.ocr_revision,
    })
    connect(result.task_id)
    ElMessage.success('单区域 OCR 任务已创建')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法重新识别区域')
  } finally {
    startingOCR.value = false
  }
}

async function startPageTranslation(): Promise<void> {
  if (!currentDetail.value) return
  await saveRegions()
  if (!translationReady.value) {
    ElMessage.warning('请先完成检测和当前页 OCR，并配置可用翻译 Profile')
    return
  }
  clearRenderedPreview()
  startingTranslation.value = true
  try {
    const result = await api.createPageTranslationTask(currentDetail.value.id, {
      profile_id: project.value?.llm_profile_id,
    })
    connect(result.task_id)
    ElMessage.success('当前页翻译任务已创建（仅发送文本）')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法创建翻译任务')
  } finally {
    startingTranslation.value = false
  }
}

async function saveTranslationText(): Promise<boolean> {
  if (!selectedRegion.value) return true
  clearRenderedPreview()
  savingTranslationText.value = true
  try {
    const saved = await api.updateRegionTranslationText(
      selectedRegion.value.id,
      translationDraft.value,
      selectedRegion.value.translation_revision ?? 0,
    )
    resetRegions = true
    history.reset(history.current.value.map((item) => (item.id === saved.id ? saved : item)))
    await nextTick()
    resetRegions = false
    if (currentPage.value) {
      const updated = await api.getPage(currentPage.value.id)
      syncCurrentPage(updated)
    }
    ElMessage.success('人工译文已保存')
    return true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '人工译文保存失败')
    return false
  } finally {
    savingTranslationText.value = false
  }
}

async function retryRegionTranslation(): Promise<void> {
  const region = selectedRegion.value
  if (!region) return
  if (!selectedTranslationReady.value) {
    ElMessage.warning('请先完成页面检测和该区域 OCR，并配置可用翻译 Profile')
    return
  }
  let overwriteManual = false
  if (region.target_text_origin === 'manual') {
    try {
      await ElMessageBox.confirm('重新翻译会覆盖人工译文。', '确认重新翻译', {
        confirmButtonText: '覆盖并翻译',
        cancelButtonText: '取消',
        type: 'warning',
      })
      overwriteManual = true
    } catch {
      return
    }
  }
  clearRenderedPreview()
  startingTranslation.value = true
  try {
    const result = await api.createRegionTranslationTask(region.id, {
      profile_id: project.value?.llm_profile_id,
      overwrite_completed: true,
      overwrite_manual: overwriteManual,
      expected_translation_revision: region.translation_revision ?? 0,
    })
    connect(result.task_id)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法重新翻译区域')
  } finally {
    startingTranslation.value = false
  }
}

async function startPageGeneration(): Promise<void> {
  if (!currentDetail.value) return
  await saveRegions()
  if (saveState.value === 'error') return
  if (!generationReady.value) {
    ElMessage.warning('请先完成检测，并为所有非保留区域准备有效译文')
    return
  }
  const selected = selectedRegion.value
  if (selected && translationDraft.value !== (selected.target_text ?? '')) {
    if (!(await saveTranslationText())) return
  }
  if (!renderReady.value) {
    ElMessage.warning('译文保存后仍有区域没有可用译文')
    return
  }
  clearRenderedPreview()
  startingGeneration.value = true
  try {
    const result = await api.createPageRenderTask(currentDetail.value.id, { force_repair: true })
    connect(result.task_id)
    ElMessage.success('已开始生成：图片修复完成后自动写回译文并生成最终图片')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法生成当前页成品')
  } finally {
    startingGeneration.value = false
  }
}

async function setCurrentCover(): Promise<void> {
  await setCoverForPage(currentPage.value)
}

async function setCoverForPage(page: PageSummary | null): Promise<void> {
  if (
    !project.value ||
    !page ||
    page.render_status !== 'completed' ||
    !page.rendered_thumbnail_url
  ) {
    ElMessage.warning('只有最新成品页才能设置为封面')
    return
  }
  settingCover.value = true
  try {
    await api.updateProjectCover(project.value.id, page.id)
    project.value.cover_page_id = page.id
    project.value.cover_thumbnail_url = page.rendered_thumbnail_url
    ElMessage.success(`已将第 ${page.page_index} 页设为封面`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '设置封面失败')
  } finally {
    settingCover.value = false
  }
}

async function loadHealth(deep = false): Promise<void> {
  healthLoading.value = true
  healthIssueOffset.value = 0
  healthIssuePage.value = 1
  try {
    healthReport.value = await api.getProjectHealth(projectId.value, deep)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '健康检查失败')
  } finally {
    healthLoading.value = false
  }
}

async function openHealth(): Promise<void> {
  healthVisible.value = true
  healthIssueOffset.value = 0
  healthIssuePage.value = 1
  await loadHealth(healthDeep.value)
}

function handleHealthIssuePageChange(page: number): void {
  healthIssuePage.value = page
  healthIssueOffset.value = (page - 1) * healthIssueLimit
}

async function openExport(): Promise<void> {
  exportVisible.value = true
  exportLoading.value = true
  await Promise.all([
    loadHealth(false),
    api
      .listProjectExports(projectId.value)
      .then((value) => {
        exports.value = value.items
      })
      .catch((error) => {
        ElMessage.error(error instanceof Error ? error.message : '导出历史加载失败')
      }),
  ])
  exportLoading.value = false
}

function downloadCurrentPage(format: 'png' | 'jpg'): void {
  if (!currentPage.value) return
  window.location.assign(api.pageExportUrl(currentPage.value.id, format))
}

async function createProjectExport(format: ExportFormat): Promise<void> {
  if (!healthReport.value) await loadHealth(false)
  if (!healthReport.value?.exportable[format]) {
    ElMessage.warning('项目存在缺失、损坏或过期成品页，暂不能归档导出')
    return
  }
  creatingExport.value = true
  try {
    const result = await api.createProjectExport(projectId.value, format)
    exportDownloads.track({
      projectId: projectId.value,
      exportId: result.export_id,
      taskId: result.task_id,
    })
    connect(result.task_id)
    await api.listProjectExports(projectId.value).then((value) => {
      exports.value = value.items
    })
    ElMessage.success(`${format.toUpperCase()} 导出任务已加入任务中心`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法创建导出任务')
  } finally {
    creatingExport.value = false
  }
}

function formatExportSize(size: number): string {
  if (!size) return '—'
  if (size < 1024 * 1024) return `${Math.ceil(size / 1024)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

function downloadProjectExport(item: ExportArtifact): void {
  if (item.status === 'completed') window.location.assign(api.exportDownloadUrl(item.id))
}

async function removeExport(item: ExportArtifact): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除导出文件“${item.filename}”吗？`, '删除导出', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
    await api.deleteExport(item.id)
    exports.value = exports.value.filter((candidate) => candidate.id !== item.id)
    ElMessage.success('导出记录已删除')
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error instanceof Error ? error.message : '删除导出失败')
  }
}

async function openHealthIssue(issue: { page_index: number | null }): Promise<void> {
  healthVisible.value = false
  if (issue.page_index) await loadPage(issue.page_index)
}

function undo(): void {
  clearRenderedPreview()
  history.undo()
  selectedIds.value = []
}
function redo(): void {
  clearRenderedPreview()
  history.redo()
  selectedIds.value = []
}

async function saveAndNext(): Promise<void> {
  if (!currentDetail.value) return
  await saveRegions()
  if (saveState.value === 'error') return
  try {
    const next = slots.value.find((slot) => slot.index > currentIndex.value)
    await loadPage(next?.index ?? Math.min(currentIndex.value + 1, totalPages.value))
    ElMessage.success('本页已保存')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存失败')
  }
}

async function startDetection(start: number, end: number, forceOverwrite = false): Promise<void> {
  if (!detectionSettings.value?.default_model_id) {
    ElMessage.warning('请将 YOLO .pt 模型放入项目 models/yolo/ 目录并选择模型')
    return
  }
  if (selectedModel.value?.status !== 'ready') {
    ElMessage.warning(
      selectedModel.value?.status === 'preparing'
        ? '检测模型准备中，请稍候'
        : selectedModel.value?.error_message || '检测模型不可用',
    )
    return
  }
  startingDetection.value = true
  try {
    await saveRegions()
    const result = await api.createDetectionTask(projectId.value, {
      model_id: detectionSettings.value.default_model_id,
      start_page: start,
      end_page: end,
      confidence: detectionSettings.value.confidence,
      image_size: detectionSettings.value.image_size,
      device: detectionSettings.value.device,
      overwrite: forceOverwrite,
    })
    connect(result.task_id)
    batchDetectionVisible.value = false
    if (currentIndex.value >= start && currentIndex.value <= end) {
      clearRenderedPreview()
    }
    ElMessage.success('检测任务已加入队列')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法创建检测任务')
  } finally {
    startingDetection.value = false
  }
}

function openBatchDetection(): void {
  if (
    selectedModel.value?.status === 'preparing' ||
    (!selectedModel.value && modelsPreparing.value)
  ) {
    ElMessage.info('检测模型准备中，请稍候')
    return
  }
  if (!selectedModel.value) {
    ElMessage.warning('请将 YOLO .pt 模型放入项目 models/yolo/ 目录并选择模型')
    return
  }
  batchStart.value = 1
  batchEnd.value = totalPages.value
  batchDetectionOverwrite.value = false
  batchDetectionVisible.value = true
}
async function cancelTask(): Promise<void> {
  if (!taskSnapshot.value) return
  try {
    taskSnapshot.value = await api.cancelTask(taskSnapshot.value.id)
    ElMessage.info('将在当前页推理结束后取消')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法取消任务')
  }
}
async function onTaskSnapshot(snapshot: TaskSnapshot): Promise<void> {
  if (
    ['export_zip', 'export_epub'].includes(snapshot.task_type) &&
    ['completed', 'failed', 'cancelled'].includes(snapshot.status)
  ) {
    void exportDownloads.check(snapshot.id)
  }
  if (
    ['completed', 'failed', 'cancelled'].includes(snapshot.status) &&
    !refreshingAfterTask.value
  ) {
    refreshingAfterTask.value = true
    loadedChunks.clear()
    try {
      await loadProject().catch(() => undefined)
      await ensureChunk(currentIndex.value)
      if (
        [
          'page_detection',
          'ocr',
          'quick_translation',
          'page_repair',
          'page_render',
          'batch_pipeline',
        ].includes(snapshot.task_type)
      )
        await loadPage(currentIndex.value)
      void preloadThumbnailChunks(projectId.value, totalPages.value)
      if (['export_zip', 'export_epub'].includes(snapshot.task_type)) {
        await api
          .listProjectExports(projectId.value)
          .then((value) => {
            exports.value = value.items
          })
          .catch(() => undefined)
      }
    } finally {
      refreshingAfterTask.value = false
    }
  }
}

function openBatchProcessing(): void {
  batchStart.value = 1
  batchEnd.value = totalPages.value
  batchPreview.value = null
  batchProcessingVisible.value = true
}

async function previewBatchProcessing(): Promise<void> {
  loadingBatchPreview.value = true
  try {
    const payload: BatchTaskPreviewRequest = {
      start_page: batchStart.value,
      end_page: batchEnd.value,
      run_ocr: true,
      run_translation: true,
      run_render: true,
      review_policy: 'reviewed_only',
      overwrite_model_results: batchOverwriteModel.value,
      overwrite_manual_ocr: batchOverwriteManualOCR.value,
      overwrite_manual_translation: batchOverwriteManualTranslation.value,
      llm_profile_id: project.value?.llm_profile_id,
    }
    batchPreview.value = await api.previewBatchTask(projectId.value, payload)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法预检批处理')
  } finally {
    loadingBatchPreview.value = false
  }
}

async function startBatchProcessing(): Promise<void> {
  // Re-run the preflight immediately before creation so changing the range,
  // mode or overwrite switches after the last preview cannot start
  // a task with stale eligibility/profile estimates.
  await previewBatchProcessing()
  if (!batchPreview.value) return
  if (batchOverwriteManualOCR.value || batchOverwriteManualTranslation.value) {
    try {
      await ElMessageBox.confirm(
        '覆盖人工 OCR 或人工译文会替换已确认的内容，是否继续？',
        '确认覆盖人工内容',
        { confirmButtonText: '继续覆盖', cancelButtonText: '取消', type: 'warning' },
      )
    } catch {
      return
    }
  }
  startingBatch.value = true
  try {
    const result = await api.createBatchTask(projectId.value, {
      start_page: batchStart.value,
      end_page: batchEnd.value,
      run_ocr: true,
      run_translation: true,
      run_render: true,
      review_policy: 'reviewed_only',
      overwrite_model_results: batchOverwriteModel.value,
      overwrite_manual_ocr: batchOverwriteManualOCR.value,
      overwrite_manual_translation: batchOverwriteManualTranslation.value,
      llm_profile_id: project.value?.llm_profile_id,
      confirm_allow_detected: true,
    })
    connect(result.task_id)
    batchProcessingVisible.value = false
    if (currentIndex.value >= batchStart.value && currentIndex.value <= batchEnd.value) {
      clearRenderedPreview()
    }
    ElMessage.success('批量处理任务已加入任务中心')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法创建批处理任务')
  } finally {
    startingBatch.value = false
  }
}

function onKeyDown(event: KeyboardEvent): void {
  const target = event.target as HTMLElement
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault()
    void saveRegions()
  } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
    event.preventDefault()
    selectedIds.value = history.current.value.map((item) => item.id)
  } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
    event.preventDefault()
    event.shiftKey ? redo() : undo()
  } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'y') {
    event.preventDefault()
    redo()
  } else if (event.key === 'Delete' || event.key === 'Backspace') deleteSelected()
  else if (event.key === 'Escape') {
    selectedIds.value = []
    mode.value = 'select'
  }
}
async function initialize(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    await loadProject()
    const [modelResult, configured, providers, profileResult, translationResult] =
      await Promise.all([
        api.listDetectionModels(),
        api.getDetectionSettings(),
        api.listOCRProviders(),
        api.listLLMProfiles(),
        api.getTranslationSettings(),
      ])
    models.value = modelResult.items
    track(modelResult.preparation)
    detectionSettings.value = configured
    ocrProviders.value = providers.items
    llmProfiles.value = profileResult.items
    translationSettings.value = translationResult
    const queryTask = Array.isArray(route.query.task) ? route.query.task[0] : route.query.task
    const taskId = queryTask || project.value?.active_task_id
    if (taskId) connect(taskId)
    if (totalPages.value) {
      await ensureChunk(currentIndex.value)
      await loadPage(currentIndex.value)
      void preloadThumbnailChunks(projectId.value, totalPages.value)
    }
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '项目加载失败'
  } finally {
    loading.value = false
  }
}

const { preparing: modelsPreparing, track } = useDetectionPreparation(async () => {
  const [result, configured] = await Promise.all([
    api.listDetectionModels(),
    api.getDetectionSettings(),
  ])
  models.value = result.items
  detectionSettings.value = configured
  track(result.preparation)
})

watch(
  history.current,
  () => {
    if (resetRegions) return
    dirty.value = true
    saveState.value = 'dirty'
    if (autosaveTimer) clearTimeout(autosaveTimer)
    autosaveTimer = setTimeout(() => void saveRegions(), 2000)
  },
  { deep: true },
)
watch(
  selectedRegion,
  (region) => {
    ocrDraft.value = region?.source_text ?? ''
  },
  { immediate: true },
)
watch(
  selectedRegion,
  (region) => {
    translationDraft.value = region?.target_text ?? ''
  },
  { immediate: true },
)
watch(projectId, () => {
  disconnect()
  void exportDownloads.check()
  blobCache.clear()
  loadedChunks.clear()
  pendingChunks.clear()
  currentIndex.value = 1
  void initialize()
})
watch(mobilePane, async (pane) => {
  if (pane === 'detection') {
    await nextTick()
    editor.value?.fit()
  }
})
onBeforeRouteLeave(async () => {
  await saveRegions()
  return saveState.value !== 'error'
})
onMounted(() => {
  window.addEventListener('keydown', onKeyDown)
  exportDownloads.start()
  void initialize()
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeyDown)
  exportDownloads.stop()
  if (autosaveTimer) clearTimeout(autosaveTimer)
  disconnect()
  blobCache.clear()
})
</script>

<template>
  <div class="project-workspace">
    <el-alert
      v-if="modelsPreparing"
      title="检测模型准备中；已有检测框的页面仍可 OCR、翻译和成图"
      type="info"
      :closable="false"
    />
    <div v-if="loading" class="workspace-loading"><el-skeleton animated :rows="8" /></div>
    <section v-else-if="loadError" class="empty-workspace small-empty">
      <div class="empty-icon">
        <el-icon><Warning /></el-icon>
      </div>
      <h2>项目无法打开</h2>
      <p>{{ loadError }}</p>
      <div class="inline-actions">
        <el-button :icon="RefreshRight" @click="initialize">重试</el-button
        ><el-button @click="router.push('/projects')">返回项目</el-button>
      </div>
    </section>
    <template v-else-if="project">
      <header class="workspace-header">
        <div>
          <button class="back-link" type="button" @click="router.push('/projects')">
            ← 翻译项目
          </button>
          <div class="workspace-title-row">
            <h1>{{ project.name }}</h1>
          </div>
        </div>
        <div class="workspace-meta">
          <el-tag type="info" effect="plain" aria-label="项目 OCR Provider">
            OCR ·
            {{
              resolvedOCRProvider?.name ??
              (resolvedOCRProviderId === 'mangaocr' ? 'MangaOCR' : 'PaddleOCR')
            }}
          </el-tag>
          <el-select
            :model-value="project.llm_profile_id ?? translationSettings?.default_profile_id"
            clearable
            size="small"
            :loading="changingLLMProfile"
            :disabled="taskBusy"
            placeholder="翻译 Profile"
            aria-label="项目翻译 Profile"
            @change="changeLLMProfile(String($event || ''))"
          >
            <el-option
              v-for="profile in llmProfiles"
              :key="profile.id"
              :label="`翻译 · ${profile.name}`"
              :value="profile.id"
            />
          </el-select>
          <StatusPill :status="project.status" /><span>{{ project.total_pages }} 页</span
          ><span v-if="project.warning_pages" class="danger-text"
            >{{ project.warning_pages }} 页异常</span
          ><span v-if="currentPage" class="page-pipeline-status"
            >翻译：{{
              translationStatusLabels[currentPage.translation_status ?? 'pending'] ??
              currentPage.translation_status
            }}
            · 成品：{{
              renderStatusLabels[currentPage.render_status ?? 'pending'] ??
              currentPage.render_status
            }}</span
          >
        </div>
      </header>
      <TaskProgress
        v-if="activeTask || refreshingAfterTask"
        :task="taskSnapshot!"
        class="workspace-task"
        @cancel="cancelTask"
      />
      <div v-if="connectionError" class="connection-note">任务连接已断开，正在自动重连…</div>

      <section v-if="totalPages" class="workflow-panel" aria-label="本页流程">
        <div class="workflow-heading">
          <strong>本页流程</strong>
          <span>{{ pipelineStage === 'complete' ? '成品已生成' : `第 ${currentIndex} 页` }}</span>
        </div>
        <div class="workflow-steps">
          <el-button
            class="stage-action"
            :class="{ 'stage-done': detectionPreviewReady }"
            :type="pipelineStage === 'detection' ? 'primary' : 'default'"
            :loading="startingDetection"
            :disabled="selectedModel?.status !== 'ready' || taskBusy"
            :title="selectedModel ? '' : '请先在设置中准备检测模型'"
            @click="startDetection(currentIndex, currentIndex, true)"
            ><span class="stage-index" aria-hidden="true">1</span>检测本页</el-button
          >
          <el-button
            class="stage-action"
            :class="{
              'stage-done':
                pipelineStage === 'translation' ||
                pipelineStage === 'render' ||
                pipelineStage === 'complete',
            }"
            :type="pipelineStage === 'ocr' ? 'primary' : 'default'"
            :loading="startingOCR"
            :disabled="
              taskBusy ||
              dirty ||
              !['detected', 'reviewed'].includes(currentPage?.detection_status ?? '') ||
              !history.current.value.length ||
              !ocrReady
            "
            :title="
              !ocrReady
                ? '请在设置中准备 OCR 模型'
                : !detectionPreviewReady
                  ? '完成检测后才能执行 OCR'
                  : ''
            "
            @click="startPageOCR"
            ><span class="stage-index" aria-hidden="true">2</span>当前页 OCR</el-button
          >
          <el-button
            class="stage-action"
            :class="{ 'stage-done': pipelineStage === 'render' || pipelineStage === 'complete' }"
            :type="pipelineStage === 'translation' ? 'primary' : 'default'"
            :loading="startingTranslation"
            :disabled="taskBusy || dirty || !translationReady"
            title="完成 OCR 并配置翻译 Profile 后可翻译"
            @click="startPageTranslation"
            ><span class="stage-index" aria-hidden="true">3</span>开始翻译</el-button
          >
          <el-button
            class="stage-action"
            :class="{ 'stage-done': pipelineStage === 'complete' }"
            :type="pipelineStage === 'render' ? 'primary' : 'default'"
            :loading="startingGeneration"
            :disabled="taskBusy || dirty || !generationReady"
            title="译文就绪后可生成成品"
            @click="startPageGeneration"
            ><span class="stage-index" aria-hidden="true">4</span>开始生成</el-button
          >
        </div>
        <div class="project-actions">
          <span class="tool-group-label">项目操作</span>
          <button
            type="button"
            class="project-actions-toggle"
            :aria-expanded="mobileProjectActionsOpen"
            @click="mobileProjectActionsOpen = !mobileProjectActionsOpen"
          >
            项目操作 <span aria-hidden="true">{{ mobileProjectActionsOpen ? '−' : '+' }}</span>
          </button>
          <div class="project-action-buttons" :class="{ open: mobileProjectActionsOpen }">
            <el-button
              plain
              :disabled="
                taskBusy ||
                !totalPages ||
                selectedModel?.status === 'preparing' ||
                (!selectedModel && modelsPreparing)
              "
              :title="selectedModel ? '只执行 YOLO 检测' : '请先在设置中准备检测模型'"
              @click="openBatchDetection"
              >批量检测</el-button
            >
            <el-button plain :disabled="taskBusy || !totalPages" @click="openBatchProcessing"
              >批量处理</el-button
            >
            <el-button plain :disabled="taskBusy" @click="openHealth">项目健康检查</el-button>
            <el-button plain :disabled="taskBusy" @click="openExport">导出</el-button>
            <el-button
              plain
              :loading="settingCover"
              :disabled="taskBusy || dirty || !coverReady"
              title="仅可选择最新成品页作为封面"
              @click="setCurrentCover"
              >设为封面</el-button
            >
          </div>
        </div>
      </section>

      <div
        v-if="totalPages"
        class="workspace-utilities"
        :class="{
          'utility-expanded': mobileThumbnailsOpen || mobileEditToolsOpen,
          'single-page': totalPages === 1,
        }"
      >
        <section
          v-if="totalPages > 1"
          ref="thumbnailRail"
          class="thumbnail-rail"
          :class="{ 'mobile-thumbnails-open': mobileThumbnailsOpen }"
          aria-label="漫画页面"
        >
          <button
            type="button"
            class="thumbnail-rail-toggle"
            :aria-expanded="mobileThumbnailsOpen"
            @click="mobileThumbnailsOpen = !mobileThumbnailsOpen"
          >
            第 {{ currentIndex }} / {{ totalPages }} 页
            <span aria-hidden="true">{{ mobileThumbnailsOpen ? '−' : '+' }}</span>
          </button>
          <div class="thumbnail-grid">
            <div
              v-for="item in slots"
              :key="item.key"
              class="thumbnail-item"
              :data-page-index="item.index"
            >
              <button
                type="button"
                class="thumbnail-button"
                :class="{
                  current: item.index === currentIndex,
                  failed: item.page && ['corrupt', 'failed'].includes(item.page.import_status),
                  detected: ['detected', 'reviewed'].includes(item.page?.detection_status ?? ''),
                }"
                :aria-label="`第 ${item.index} 页`"
                @click="loadPage(item.index)"
              >
                <img
                  v-if="item.page?.thumbnail_url"
                  :src="item.page.thumbnail_url"
                  alt=""
                  loading="lazy"
                /><span v-else class="thumbnail-placeholder">{{
                  String(item.index).padStart(3, '0')
                }}</span
                ><small>{{ String(item.index).padStart(3, '0') }}</small>
              </button>
              <button
                v-if="item.page?.render_status === 'completed' && item.page.rendered_thumbnail_url"
                type="button"
                class="thumbnail-cover-action"
                :aria-label="`第 ${item.index} 页设为封面`"
                title="设为封面"
                :disabled="taskBusy || settingCover || dirty"
                @click.stop="setCoverForPage(item.page)"
              >
                <el-icon><Picture /></el-icon>
              </button>
            </div>
          </div>
        </section>

        <section
          class="detection-toolbar"
          :class="{ 'mobile-tools-open': mobileEditToolsOpen }"
          aria-label="画布编辑工具"
        >
          <button
            type="button"
            class="edit-tools-toggle"
            :aria-expanded="mobileEditToolsOpen"
            @click="mobileEditToolsOpen = !mobileEditToolsOpen"
          >
            画布工具
            <span
              >{{
                { saved: '已保存', dirty: '待保存', saving: '保存中…', error: '保存失败' }[
                  saveState
                ]
              }}
              {{ mobileEditToolsOpen ? '−' : '+' }}</span
            >
          </button>
          <div class="tool-group edit-tool-group">
            <span class="tool-group-label">画布工具</span>
            <el-button-group>
              <el-button
                :type="mode === 'select' ? 'primary' : 'default'"
                :icon="EditPen"
                @click="mode = 'select'"
                >选择</el-button
              >
              <el-button
                :type="mode === 'draw' ? 'primary' : 'default'"
                :icon="Picture"
                :disabled="!selectedModel"
                @click="mode = 'draw'"
                >画框</el-button
              >
            </el-button-group>
            <el-button :icon="Delete" :disabled="!selectedIds.length" @click="deleteSelected"
              >删除框</el-button
            >
            <el-button :disabled="!history.canUndo.value" @click="undo">撤销</el-button>
            <el-button :disabled="!history.canRedo.value" @click="redo">重做</el-button>
            <el-button :icon="boxesVisible ? Hide : View" @click="boxesVisible = !boxesVisible">{{
              boxesVisible ? '隐藏框' : '显示框'
            }}</el-button>
            <el-button :icon="ZoomIn" @click="editor?.fit()">适应画布</el-button>
          </div>
          <div class="tool-group edit-save-group">
            <span class="save-indicator" :class="saveState">{{
              { saved: '已保存', dirty: '待保存', saving: '保存中…', error: '保存失败' }[saveState]
            }}</span>
            <el-button @click="saveRegions">保存</el-button>
            <el-button
              :icon="Check"
              :disabled="currentPage?.detection_status === 'undetected'"
              @click="saveAndNext"
              >保存并下一页</el-button
            >
          </div>
        </section>
      </div>

      <main v-if="totalPages" class="annotation-layout">
        <div class="comparison-switcher" role="group" aria-label="图像视图">
          <button
            type="button"
            :class="{ active: mobilePane === 'detection' }"
            :aria-pressed="mobilePane === 'detection'"
            @click="mobilePane = 'detection'"
          >
            检测图
          </button>
          <button
            type="button"
            :class="{ active: mobilePane === 'final' }"
            :aria-pressed="mobilePane === 'final'"
            @click="mobilePane = 'final'"
          >
            成品图
          </button>
        </div>
        <section
          class="comparison-pane detection-pane"
          :class="{ 'mobile-pane-hidden': mobilePane !== 'detection' }"
          aria-label="YOLO 检测图"
        >
          <div class="comparison-pane-head">
            <strong>YOLO 检测图</strong>
            <span v-if="previewUrl && !detectionPreviewReady" class="pane-stage-note"
              >原稿 · 待检测</span
            >
          </div>
          <div
            class="page-canvas annotation-canvas"
            :class="{ loading: previewLoading }"
            :style="detectionCanvasStyle"
          >
            <DetectionEditor
              v-if="
                detectionPreviewReady && previewUrl && currentDetail?.width && currentDetail.height
              "
              ref="editor"
              :image-url="previewUrl"
              :image-width="currentDetail.width"
              :image-height="currentDetail.height"
              :regions="boxesVisible ? history.current.value : []"
              :selected-ids="boxesVisible ? selectedIds : []"
              :mode="boxesVisible ? mode : 'select'"
              :draw-class-id="Number(drawClass[0])"
              :draw-class-name="drawClass[1]"
              @change="onEditorChange"
              @select="selectedIds = $event"
            />
            <img
              v-else-if="previewUrl"
              class="source-preview-image"
              :src="previewUrl"
              :alt="`第 ${currentIndex} 页漫画原稿`"
            />
            <div v-else class="preview-message">
              <el-icon><Picture /></el-icon>
              <strong>{{ previewLoading ? '正在载入原稿' : '原稿暂不可用' }}</strong>
              <span v-if="previewError">{{ previewError }}</span>
              <span v-else-if="!detectionPreviewReady">完成检测后，检测框会显示在原稿上。</span>
            </div>
          </div>
        </section>
        <section
          class="comparison-pane final-pane"
          :class="{ 'mobile-pane-hidden': mobilePane !== 'final' }"
          aria-label="翻译成品图"
        >
          <div class="comparison-pane-head">
            <strong>翻译成品图</strong>
            <el-button
              size="small"
              :disabled="
                !previewUrl || !renderState?.mask_url || renderState.repair_status !== 'completed'
              "
              :loading="maskPreviewLoading"
              @click="toggleMaskPreview"
              >{{ maskPreviewVisible ? '返回成品' : '查看掩膜' }}</el-button
            >
          </div>
          <div class="page-canvas final-canvas" :class="{ loading: renderedPreviewLoading }">
            <div
              v-if="maskPreviewVisible && maskPreviewUrl && previewUrl"
              style="position: relative; width: 100%"
            >
              <img :src="previewUrl" alt="原图与擦除掩膜" style="display: block; width: 100%" />
              <div
                :style="{
                  position: 'absolute',
                  inset: '0',
                  background: 'rgba(255, 30, 70, 0.65)',
                  maskImage: `url(${maskPreviewUrl})`,
                  maskMode: 'luminance',
                  maskSize: '100% 100%',
                  maskRepeat: 'no-repeat',
                  pointerEvents: 'none',
                }"
              />
              <span
                style="
                  position: absolute;
                  top: 8px;
                  left: 8px;
                  padding: 4px 8px;
                  background: #222;
                  color: white;
                "
                >红色为擦除范围</span
              >
            </div>
            <img
              v-else-if="renderedPreviewVisible"
              :src="renderedPreviewUrl!"
              alt="翻译最终成品图"
              class="rendered-preview-image"
            />
            <div v-else class="preview-message">
              <el-icon><Picture /></el-icon>
              <strong>{{ renderedPreviewStatusText }}</strong>
              <span v-if="renderedPreviewError">{{ renderedPreviewError }}</span>
              <span v-else-if="renderState?.render_error">{{ renderState.render_error }}</span>
            </div>
          </div>
        </section>
        <aside class="region-panel">
          <div class="region-panel-head">
            <div>
              <h2>文本区域 {{ history.current.value.length }}</h2>
            </div>
            <span>{{ selectedIds.length }} 个已选</span>
          </div>
          <section class="region-picker">
            <div class="region-section-head">
              <span>图片识别的文本框</span>
              <small>点击选择</small>
            </div>
            <ol class="region-list">
              <li v-for="(region, index) in history.current.value" :key="region.id">
                <button
                  type="button"
                  :class="{ selected: selectedIds.includes(region.id) }"
                  :disabled="refreshingAfterTask"
                  @click="selectedIds = [region.id]"
                >
                  <span>#{{ index + 1 }} {{ region.class_name }}</span>
                  <small
                    >{{ ocrStatusLabels[region.ocr_status] ?? region.ocr_status }} ·
                    {{
                      translationStatusLabels[region.translation_status ?? 'pending'] ??
                      region.translation_status
                    }}</small
                  >
                </button>
              </li>
            </ol>
          </section>
          <section class="region-editor">
            <div v-if="selectedRegions.length === 1" class="region-detail">
              <el-form label-position="top"
                ><el-form-item label="类别"
                  ><el-select
                    :model-value="selectedRegions[0]!.class_name"
                    @change="changeSelectedClass(String($event))"
                    ><el-option
                      v-for="entry in classEntries"
                      :key="entry[0]"
                      :label="entry[1]"
                      :value="entry[1]" /></el-select></el-form-item
              ></el-form>
              <dl>
                <div>
                  <dt>OCR 状态</dt>
                  <dd :class="{ 'danger-text': selectedRegions[0]!.ocr_status === 'failed' }">
                    {{
                      ocrStatusLabels[selectedRegions[0]!.ocr_status] ??
                      selectedRegions[0]!.ocr_status
                    }}
                  </dd>
                </div>
                <div v-if="selectedRegions[0]!.ocr_provider">
                  <dt>Provider</dt>
                  <dd>{{ selectedRegions[0]!.ocr_provider }}</dd>
                </div>
                <div v-if="selectedRegions[0]!.ocr_confidence !== null">
                  <dt>OCR 置信度</dt>
                  <dd>{{ (selectedRegions[0]!.ocr_confidence! * 100).toFixed(1) }}%</dd>
                </div>
                <div>
                  <dt>置信度</dt>
                  <dd>
                    {{
                      selectedRegions[0]!.confidence === null
                        ? '人工框'
                        : `${(selectedRegions[0]!.confidence! * 100).toFixed(1)}%`
                    }}
                  </dd>
                </div>
                <div>
                  <dt>坐标</dt>
                  <dd>
                    {{ Math.round(selectedRegions[0]!.x1) }},
                    {{ Math.round(selectedRegions[0]!.y1) }} →
                    {{ Math.round(selectedRegions[0]!.x2) }},
                    {{ Math.round(selectedRegions[0]!.y2) }}
                  </dd>
                </div>
              </dl>
              <div class="ocr-editor-block">
                <label for="source-text">原文</label>
                <el-input
                  id="source-text"
                  v-model="ocrDraft"
                  type="textarea"
                  :rows="5"
                  maxlength="10000"
                  show-word-limit
                  placeholder="识别结果或人工校对原文"
                  @input="clearRenderedPreview"
                />
                <p v-if="selectedRegions[0]!.ocr_status === 'outdated'" class="ocr-outdated">
                  OCR 已过期：保存检测框后可重新识别。
                </p>
                <p v-if="selectedRegions[0]!.ocr_error" class="inline-error">
                  {{ selectedRegions[0]!.ocr_error }}
                </p>
                <div class="ocr-editor-actions">
                  <el-button :loading="savingOCRText" @click="saveOCRText">保存原文</el-button>
                  <el-button
                    type="primary"
                    plain
                    :loading="startingOCR"
                    :disabled="
                      taskBusy ||
                      !ocrReady ||
                      !['detected', 'reviewed'].includes(currentPage?.detection_status ?? '')
                    "
                    @click="retryRegionOCR"
                  >
                    {{
                      selectedRegions[0]!.ocr_status === 'failed' ? '重新识别失败区域' : '重新 OCR'
                    }}
                  </el-button>
                </div>
              </div>
              <div class="ocr-editor-block translation-editor-block">
                <label for="target-text">API 译文（可校对）</label>
                <el-input
                  id="target-text"
                  v-model="translationDraft"
                  type="textarea"
                  :rows="5"
                  maxlength="10000"
                  show-word-limit
                  placeholder="API 返回的译文，可直接校对"
                  @input="clearRenderedPreview"
                />
                <p
                  v-if="selectedRegions[0]!.translation_status === 'outdated'"
                  class="ocr-outdated"
                >
                  译文已过期：OCR 原文或文本框发生变化，请重新翻译。
                </p>
                <p v-if="selectedRegions[0]!.translation_error" class="inline-error">
                  {{ selectedRegions[0]!.translation_error }}
                </p>
                <div class="ocr-editor-actions">
                  <el-button :loading="savingTranslationText" @click="saveTranslationText"
                    >保存译文</el-button
                  >
                  <el-button
                    type="primary"
                    plain
                    :loading="startingTranslation"
                    :disabled="taskBusy || !selectedTranslationReady"
                    @click="retryRegionTranslation"
                    >{{
                      selectedRegions[0]!.translation_status === 'failed' ? '重试翻译' : '重新翻译'
                    }}</el-button
                  >
                </div>
              </div>
            </div>
            <div v-else class="region-empty">
              <strong>{{ selectedIds.length > 1 ? '已多选文本框' : '选择一个文本框' }}</strong
              ><span>拖动可移动，控制点可缩放。按空格拖动画布，滚轮缩放。</span>
            </div>
          </section>
        </aside>
      </main>

      <footer v-if="totalPages" class="page-navigation">
        <el-button
          circle
          :icon="ArrowLeft"
          :disabled="currentIndex <= 1"
          aria-label="上一页"
          @click="loadPage(currentIndex - 1)"
        /><el-slider
          v-model="navigationIndex"
          :min="1"
          :max="Math.max(totalPages, 1)"
          :show-tooltip="false"
          @change="loadPage(Number($event))"
        />
        <div class="page-counter">
          <el-input-number
            v-model="navigationIndex"
            :min="1"
            :max="Math.max(totalPages, 1)"
            controls-position="right"
            @change="loadPage(Number($event))"
          /><span>/ {{ totalPages }}</span>
        </div>
        <el-button
          circle
          :icon="ArrowRight"
          :disabled="currentIndex >= totalPages"
          aria-label="下一页"
          @click="loadPage(currentIndex + 1)"
        />
      </footer>
    </template>

    <el-dialog v-model="batchDetectionVisible" title="批量 YOLO 检测" width="460px">
      <el-form label-position="top">
        <div class="range-grid">
          <el-form-item label="起始页">
            <el-input-number v-model="batchStart" :min="1" :max="batchEnd" />
          </el-form-item>
          <el-form-item label="结束页">
            <el-input-number v-model="batchEnd" :min="batchStart" :max="totalPages" />
          </el-form-item>
        </div>
        <el-form-item>
          <el-checkbox v-model="batchDetectionOverwrite">覆盖已有检测结果</el-checkbox>
        </el-form-item>
        <p class="dialog-note">此任务只执行 YOLO 检测，不会自动开始 OCR、翻译或成图。</p>
      </el-form>
      <template #footer>
        <el-button @click="batchDetectionVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="startingDetection"
          @click="startDetection(batchStart, batchEnd, batchDetectionOverwrite)"
          >开始批量检测</el-button
        >
      </template>
    </el-dialog>

    <el-dialog v-model="batchProcessingVisible" title="批量处理" width="520px">
      <el-form label-position="top">
        <div class="range-grid">
          <el-form-item label="起始页"
            ><el-input-number v-model="batchStart" :min="1" :max="batchEnd"
          /></el-form-item>
          <el-form-item label="结束页"
            ><el-input-number v-model="batchEnd" :min="batchStart" :max="totalPages"
          /></el-form-item>
        </div>
        <el-form-item label="执行流水线">
          <div class="batch-pipeline-steps" aria-label="批量处理流水线">
            <el-tag type="warning">OCR</el-tag>
            <span class="batch-pipeline-arrow">→</span>
            <el-tag type="primary">API 翻译</el-tag>
            <span class="batch-pipeline-arrow">→</span>
            <el-tag type="success">图片修复</el-tag>
            <span class="batch-pipeline-arrow">→</span>
            <el-tag>译文回填</el-tag>
            <span class="batch-pipeline-arrow">→</span>
            <el-tag type="success">最终成图</el-tag>
          </div>
          <p class="dialog-note">
            批量处理只处理已有检测框的页面；未检测页面会被跳过，可先使用“批量检测”。
          </p>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="batchOverwriteModel">覆盖模型结果</el-checkbox>
          <el-checkbox v-model="batchOverwriteManualOCR">覆盖人工 OCR</el-checkbox>
          <el-checkbox v-model="batchOverwriteManualTranslation">覆盖人工译文</el-checkbox>
        </el-form-item>
        <p v-if="batchPreview" class="dialog-note">
          可处理 {{ batchPreview.eligible_pages }} 页，跳过 {{ batchPreview.skipped_pages }} 页；
          翻译只发送 OCR 文本。
        </p>
        <p v-if="batchPreview" class="dialog-note">
          预计：OCR {{ batchPreview.estimated_ocr_requests }}
          · LLM
          {{ batchPreview.estimated_llm_requests }} · 修复/生成
          {{ batchPreview.estimated_render_requests }}
        </p>
        <ul v-if="batchPreview?.skipped.length" class="batch-skip-list">
          <li v-for="item in batchPreview.skipped.slice(0, 8)" :key="String(item.page_index)">
            第 {{ item.page_index }} 页：{{ item.reason || '不满足处理条件' }}
          </li>
          <li v-if="batchPreview.skipped.length > 8">
            还有 {{ batchPreview.skipped.length - 8 }} 页…
          </li>
        </ul>
      </el-form>
      <template #footer>
        <el-button @click="batchProcessingVisible = false">取消</el-button>
        <el-button :loading="loadingBatchPreview" @click="previewBatchProcessing">预检</el-button>
        <el-button type="primary" :loading="startingBatch" @click="startBatchProcessing"
          >开始批量处理</el-button
        >
      </template>
    </el-dialog>
    <el-drawer v-model="healthVisible" title="项目健康检查" size="500px">
      <div v-loading="healthLoading" class="health-drawer">
        <div v-if="healthReport" class="health-summary" :class="`health-${healthReport.status}`">
          <strong>{{
            healthReport.status === 'healthy'
              ? '健康'
              : healthReport.status === 'warning'
                ? '有警告'
                : '存在问题'
          }}</strong>
          <span
            >错误 {{ healthReport.counts.errors }} · 警告 {{ healthReport.counts.warnings }} · 正常
            {{ healthReport.counts.ok }}</span
          >
        </div>
        <el-checkbox v-model="healthDeep" @change="loadHealth(Boolean(healthDeep))"
          >执行深度图片校验</el-checkbox
        >
        <el-empty v-if="healthReport && !healthReport.checks.length" description="未发现问题" />
        <ul v-else-if="healthReport" class="health-issue-list">
          <li v-for="(issue, index) in visibleHealthIssues" :key="`${issue.code}-${index}`">
            <div>
              <el-tag :type="issue.severity === 'error' ? 'danger' : 'warning'" size="small">
                {{ issue.severity === 'error' ? '错误' : '警告' }}
              </el-tag>
              <strong>{{ issue.code }}</strong>
            </div>
            <p>{{ issue.message }}</p>
            <el-button v-if="issue.page_index" text type="primary" @click="openHealthIssue(issue)"
              >打开第 {{ issue.page_index }} 页</el-button
            >
          </li>
        </ul>
        <el-pagination
          v-if="healthReport && healthReport.checks.length > healthIssueLimit"
          v-model:current-page="healthIssuePage"
          :page-size="healthIssueLimit"
          :total="healthReport.checks.length"
          layout="prev, pager, next"
          @current-change="handleHealthIssuePageChange"
        />
      </div>
    </el-drawer>
    <el-dialog v-model="exportVisible" title="导出项目" width="620px">
      <div v-loading="exportLoading" class="export-dialog">
        <p class="dialog-note">
          当前页 PNG/JPG 会直接下载；ZIP/EPUB
          在后台生成，完成后自动由浏览器下载。历史导出可用于重新下载。归档不包含原图、OCR
          文本或密钥。
        </p>
        <div class="export-current-actions">
          <span>当前第 {{ currentIndex }} 页</span>
          <el-button :disabled="!currentPage" @click="downloadCurrentPage('png')"
            >下载 PNG</el-button
          >
          <el-button :disabled="!currentPage" @click="downloadCurrentPage('jpg')"
            >下载 JPG</el-button
          >
        </div>
        <el-alert
          v-if="healthReport && !healthReport.exportable.zip"
          type="warning"
          :closable="false"
          title="项目归档导出被阻止：请先修复健康检查中的缺失或过期成品页。"
        />
        <div class="export-archive-actions">
          <el-button
            type="primary"
            :loading="creatingExport && exportFormat === 'zip'"
            :disabled="!healthReport?.exportable.zip || creatingExport"
            @click="createProjectExport('zip')"
            >创建 ZIP 漫画包</el-button
          >
          <el-button
            type="primary"
            :loading="creatingExport && exportFormat === 'epub'"
            :disabled="!healthReport?.exportable.epub || creatingExport"
            @click="createProjectExport('epub')"
            >创建 EPUB 3</el-button
          >
        </div>
        <h3>历史导出</h3>
        <el-empty v-if="!exports.length" description="暂无导出记录" />
        <ul v-else class="export-history-list">
          <li v-for="item in exports" :key="item.id">
            <div>
              <strong>{{ item.format.toUpperCase() }}</strong>
              <span>{{ item.filename }} · {{ formatExportSize(item.size) }}</span>
            </div>
            <el-tag v-if="item.status !== 'completed'" size="small">{{ item.status }}</el-tag>
            <div class="inline-actions">
              <el-button
                v-if="item.status === 'completed' && item.download_url"
                text
                type="primary"
                @click="downloadProjectExport(item)"
                >下载</el-button
              >
              <el-button text type="danger" @click="removeExport(item)">删除</el-button>
            </div>
          </li>
        </ul>
      </div>
    </el-dialog>
  </div>
</template>
