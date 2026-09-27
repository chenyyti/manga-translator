export type SourceLanguage = 'ja' | 'ko' | 'en'
export type ImportSourceType = 'single' | 'multiple' | 'folder' | 'zip'
export type ProjectTranslationMode = 'quick'
export type OCRProviderId = 'auto' | 'mangaocr' | 'paddleocr'
export type LLMProviderId =
  'openai' | 'openai_compatible' | 'deepseek' | 'qwen' | 'claude' | 'gemini'
export type InpaintingProviderId = 'auto' | 'fast' | 'opencv' | 'lama'
export type ExportFormat = 'zip' | 'epub'
export type PageExportFormat = 'png' | 'jpg'
export type RenderOrientation = 'auto' | 'horizontal' | 'vertical'
export type TranslationStatus =
  | 'pending'
  | 'processing'
  | 'completed'
  | 'completed_with_warnings'
  | 'manual'
  | 'failed'
  | 'outdated'
export type OCRStatus =
  | 'pending'
  | 'processing'
  | 'completed'
  | 'completed_with_warnings'
  | 'manual'
  | 'failed'
  | 'outdated'

export interface Envelope<T> {
  success: true
  data: T
}

export interface ApiFailure {
  success: false
  error: {
    code: string
    message: string
    request_id?: string
    details?: unknown
  }
}

export interface ProjectSummary {
  id: string
  name: string
  source_language: SourceLanguage
  target_language: 'zh-CN'
  translation_mode: ProjectTranslationMode
  ocr_provider: OCRProviderId
  llm_profile_id?: string | null
  render_status?: 'pending' | 'partial' | 'completed' | 'completed_with_warnings' | 'outdated'
  status: 'importing' | 'ready' | 'ready_with_warnings' | 'import_failed' | 'deleting'
  total_pages: number
  cover_page_id: string | null
  cover_thumbnail_url: string | null
  last_read_page_id?: string | null
  last_read_page_index?: number | null
  cover_is_fallback?: boolean
  continue_url?: string | null
  active_task_id: string | null
  created_at: string
  updated_at: string
}

export interface ProjectDetail extends ProjectSummary {
  warning_pages: number
}

export interface PageSummary {
  id: string
  page_index: number
  source_filename: string
  import_status: 'pending' | 'processing' | 'ready' | 'corrupt' | 'failed'
  detection_status: string
  ocr_status: OCRStatus | string
  translation_status: TranslationStatus | string
  repair_status?: string
  render_status?: string
  region_revision: number
  reviewed_at: string | null
  last_detection_error: string | null
  thumbnail_url: string | null
  preview_url: string | null
  rendered_thumbnail_url?: string | null
  rendered_thumbnail_width?: number | null
  rendered_thumbnail_height?: number | null
}

export interface PageDetail extends PageSummary {
  width: number | null
  height: number | null
  preview_width: number | null
  preview_height: number | null
  sha256: string | null
  last_detection_model_id: string | null
  repair_input_revision?: number
  repair_revision?: number
  render_input_revision?: number
  render_revision?: number
  rendered_width?: number | null
  rendered_height?: number | null
  rendered_thumbnail_width?: number | null
  rendered_thumbnail_height?: number | null
}

export interface ReaderManifest {
  project_id: string
  project_name: string
  readable: boolean
  total_pages: number
  readable_pages: number
  render_status: string
  cover_page_id: string | null
  cover_is_fallback: boolean
  last_read_page_id: string | null
  last_read_page_index: number | null
  first_page_index: number | null
  first_page_id: string | null
}

export interface ReaderPage {
  id: string
  page_index: number
  source_filename: string
  import_status: string
  render_status: string
  readable: boolean
  rendered_url: string | null
  rendered_thumbnail_url: string | null
  missing_reason: 'source_invalid' | 'render_outdated' | 'render_failed' | 'render_missing' | null
  is_cover: boolean
}

export interface Paginated<T> {
  items: T[]
  total: number
  offset: number
  limit: number
}

export interface ImportSession {
  id: string
  project_name: string
  source_language: SourceLanguage
  target_language: 'zh-CN'
  translation_mode: ProjectTranslationMode
  ocr_provider: OCRProviderId
  llm_profile_id: string | null
  source_type: ImportSourceType
  status: string
  total_bytes: number
  expires_at: string
  files: Array<{ id: string; relative_path: string; size: number; sha256: string }>
}

export interface TaskSnapshot {
  id: string
  project_id: string
  task_type: string
  status: 'pending' | 'running' | 'pausing' | 'paused' | 'completed' | 'failed' | 'cancelled'
  stage: string
  total: number
  completed: number
  failed: number
  skipped: number
  current_page_id: string | null
  cancel_requested: boolean
  error_code: string | null
  error_message: string | null
  created_at: string
  updated_at: string
  pause_requested?: boolean
  can_pause?: boolean
  can_resume?: boolean
  active_page_id?: string | null
  parent_task_id?: string | null
  retry_of_task_id?: string | null
  task_revision?: number
  queue_position?: number | null
  stage_counts?: Record<string, Record<string, number>>
}

export type BatchTranslationMode = ProjectTranslationMode
export type BatchReviewPolicy = 'reviewed_only' | 'allow_detected'

export interface BatchTaskPreviewRequest {
  start_page?: number | null
  end_page?: number | null
  translation_mode?: BatchTranslationMode
  run_ocr: boolean
  run_translation: boolean
  run_render: boolean
  review_policy: BatchReviewPolicy
  overwrite_model_results: boolean
  overwrite_manual_ocr: boolean
  overwrite_manual_translation: boolean
  llm_profile_id?: string | null
}

export interface BatchTaskPreview {
  project_id: string
  start_page: number
  end_page: number
  translation_mode: BatchTranslationMode
  eligible_pages: number
  skipped_pages: number
  estimated_ocr_requests: number
  estimated_llm_requests: number
  estimated_render_requests: number
  region_count: number
  items: Array<Record<string, unknown>>
  skipped: Array<Record<string, unknown>>
  warnings: string[]
}

export interface TaskItemStage {
  stage: string
  status: string
  child_task_id: string | null
  retry_count: number
  error_code: string | null
  error_message: string | null
  started_at: string | null
  finished_at: string | null
}

export interface TaskItemRead {
  id: string
  page_id: string | null
  region_id?: string | null
  page_index: number
  sequence_index?: number
  item_type?: string
  status: string
  current_stage: string | null
  error_code: string | null
  error_message: string | null
  started_at?: string | null
  finished_at?: string | null
  retry_count?: number
  stages: TaskItemStage[]
}

export interface PerformanceSettings {
  revision: number
  batch_concurrency: number
  pipeline_window: number
  ocr_concurrency: number
  llm_concurrency: number
  yolo_concurrency: number
  lama_concurrency: number
  render_concurrency: number
}

export interface ExportArtifact {
  id: string
  project_id: string
  task_id: string | null
  format: ExportFormat
  status: string
  filename: string
  size: number
  sha256: string | null
  page_count: number
  error_code: string | null
  error_message: string | null
  created_at: string
  updated_at: string
  download_url: string | null
}

export interface HealthIssue {
  code: string
  severity: 'error' | 'warning' | string
  message: string
  page_id: string | null
  page_index: number | null
  action: string | null
}

export interface HealthReport {
  project_id: string
  status: 'healthy' | 'warning' | 'error' | string
  checked_at: string
  counts: { errors: number; warnings: number; ok: number }
  checks: HealthIssue[]
  exportable: { png: boolean; jpg: boolean; zip: boolean; epub: boolean }
}

export interface DetectionRegion {
  id: string
  model_id?: string | null
  class_id: number
  class_name: string
  confidence: number | null
  x1: number
  y1: number
  x2: number
  y2: number
  source: 'model' | 'manual'
  is_manual_edited: boolean
  source_text: string | null
  source_text_origin: 'provider' | 'manual' | null
  ocr_status: OCRStatus
  ocr_provider: Exclude<OCRProviderId, 'auto'> | null
  ocr_confidence: number | null
  ocr_error: string | null
  geometry_revision: number
  ocr_revision: number
  ocr_updated_at: string | null
  target_text?: string | null
  target_text_origin?: 'provider' | 'manual' | 'sfx_preserve' | null
  translation_status?: TranslationStatus
  llm_profile_id?: string | null
  llm_provider?: LLMProviderId | null
  llm_model?: string | null
  translation_error?: string | null
  reading_order?: number | null
  sfx_strategy?: 'preserve' | 'replace' | 'bilingual' | null
  translation_revision?: number
  translated_from_ocr_revision?: number | null
  translation_updated_at?: string | null
  reading_order_origin?: 'detector' | 'manual' | null
  translation_mode?: 'quick' | 'manual' | null
}

export interface RegionDocument {
  page_id: string
  revision: number
  detection_status: string
  ocr_status: OCRStatus | string
  translation_status?: TranslationStatus | string
  reviewed_at: string | null
  regions: DetectionRegion[]
}

export interface DetectionModel {
  id: string
  name: string
  filename: string
  sha256: string
  size: number
  status: string
  class_names: Record<string, string>
  framework_version: string | null
  task_name: string | null
  source: 'project' | 'data'
  storage_scope: 'project' | 'data'
  relative_path: string
  readonly: boolean
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface DetectionSettings {
  default_model_id: string | null
  device: 'auto' | 'cpu' | 'cuda'
  confidence: number
  image_size: number
}

export interface DetectionRuntime {
  ultralytics_installed: boolean
  torch_installed: boolean
  ultralytics_version: string | null
  torch_version: string | null
  cuda_available: boolean
  gpu_name: string | null
  total_vram_mb: number | null
  architecture: string
  gpu_fallback: boolean
}

export interface OCRProviderInfo {
  id: Exclude<OCRProviderId, 'auto'>
  name: string
  supported_languages: SourceLanguage[]
  installed: boolean
  version: string | null
  model_ready: boolean
  model_names: string[]
  cuda_available: boolean
  architecture: 'isolated_process'
}

export interface OCRSettings {
  japanese_provider: OCRProviderId
  korean_provider: OCRProviderId
  english_provider: OCRProviderId
  device: 'auto' | 'cpu' | 'cuda'
}

export interface RuntimeSettings {
  app_name: string
  version: string
  data_dir: string
  database_path: string
  models_dir: string
  ocr_models_dir: string
  inpainting_models_dir: string
  yolo_models_dir: string
  model_source: 'project'
  thumbnail_size: number
  preview_size: number
  local_only: boolean
}

export interface LauncherSession {
  managed: boolean
  session_id: string | null
  heartbeat_interval_seconds: number
  heartbeat_timeout_seconds: number
}

export interface InpaintingProviderInfo {
  id: InpaintingProviderId
  name: string
  installed: boolean
  version: string | null
  model_ready: boolean
  model_name: string | null
  available_devices: string[]
  description: string
}

export interface RenderSettings {
  revision: number
  repair_mode: InpaintingProviderId
  device: 'auto' | 'cpu' | 'cuda'
  mask_padding_ratio: number
  mask_dilation_px: number
  opencv_radius: number
  lama_max_edge: number
  ai_fallback: boolean
  font_id: string
  font_size: number
  auto_font_size: boolean
  min_font_size: number
  max_font_size: number
  margin_ratio: number
  font_color: string
  stroke_color: string
  stroke_width: number
  orientation: RenderOrientation
  rotation_degrees: number
}

export interface FontInfo {
  id: string
  display_name: string
  family_name: string | null
  source: 'system' | 'custom'
  filename: string
  size: number
  status: string
  deletable: boolean
}

export interface RenderState {
  page_id: string
  repair_status: string
  repair_input_revision: number
  repair_revision: number
  mask_url: string | null
  inpainted_url: string | null
  render_status: string
  render_input_revision: number
  render_revision: number
  rendered_url: string | null
  rendered_sha256: string | null
  rendered_width: number | null
  rendered_height: number | null
  repair_methods: Record<string, unknown>
  repair_error: string | null
  render_error: string | null
  settings: Record<string, unknown>
}

export interface LLMProviderInfo {
  id: LLMProviderId
  name: string
  supported_languages: SourceLanguage[]
  default_base_url: string | null
  installed: boolean
  version: string | null
  protocol: string
  model_ready: boolean
  available_devices: string[]
}

export interface LLMProfile {
  id: string
  name: string
  provider: LLMProviderId
  base_url: string
  model: string
  temperature: number | null
  max_tokens: number
  timeout_seconds: number
  max_concurrency: number
  api_key_hint: string | null
  has_api_key: boolean
  created_at: string
  updated_at: string
}

export interface TranslationSettings {
  default_profile_id: string | null
  sfx_strategy: 'preserve' | 'replace' | 'bilingual'
  sfx_class_names: string[]
}
