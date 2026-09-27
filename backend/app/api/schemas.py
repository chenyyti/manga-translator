from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class SourceLanguage(StrEnum):
    JAPANESE = "ja"
    KOREAN = "ko"
    ENGLISH = "en"


class ImportSourceType(StrEnum):
    SINGLE = "single"
    MULTIPLE = "multiple"
    FOLDER = "folder"
    ZIP = "zip"


class TranslationMode(StrEnum):
    QUICK = "quick"


class OCRProviderId(StrEnum):
    AUTO = "auto"
    MANGAOCR = "mangaocr"
    PADDLEOCR = "paddleocr"


class LLMProviderId(StrEnum):
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai_compatible"
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    CLAUDE = "claude"
    GEMINI = "gemini"


class InpaintingProviderId(StrEnum):
    AUTO = "auto"
    FAST = "fast"
    OPENCV = "opencv"
    LAMA = "lama"


class ExportFormat(StrEnum):
    ZIP = "zip"
    EPUB = "epub"


class PageExportFormat(StrEnum):
    PNG = "png"
    JPG = "jpg"


class RenderOrientation(StrEnum):
    AUTO = "auto"
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


class SFXStrategy(StrEnum):
    PRESERVE = "preserve"
    REPLACE = "replace"
    BILINGUAL = "bilingual"


class ImportSessionCreate(BaseModel):
    project_name: str = Field(min_length=1, max_length=120)
    source_language: SourceLanguage
    target_language: str = "zh-CN"
    translation_mode: TranslationMode = TranslationMode.QUICK
    source_type: ImportSourceType
    ocr_provider: OCRProviderId = OCRProviderId.AUTO
    llm_profile_id: str | None = Field(default=None, min_length=1, max_length=36)
    vlm_profile_id: str | None = Field(default=None, exclude=True)

    @field_validator("project_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("项目名称不能为空")
        return normalized

    @field_validator("target_language")
    @classmethod
    def only_simplified_chinese(cls, value: str) -> str:
        if value != "zh-CN":
            raise ValueError("当前版本目标语言仅支持 zh-CN")
        return value

    @field_validator("vlm_profile_id")
    @classmethod
    def reject_vlm_profile(cls, value: str | None) -> None:
        if value is not None:
            raise ValueError("视觉模型配置已移除")
        return None

class UploadedFileRead(BaseModel):
    id: str
    relative_path: str
    size: int
    sha256: str

    model_config = ConfigDict(from_attributes=True)


class ImportSessionRead(BaseModel):
    id: str
    project_name: str
    source_language: str
    target_language: str
    translation_mode: TranslationMode
    source_type: str
    ocr_provider: str
    llm_profile_id: str | None = None
    status: str
    total_bytes: int
    expires_at: datetime
    files: list[UploadedFileRead]

    model_config = ConfigDict(from_attributes=True)


class ImportCommitRead(BaseModel):
    project_id: str
    task_id: str


class ProjectSummary(BaseModel):
    id: str
    name: str
    source_language: str
    target_language: str
    translation_mode: TranslationMode
    ocr_provider: str
    llm_profile_id: str | None = None
    render_status: str = "pending"
    status: str
    total_pages: int
    cover_page_id: str | None
    cover_thumbnail_url: str | None
    last_read_page_id: str | None = None
    last_read_page_index: int | None = None
    cover_is_fallback: bool = False
    continue_url: str | None = None
    active_task_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ProjectDetail(ProjectSummary):
    warning_pages: int = 0


class PageSummary(BaseModel):
    id: str
    page_index: int
    source_filename: str
    import_status: str
    detection_status: str
    ocr_status: str
    translation_status: str
    repair_status: str = "pending"
    render_status: str = "pending"
    thumbnail_url: str | None
    preview_url: str | None
    rendered_thumbnail_url: str | None = None
    rendered_thumbnail_width: int | None = None
    rendered_thumbnail_height: int | None = None


class PageDetail(PageSummary):
    width: int | None
    height: int | None
    preview_width: int | None
    preview_height: int | None
    sha256: str | None
    repair_input_revision: int = 0
    repair_revision: int = 0
    render_input_revision: int = 0
    render_revision: int = 0
    rendered_width: int | None = None
    rendered_height: int | None = None


class ReaderPageSummary(BaseModel):
    id: str
    page_index: int
    source_filename: str
    import_status: str
    render_status: str
    readable: bool
    rendered_url: str | None = None
    rendered_thumbnail_url: str | None = None
    missing_reason: str | None = None
    is_cover: bool = False


class ReaderManifest(BaseModel):
    project_id: str
    project_name: str
    readable: bool = True
    total_pages: int
    readable_pages: int
    render_status: str
    cover_page_id: str | None
    cover_is_fallback: bool = False
    last_read_page_id: str | None = None
    last_read_page_index: int | None = None
    first_page_id: str | None = None
    first_page_index: int | None = None


class ReadingProgressWrite(BaseModel):
    page_id: str


class ReadingProgressRead(BaseModel):
    project_id: str
    page_id: str | None
    page_index: int | None
    updated_at: datetime


class CoverPageWrite(BaseModel):
    page_id: str


class CoverPageRead(BaseModel):
    project_id: str
    cover_page_id: str
    cover_thumbnail_url: str


class PaginatedProjects(BaseModel):
    items: list[ProjectSummary]
    total: int
    offset: int
    limit: int


class PaginatedPages(BaseModel):
    items: list[PageSummary]
    total: int
    offset: int
    limit: int


class TaskSnapshot(BaseModel):
    id: str
    project_id: str
    task_type: str
    status: str
    stage: str
    total: int
    completed: int
    failed: int
    skipped: int = 0
    current_page_id: str | None
    cancel_requested: bool
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    pause_requested: bool = False
    can_pause: bool = False
    can_resume: bool = False
    active_page_id: str | None = None
    parent_task_id: str | None = None
    retry_of_task_id: str | None = None
    task_revision: int = 0
    queue_position: int | None = None
    stage_counts: dict[str, dict[str, int]] = Field(default_factory=dict)


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


BatchTranslationMode = TranslationMode


class BatchReviewPolicy(StrEnum):
    REVIEWED_ONLY = "reviewed_only"
    ALLOW_DETECTED = "allow_detected"


class BatchTaskPreviewRequest(BaseModel):
    start_page: int | None = Field(default=None, ge=1)
    end_page: int | None = Field(default=None, ge=1)
    translation_mode: BatchTranslationMode = BatchTranslationMode.QUICK
    run_ocr: bool = True
    run_translation: bool = True
    run_render: bool = True
    # Compatibility-only input. A non-null value is rejected by the batch
    # API so older clients cannot mistakenly assume YOLO is still included.
    detection: dict[str, Any] | None = Field(default=None, exclude=True)
    # Kept for old clients. Detection review no longer gates processing.
    review_policy: BatchReviewPolicy = BatchReviewPolicy.REVIEWED_ONLY
    overwrite_model_results: bool = False
    overwrite_manual_ocr: bool = False
    overwrite_manual_translation: bool = False
    llm_profile_id: str | None = None
    vlm_profile_id: str | None = None


class BatchTaskCreate(BatchTaskPreviewRequest):
    confirm_allow_detected: bool = False


class PerformanceSettingsRead(BaseModel):
    revision: int = 0
    batch_concurrency: int = 2
    pipeline_window: int = 6
    ocr_concurrency: int = 2
    llm_concurrency: int = 3
    yolo_concurrency: int = 1
    lama_concurrency: int = 1
    render_concurrency: int = 1


class PerformanceSettingsWrite(BaseModel):
    expected_revision: int = Field(default=0, ge=0)
    batch_concurrency: int = Field(default=2, ge=1, le=8)
    pipeline_window: int = Field(default=6, ge=1, le=32)
    ocr_concurrency: int = Field(default=2, ge=1, le=8)
    llm_concurrency: int = Field(default=3, ge=1, le=16)


class RetryFailedTaskWrite(BaseModel):
    item_ids: list[str] | None = Field(default=None, max_length=500)


class TaskItemStageRead(BaseModel):
    stage: str
    status: str
    child_task_id: str | None = None
    retry_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskItemRead(BaseModel):
    id: str
    page_id: str | None
    region_id: str | None = None
    page_index: int
    sequence_index: int = 0
    item_type: str = "region"
    status: str
    current_stage: str | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    retry_count: int = 0
    stages: list[TaskItemStageRead] = Field(default_factory=list)


class DetectionRegionInput(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    class_id: int = Field(ge=0)
    class_name: str = Field(min_length=1, max_length=120)
    confidence: float | None = Field(default=None, ge=0, le=1)
    x1: float = Field(ge=0)
    y1: float = Field(ge=0)
    x2: float = Field(gt=0)
    y2: float = Field(gt=0)
    source: str = Field(default="manual", pattern="^(model|manual)$")
    is_manual_edited: bool = False


class LLMProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: LLMProviderId
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=64, le=32768)
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    max_concurrency: int = Field(default=3, ge=1, le=16)

    @field_validator("name", "model")
    @classmethod
    def normalize_text_field(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("字段不能为空")
        return value


class LLMProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    provider: LLMProviderId | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None, min_length=1)
    clear_api_key: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=64, le=32768)
    timeout_seconds: int | None = Field(default=None, ge=5, le=600)
    max_concurrency: int | None = Field(default=None, ge=1, le=16)

    @field_validator("name", "model")
    @classmethod
    def normalize_optional_text_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("字段不能为空")
        return value


class LLMProfileRead(BaseModel):
    id: str
    name: str
    provider: str
    base_url: str
    model: str
    temperature: float | None
    max_tokens: int
    timeout_seconds: int
    max_concurrency: int
    api_key_hint: str | None
    has_api_key: bool
    created_at: datetime
    updated_at: datetime


class RenderSettingsValues(BaseModel):
    repair_mode: InpaintingProviderId = InpaintingProviderId.AUTO
    device: str = Field(default="auto", pattern="^(auto|cpu|cuda)$")
    mask_padding_ratio: float = Field(default=0.04, ge=0, le=0.25)
    mask_dilation_px: int = Field(default=2, ge=0, le=64)
    opencv_radius: float = Field(default=3, ge=0.1, le=20)
    lama_max_edge: int = Field(default=2048, ge=512, le=8192)
    ai_fallback: bool = True
    font_id: str = Field(default="system:msyh.ttc", min_length=1, max_length=255)
    font_size: int = Field(default=36, ge=6, le=512)
    auto_font_size: bool = True
    min_font_size: int = Field(default=12, ge=6, le=512)
    max_font_size: int = Field(default=96, ge=6, le=512)
    margin_ratio: float = Field(default=0.08, ge=0, le=0.45)
    font_color: str = Field(default="#000000", pattern="^#[0-9A-Fa-f]{6}$")
    stroke_color: str = Field(default="#FFFFFF", pattern="^#[0-9A-Fa-f]{6}$")
    stroke_width: int = Field(default=0, ge=0, le=64)
    orientation: RenderOrientation = RenderOrientation.AUTO
    rotation_degrees: float = Field(default=0, ge=-180, le=180)

    @field_validator("font_id")
    @classmethod
    def normalize_font_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字体不能为空")
        return value

    @field_validator("max_font_size")
    @classmethod
    def max_size_not_smaller_than_min(cls, value: int, info: Any) -> int:
        minimum = info.data.get("min_font_size")
        if minimum is not None and value < minimum:
            raise ValueError("最大字号不能小于最小字号")
        return value


class RenderSettingsRead(RenderSettingsValues):
    revision: int = 0


class RenderSettingsWrite(RenderSettingsValues):
    expected_revision: int = Field(default=0, ge=0)


class RenderOverrides(BaseModel):
    repair_mode: InpaintingProviderId | None = None
    device: str | None = Field(default=None, pattern="^(auto|cpu|cuda)$")
    mask_padding_ratio: float | None = Field(default=None, ge=0, le=0.25)
    mask_dilation_px: int | None = Field(default=None, ge=0, le=64)
    opencv_radius: float | None = Field(default=None, ge=0.1, le=20)
    lama_max_edge: int | None = Field(default=None, ge=512, le=8192)
    ai_fallback: bool | None = None
    font_id: str | None = Field(default=None, min_length=1, max_length=255)
    font_size: int | None = Field(default=None, ge=6, le=512)
    auto_font_size: bool | None = None
    min_font_size: int | None = Field(default=None, ge=6, le=512)
    max_font_size: int | None = Field(default=None, ge=6, le=512)
    margin_ratio: float | None = Field(default=None, ge=0, le=0.45)
    font_color: str | None = Field(default=None, pattern="^#[0-9A-Fa-f]{6}$")
    stroke_color: str | None = Field(default=None, pattern="^#[0-9A-Fa-f]{6}$")
    stroke_width: int | None = Field(default=None, ge=0, le=64)
    orientation: RenderOrientation | None = None
    rotation_degrees: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("font_id")
    @classmethod
    def normalize_override_font_id(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("max_font_size")
    @classmethod
    def override_max_size_not_smaller(cls, value: int | None, info: Any) -> int | None:
        minimum = info.data.get("min_font_size")
        if value is not None and minimum is not None and value < minimum:
            raise ValueError("最大字号不能小于最小字号")
        return value


class RenderOverridesWrite(BaseModel):
    expected_revision: int = Field(default=0, ge=0)
    overrides: RenderOverrides = Field(default_factory=RenderOverrides)


class FontRead(BaseModel):
    id: str
    display_name: str
    family_name: str | None
    source: str
    filename: str
    size: int
    status: str
    deletable: bool


class RepairTaskCreate(BaseModel):
    provider: InpaintingProviderId | None = None
    device: str | None = Field(default=None, pattern="^(auto|cpu|cuda)$")
    expected_repair_input_revision: int | None = Field(default=None, ge=0)
    force: bool = False


class RenderTaskCreate(BaseModel):
    provider: InpaintingProviderId | None = None
    device: str | None = Field(default=None, pattern="^(auto|cpu|cuda)$")
    expected_render_input_revision: int | None = Field(default=None, ge=0)
    force_repair: bool = False


class TranslationSettingsWrite(BaseModel):
    default_profile_id: str | None = None
    sfx_strategy: SFXStrategy = SFXStrategy.PRESERVE
    sfx_class_names: list[str] = Field(
        default_factory=lambda: [
            "sfx",
            "sound_effect",
            "sound-effect",
            "onomatopoeia",
            "拟声词",
            "効果音",
        ],
        max_length=30,
    )

    @field_validator("sfx_class_names")
    @classmethod
    def normalize_sfx_names(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(item.split()) for item in value if item.strip()]
        if not cleaned:
            raise ValueError("至少需要一个 SFX 类别名称")
        return list(dict.fromkeys(cleaned))


class TranslationTextWrite(BaseModel):
    text: str = Field(max_length=10_000)
    expected_translation_revision: int = Field(ge=0)

    @field_validator("text")
    @classmethod
    def normalize_translation(cls, value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()


class TranslationTaskCreate(BaseModel):
    profile_id: str | None = None
    overwrite_completed: bool = False
    overwrite_manual: bool = False
    sfx_strategy: SFXStrategy | None = None
    expected_translation_revision: int | None = Field(default=None, ge=0)


class ProjectLLMProfileWrite(BaseModel):
    profile_id: str | None = None


class OCRSettingsWrite(BaseModel):
    japanese_provider: OCRProviderId = OCRProviderId.AUTO
    korean_provider: OCRProviderId = OCRProviderId.AUTO
    english_provider: OCRProviderId = OCRProviderId.AUTO
    device: str = Field(default="auto", pattern="^(auto|cpu|cuda)$")

class OCRTaskCreate(BaseModel):
    provider: OCRProviderId | None = None
    overwrite_completed: bool = False
    overwrite_manual: bool = False
    expected_ocr_revision: int | None = Field(default=None, ge=0)


class OCRTextWrite(BaseModel):
    text: str = Field(max_length=10_000)
    expected_ocr_revision: int = Field(ge=0)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()


class ProjectOCRProviderWrite(BaseModel):
    provider: OCRProviderId


class DetectionRegionsWrite(BaseModel):
    expected_revision: int = Field(ge=0)
    regions: list[DetectionRegionInput] = Field(max_length=2000)


class DetectionTaskCreate(BaseModel):
    model_id: str
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    confidence: float = Field(default=0.25, ge=0.01, le=1)
    image_size: int = Field(default=1280, ge=320, le=4096)
    device: str = Field(default="auto", pattern="^(auto|cpu|cuda)$")
    overwrite: bool = False


class DetectionSettingsWrite(BaseModel):
    default_model_id: str | None = None
    device: str = Field(default="auto", pattern="^(auto|cpu|cuda)$")
    confidence: float = Field(default=0.25, ge=0.01, le=1)
    image_size: int = Field(default=1280, ge=320, le=4096)


class RuntimeSettingsRead(BaseModel):
    app_name: str
    version: str
    data_dir: str
    database_path: str
    thumbnail_size: int
    preview_size: int
    local_only: bool = True


class LauncherSessionHeartbeatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)


class LauncherSessionRead(BaseModel):
    managed: bool
    session_id: str | None
    heartbeat_interval_seconds: int
    heartbeat_timeout_seconds: int


class LauncherSessionStatusRead(BaseModel):
    managed: bool
    active: bool


class ProjectExportCreate(BaseModel):
    format: ExportFormat


class ExportArtifactRead(BaseModel):
    id: str
    project_id: str
    task_id: str | None = None
    format: ExportFormat
    status: str
    filename: str
    size: int = 0
    sha256: str | None = None
    page_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    download_url: str | None = None


class HealthIssueRead(BaseModel):
    code: str
    severity: str
    message: str
    page_id: str | None = None
    page_index: int | None = None
    action: str | None = None


class HealthReportRead(BaseModel):
    project_id: str
    status: str
    checked_at: datetime
    counts: dict[str, int]
    checks: list[HealthIssueRead]
    exportable: dict[str, bool]


class SuccessEnvelope(BaseModel):
    success: bool = True
    data: Any
