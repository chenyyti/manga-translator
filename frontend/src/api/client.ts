import axios, { AxiosError, type AxiosProgressEvent } from 'axios'

import type {
  ApiFailure,
  DetectionModel,
  DetectionRegion,
  DetectionRuntime,
  DetectionSettings,
  Envelope,
  ImportSession,
  ImportSourceType,
  LLMProfile,
  LLMProviderId,
  LLMProviderInfo,
  FontInfo,
  InpaintingProviderInfo,
  InpaintingProviderId,
  RenderSettings,
  RenderState,
  OCRProviderId,
  OCRProviderInfo,
  OCRSettings,
  PageDetail,
  PageSummary,
  Paginated,
  ProjectDetail,
  ProjectSummary,
  ReaderManifest,
  ReaderPage,
  RegionDocument,
  RuntimeSettings,
  SourceLanguage,
  TaskSnapshot,
  TaskItemRead,
  BatchTaskPreview,
  BatchTaskPreviewRequest,
  PerformanceSettings,
  TranslationSettings,
  ExportArtifact,
  ExportFormat,
  HealthReport,
  LauncherSession,
} from '@/types'

export class ClientError extends Error {
  constructor(
    message: string,
    public readonly code = 'REQUEST_FAILED',
    public readonly requestId?: string,
  ) {
    super(message)
  }
}

const http = axios.create({ baseURL: '/api', timeout: 60_000 })

function toClientError(error: unknown): ClientError {
  if (error instanceof AxiosError) {
    const body = error.response?.data as ApiFailure | undefined
    if (body && body.success === false) {
      return new ClientError(body.error.message, body.error.code, body.error.request_id)
    }
    if (error.code === 'ECONNABORTED') return new ClientError('请求超时，请稍后重试', 'TIMEOUT')
    if (!error.response) return new ClientError('无法连接本地服务', 'CONNECTION_FAILED')
  }
  return new ClientError(error instanceof Error ? error.message : '请求失败')
}

async function request<T>(operation: Promise<{ data: Envelope<T> }>): Promise<T> {
  try {
    return (await operation).data.data
  } catch (error) {
    throw toClientError(error)
  }
}

export const api = {
  createImportSession(payload: {
    project_name: string
    source_language: SourceLanguage
    target_language: 'zh-CN'
    source_type: ImportSourceType
    ocr_provider: OCRProviderId
    llm_profile_id?: string | null
  }) {
    return request<ImportSession>(http.post('/import-sessions', payload))
  },
  uploadImportFile(
    sessionId: string,
    file: File,
    relativePath: string,
    onProgress?: (event: AxiosProgressEvent) => void,
  ) {
    const data = new FormData()
    data.append('file', file, file.name)
    data.append('relative_path', relativePath)
    return request<{ id: string; relative_path: string; size: number; sha256: string }>(
      http.post(`/import-sessions/${sessionId}/files`, data, {
        onUploadProgress: onProgress,
        timeout: 0,
      }),
    )
  },
  deleteImportSession(sessionId: string) {
    return request<{ deleted: boolean }>(http.delete(`/import-sessions/${sessionId}`))
  },
  commitImportSession(sessionId: string) {
    return request<{ project_id: string; task_id: string }>(
      http.post(`/import-sessions/${sessionId}/commit`),
    )
  },
  listProjects(offset = 0, limit = 30) {
    return request<Paginated<ProjectSummary>>(http.get('/projects', { params: { offset, limit } }))
  },
  listBookshelf(offset = 0, limit = 30) {
    return request<Paginated<ProjectSummary>>(http.get('/bookshelf', { params: { offset, limit } }))
  },
  getProject(id: string) {
    return request<ProjectDetail>(http.get(`/projects/${id}`))
  },
  getReaderManifest(projectId: string) {
    return request<ReaderManifest>(http.get(`/projects/${projectId}/reader`))
  },
  listReaderPages(projectId: string, offset = 0, limit = 50) {
    return request<Paginated<ReaderPage>>(
      http.get(`/projects/${projectId}/reader/pages`, { params: { offset, limit } }),
    )
  },
  updateReadingProgress(projectId: string, pageId: string) {
    return request<{
      project_id: string
      page_id: string
      page_index: number
      updated_at: string
    }>(http.put(`/projects/${projectId}/reading-progress`, { page_id: pageId }))
  },
  updateProjectCover(projectId: string, pageId: string) {
    return request<{ project_id: string; cover_page_id: string; cover_thumbnail_url: string }>(
      http.put(`/projects/${projectId}/cover`, { page_id: pageId }),
    )
  },
  pageExportUrl(pageId: string, format: 'png' | 'jpg') {
    return `/api/pages/${encodeURIComponent(pageId)}/export/${format}`
  },
  createProjectExport(projectId: string, format: ExportFormat) {
    return request<{
      export_id: string
      task_id: string
      format: ExportFormat
      page_count: number
    }>(http.post(`/projects/${projectId}/exports`, { format }))
  },
  listProjectExports(projectId: string, offset = 0, limit = 30) {
    return request<Paginated<ExportArtifact>>(
      http.get(`/projects/${projectId}/exports`, { params: { offset, limit } }),
    )
  },
  getExport(exportId: string) {
    return request<ExportArtifact>(http.get(`/exports/${exportId}`))
  },
  exportDownloadUrl(exportId: string) {
    return `/api/exports/${encodeURIComponent(exportId)}/download`
  },
  deleteExport(exportId: string) {
    return request<{ deleted: boolean; export_id: string }>(
      http.delete(`/exports/${encodeURIComponent(exportId)}`),
    )
  },
  getProjectHealth(projectId: string, deep = false) {
    return request<HealthReport>(
      http.get(`/projects/${projectId}/health-check`, { params: { deep } }),
    )
  },
  deleteProject(id: string) {
    return request<{ deleted: boolean }>(http.delete(`/projects/${id}`))
  },
  listPages(projectId: string, offset: number, limit = 50) {
    return request<Paginated<PageSummary>>(
      http.get(`/projects/${projectId}/pages`, { params: { offset, limit } }),
    )
  },
  getPage(pageId: string) {
    return request<PageDetail>(http.get(`/pages/${pageId}`))
  },
  getTask(taskId: string) {
    return request<TaskSnapshot>(http.get(`/tasks/${taskId}`))
  },
  cancelTask(taskId: string) {
    return request<TaskSnapshot>(http.post(`/tasks/${taskId}/cancel`))
  },
  listTasks(
    params: {
      project_id?: string
      status?: string
      task_type?: string
      offset?: number
      limit?: number
    } = {},
  ) {
    return request<Paginated<TaskSnapshot>>(http.get('/tasks', { params }))
  },
  listTaskItems(taskId: string, params: { status?: string; offset?: number; limit?: number } = {}) {
    return request<Paginated<TaskItemRead>>(http.get(`/tasks/${taskId}/items`, { params }))
  },
  pauseTask(taskId: string) {
    return request<TaskSnapshot>(http.post(`/tasks/${taskId}/pause`))
  },
  resumeTask(taskId: string) {
    return request<TaskSnapshot>(http.post(`/tasks/${taskId}/resume`))
  },
  retryFailedTask(taskId: string) {
    return request<{ task_id: string; project_id: string; retry_of_task_id: string }>(
      http.post(`/tasks/${taskId}/retry-failed`),
    )
  },
  previewBatchTask(projectId: string, payload: BatchTaskPreviewRequest) {
    return request<BatchTaskPreview>(
      http.post(`/projects/${projectId}/batch-tasks/preview`, payload),
    )
  },
  createBatchTask(
    projectId: string,
    payload: BatchTaskPreviewRequest & { confirm_allow_detected?: boolean },
  ) {
    return request<{ task_id: string; project_id: string; preview: BatchTaskPreview }>(
      http.post(`/projects/${projectId}/batch-tasks`, payload),
    )
  },
  getPerformanceSettings() {
    return request<PerformanceSettings>(http.get('/settings/performance'))
  },
  updatePerformanceSettings(payload: PerformanceSettings & { expected_revision: number }) {
    return request<PerformanceSettings>(http.put('/settings/performance', payload))
  },
  getRuntimeSettings() {
    return request<RuntimeSettings>(http.get('/settings/runtime'))
  },
  getLauncherSession() {
    return request<LauncherSession>(http.get('/runtime/session'))
  },
  sendLauncherHeartbeat(sessionId: string) {
    return request<{ active: boolean }>(
      http.post('/runtime/session/heartbeat', { session_id: sessionId }, { timeout: 5_000 }),
    )
  },
  listInpaintingProviders() {
    return request<{ items: InpaintingProviderInfo[] }>(http.get('/providers/inpainting'))
  },
  getRenderSettings() {
    return request<RenderSettings>(http.get('/settings/render'))
  },
  updateRenderSettings(payload: RenderSettings & { expected_revision: number }) {
    return request<RenderSettings>(http.put('/settings/render', payload))
  },
  listFonts() {
    return request<{ items: FontInfo[] }>(http.get('/fonts'))
  },
  uploadFont(file: File, onProgress?: (event: AxiosProgressEvent) => void) {
    const data = new FormData()
    data.append('file', file, file.name)
    return request<FontInfo>(
      http.post('/fonts', data, { onUploadProgress: onProgress, timeout: 0 }),
    )
  },
  deleteFont(id: string) {
    return request<{ deleted: boolean }>(http.delete(`/fonts/${encodeURIComponent(id)}`))
  },
  getPageRenderSettings(pageId: string) {
    return request<{
      page_id: string
      revision: number
      overrides: Record<string, unknown>
      effective: Record<string, unknown>
    }>(http.get(`/pages/${pageId}/render-settings`))
  },
  updatePageRenderSettings(
    pageId: string,
    expectedRevision: number,
    overrides: Record<string, unknown>,
  ) {
    return request<{ page_id: string; revision: number; overrides: Record<string, unknown> }>(
      http.put(`/pages/${pageId}/render-settings`, {
        expected_revision: expectedRevision,
        overrides,
      }),
    )
  },
  getRegionRenderSettings(regionId: string) {
    return request<{
      region_id: string
      revision: number
      overrides: Record<string, unknown>
      effective?: Record<string, unknown>
    }>(http.get(`/regions/${regionId}/render-settings`))
  },
  updateRegionRenderSettings(
    regionId: string,
    expectedRevision: number,
    overrides: Record<string, unknown>,
  ) {
    return request<{
      region_id: string
      revision: number
      overrides: Record<string, unknown>
      effective?: Record<string, unknown>
    }>(
      http.put(`/regions/${regionId}/render-settings`, {
        expected_revision: expectedRevision,
        overrides,
      }),
    )
  },
  createPageRepairTask(
    pageId: string,
    payload: {
      provider?: InpaintingProviderId
      device?: 'auto' | 'cpu' | 'cuda'
      force?: boolean
    } = {},
  ) {
    return request<{ task_id: string; project_id: string; page_id: string }>(
      http.post(`/pages/${pageId}/repair`, payload),
    )
  },
  createPageRenderTask(
    pageId: string,
    payload: {
      provider?: InpaintingProviderId
      device?: 'auto' | 'cpu' | 'cuda'
      force_repair?: boolean
    } = {},
  ) {
    return request<{ task_id: string; project_id: string; page_id: string }>(
      http.post(`/pages/${pageId}/render`, payload),
    )
  },
  getPageRenderState(pageId: string) {
    return request<RenderState>(http.get(`/pages/${pageId}/render-state`))
  },
  getDetectionRuntime() {
    return request<DetectionRuntime>(http.get('/detection/runtime'))
  },
  listDetectionModels() {
    return request<{ items: DetectionModel[]; preparation?: { status: string } }>(
      http.get('/detection-models'),
    )
  },
  getStartupState() {
    return request<{
      core_status: string
      yolo: { status: string; error: string | null }
      timings: Record<string, number>
    }>(http.get('/runtime/startup'))
  },
  uploadDetectionModel(name: string, file: File, onProgress?: (event: AxiosProgressEvent) => void) {
    const data = new FormData()
    data.append('name', name)
    data.append('file', file, file.name)
    return request<DetectionModel>(
      http.post('/detection-models', data, { onUploadProgress: onProgress, timeout: 0 }),
    )
  },
  deleteDetectionModel(id: string) {
    return request<{ deleted: boolean }>(http.delete(`/detection-models/${id}`))
  },
  getDetectionSettings() {
    return request<DetectionSettings>(http.get('/settings/detection'))
  },
  updateDetectionSettings(payload: DetectionSettings) {
    return request<DetectionSettings>(http.put('/settings/detection', payload))
  },
  createDetectionTask(
    projectId: string,
    payload: {
      model_id: string
      start_page: number
      end_page: number
      confidence: number
      image_size: number
      device: 'auto' | 'cpu' | 'cuda'
      overwrite: boolean
    },
  ) {
    return request<{ task_id: string; project_id: string }>(
      http.post(`/projects/${projectId}/detection-tasks`, payload),
    )
  },
  getPageRegions(pageId: string) {
    return request<RegionDocument>(http.get(`/pages/${pageId}/regions`))
  },
  updatePageRegions(pageId: string, expectedRevision: number, regions: DetectionRegion[]) {
    return request<RegionDocument>(
      http.put(`/pages/${pageId}/regions`, {
        expected_revision: expectedRevision,
        regions,
      }),
    )
  },
  reviewPage(pageId: string) {
    return request<RegionDocument>(http.post(`/pages/${pageId}/review`))
  },
  listOCRProviders() {
    return request<{
      items: OCRProviderInfo[]
      automatic: Record<SourceLanguage, Exclude<OCRProviderId, 'auto'>>
    }>(http.get('/providers/ocr'))
  },
  getOCRSettings() {
    return request<OCRSettings>(http.get('/settings/ocr'))
  },
  updateOCRSettings(payload: OCRSettings) {
    return request<OCRSettings>(http.put('/settings/ocr', payload))
  },
  updateProjectOCRProvider(projectId: string, provider: OCRProviderId) {
    return request<{ project_id: string; ocr_provider: OCRProviderId }>(
      http.put(`/projects/${projectId}/ocr-provider`, { provider }),
    )
  },
  listLLMProviders() {
    return request<{ items: LLMProviderInfo[] }>(http.get('/providers/llm'))
  },
  listLLMProfiles() {
    return request<{ items: LLMProfile[] }>(http.get('/llm-profiles'))
  },
  createLLMProfile(payload: {
    name: string
    provider: LLMProviderId
    base_url: string
    model: string
    api_key: string
    temperature: number | null
    max_tokens: number
    timeout_seconds: number
    max_concurrency: number
  }) {
    return request<LLMProfile>(http.post('/llm-profiles', payload))
  },
  updateLLMProfile(id: string, payload: Record<string, unknown>) {
    return request<LLMProfile>(http.put(`/llm-profiles/${id}`, payload))
  },
  deleteLLMProfile(id: string) {
    return request<{ deleted: boolean }>(http.delete(`/llm-profiles/${id}`))
  },
  testLLMProfile(id: string) {
    return request<{ provider: string; model: string; latency_ms: number | null }>(
      http.post(`/llm-profiles/${id}/test`),
    )
  },
  getTranslationSettings() {
    return request<TranslationSettings>(http.get('/settings/translation'))
  },
  updateTranslationSettings(payload: TranslationSettings) {
    return request<TranslationSettings>(http.put('/settings/translation', payload))
  },
  updateProjectLLMProfile(projectId: string, profileId: string | null) {
    return request<{ project_id: string; llm_profile_id: string | null }>(
      http.put(`/projects/${projectId}/llm-profile`, { profile_id: profileId }),
    )
  },
  createPageTranslationTask(
    pageId: string,
    payload: {
      profile_id?: string | null
      overwrite_completed?: boolean
      overwrite_manual?: boolean
      sfx_strategy?: 'preserve' | 'replace' | 'bilingual'
    } = {},
  ) {
    return request<{ task_id: string; project_id: string; profile_id: string | null }>(
      http.post(`/pages/${pageId}/translate`, payload),
    )
  },
  createRegionTranslationTask(
    regionId: string,
    payload: {
      profile_id?: string | null
      overwrite_completed?: boolean
      overwrite_manual?: boolean
      expected_translation_revision: number
    },
  ) {
    return request<{ task_id: string; project_id: string; profile_id: string | null }>(
      http.post(`/regions/${regionId}/translate`, payload),
    )
  },
  updateRegionTranslationText(regionId: string, text: string, expectedRevision: number) {
    return request<DetectionRegion>(
      http.put(`/regions/${regionId}/translation-text`, {
        text,
        expected_translation_revision: expectedRevision,
      }),
    )
  },
  createPageOCRTask(
    pageId: string,
    payload: {
      provider?: OCRProviderId
      overwrite_completed?: boolean
      overwrite_manual?: boolean
    } = {},
  ) {
    return request<{ task_id: string; project_id: string; provider: string }>(
      http.post(`/pages/${pageId}/ocr`, payload),
    )
  },
  createRegionOCRTask(
    regionId: string,
    payload: {
      overwrite_manual?: boolean
      overwrite_completed?: boolean
      expected_ocr_revision: number
    },
  ) {
    return request<{ task_id: string; project_id: string; provider: string }>(
      http.post(`/regions/${regionId}/ocr`, payload),
    )
  },
  updateRegionOCRText(regionId: string, text: string, expectedOCRRevision: number) {
    return request<DetectionRegion>(
      http.put(`/regions/${regionId}/ocr-text`, {
        text,
        expected_ocr_revision: expectedOCRRevision,
      }),
    )
  },
  async getPreviewBlob(url: string): Promise<Blob> {
    try {
      return (await http.get<Blob>(url.replace(/^\/api/, ''), { responseType: 'blob' })).data
    } catch (error) {
      throw toClientError(error)
    }
  },
}
