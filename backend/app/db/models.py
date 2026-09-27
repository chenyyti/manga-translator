from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


def uuid_str() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_language: Mapped[str] = mapped_column(String(8), nullable=False)
    target_language: Mapped[str] = mapped_column(String(8), nullable=False, default="zh-CN")
    translation_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="quick", server_default="quick"
    )
    ocr_provider: Mapped[str] = mapped_column(String(24), nullable=False, default="auto")
    llm_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    vlm_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    context_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    render_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="importing")
    cover_page_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    total_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    workspace_path: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    pages: Mapped[list[Page]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    tasks: Mapped[list[Task]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    llm_profile: Mapped[APIProfile | None] = relationship(
        foreign_keys=[llm_profile_id], back_populates="projects"
    )
    vlm_profile: Mapped[APIProfile | None] = relationship(
        foreign_keys=[vlm_profile_id], back_populates="vlm_projects"
    )

    __table_args__ = (Index("idx_projects_status_updated", "status", "updated_at"),)


class Page(TimestampMixin, Base):
    __tablename__ = "pages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    original_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    preview_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    rendered_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    rendered_thumbnail_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    rendered_thumbnail_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rendered_thumbnail_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preview_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preview_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    import_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    detection_status: Mapped[str] = mapped_column(String(24), nullable=False, default="undetected")
    region_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_detection_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_detection_model_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("detection_models.id", ondelete="SET NULL"), nullable=True
    )
    ocr_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    translation_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    refinement_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    vlm_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    vlm_input_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    vlm_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    render_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    repair_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    repair_input_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    repair_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mask_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    inpainted_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    repair_methods_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    repair_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    render_input_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    render_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    render_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    rendered_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rendered_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rendered_height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    project: Mapped[Project] = relationship(back_populates="pages")
    task_items: Mapped[list[TaskItem]] = relationship(back_populates="page")
    regions: Mapped[list[DetectionRegion]] = relationship(
        back_populates="page", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("project_id", "page_index", name="uq_pages_project_index"),
        Index("idx_pages_project_index", "project_id", "page_index"),
        Index("idx_pages_project_import_status", "project_id", "import_status"),
    )


class ReadingProgress(TimestampMixin, Base):
    """The last page opened in the local reader for a project."""

    __tablename__ = "reading_progress"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    last_page_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pages.id", ondelete="SET NULL"), nullable=True
    )
    last_page_index: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (Index("idx_reading_progress_page", "last_page_id"),)


class ExportArtifact(TimestampMixin, Base):
    """A durable project archive produced by the Phase 9 export worker.

    The database stores only metadata and a path relative to the project
    workspace.  The archive itself is always written atomically below the
    project's ``export`` directory.
    """

    __tablename__ = "export_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_export_artifacts_project_created", "project_id", "created_at"),
        Index("idx_export_artifacts_status", "status"),
        Index("idx_export_artifacts_task", "task_id"),
        Index(
            "uq_export_artifacts_active_project_format",
            "project_id",
            "format",
            unique=True,
            sqlite_where=text("status IN ('pending', 'running', 'pausing', 'paused')"),
        ),
    )


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    import_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("import_sessions.id", ondelete="SET NULL"), nullable=True
    )
    llm_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    vlm_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    task_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    stage: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parameters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_page_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parent_task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    retry_of_task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    task_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="tasks")
    items: Mapped[list[TaskItem]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )
    parent: Mapped[Task | None] = relationship(
        remote_side="Task.id", foreign_keys=[parent_task_id], back_populates="children"
    )
    children: Mapped[list[Task]] = relationship(
        foreign_keys=[parent_task_id], back_populates="parent"
    )
    retry_of: Mapped[Task | None] = relationship(
        remote_side="Task.id", foreign_keys=[retry_of_task_id], back_populates="retries"
    )
    retries: Mapped[list[Task]] = relationship(
        foreign_keys=[retry_of_task_id], back_populates="retry_of"
    )

    __table_args__ = (Index("idx_tasks_status_updated", "status", "updated_at"),)


class TaskItem(TimestampMixin, Base):
    __tablename__ = "task_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    page_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pages.id", ondelete="SET NULL"), nullable=True
    )
    region_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("detection_regions.id", ondelete="SET NULL"), nullable=True
    )
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False, default="region")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    current_stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_content_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_output_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_vlm_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_context_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_repair_input_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_repair_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_render_input_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped[Task] = relationship(back_populates="items")
    page: Mapped[Page | None] = relationship(back_populates="task_items")
    stages: Mapped[list[TaskItemStage]] = relationship(
        back_populates="task_item", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("idx_task_items_task_status", "task_id", "status"),
        Index("idx_task_items_task_sequence", "task_id", "sequence_index"),
    )


class TaskItemStage(TimestampMixin, Base):
    __tablename__ = "task_item_stages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    task_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_items.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    child_task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task_item: Mapped[TaskItem] = relationship(back_populates="stages")
    child_task: Mapped[Task | None] = relationship(foreign_keys=[child_task_id])

    __table_args__ = (
        UniqueConstraint("task_item_id", "stage", name="uq_task_item_stage"),
        Index("idx_task_item_stages_status", "status"),
    )


class ImportSession(TimestampMixin, Base):
    __tablename__ = "import_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    project_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_language: Mapped[str] = mapped_column(String(8), nullable=False)
    target_language: Mapped[str] = mapped_column(String(8), nullable=False, default="zh-CN")
    translation_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="quick", server_default="quick"
    )
    ocr_provider: Mapped[str] = mapped_column(String(24), nullable=False, default="auto")
    llm_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    vlm_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="uploading")
    total_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    files: Mapped[list[UploadedFile]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("idx_import_sessions_status_expires", "status", "expires_at"),)


class UploadedFile(TimestampMixin, Base):
    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("import_sessions.id", ondelete="CASCADE"), nullable=False
    )
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    session: Mapped[ImportSession] = relationship(back_populates="files")

    __table_args__ = (
        UniqueConstraint("session_id", "relative_path", name="uq_upload_session_relative_path"),
        Index("idx_uploaded_files_session", "session_id"),
    )


class DetectionModel(TimestampMixin, Base):
    __tablename__ = "detection_models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ready")
    class_names_json: Mapped[str] = mapped_column(Text, nullable=False)
    framework_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    task_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    storage_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="data")
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="uploaded")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_mtime_ns: Mapped[str | None] = mapped_column(String(32), nullable=True)
    validation_environment: Mapped[str | None] = mapped_column(String(64), nullable=True)
    validation_version: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DetectionSettings(TimestampMixin, Base):
    __tablename__ = "detection_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    default_model_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("detection_models.id", ondelete="SET NULL"), nullable=True
    )
    device: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.25)
    image_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1280)


class OCRSettings(TimestampMixin, Base):
    __tablename__ = "ocr_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    japanese_provider: Mapped[str] = mapped_column(String(24), nullable=False, default="mangaocr")
    korean_provider: Mapped[str] = mapped_column(String(24), nullable=False, default="paddleocr")
    english_provider: Mapped[str] = mapped_column(String(24), nullable=False, default="paddleocr")
    device: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")


class APIProfile(TimestampMixin, Base):
    __tablename__ = "api_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    profile_type: Mapped[str] = mapped_column(String(16), nullable=False, default="llm")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=2048)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    image_max_edge: Mapped[str | None] = mapped_column(String(16), nullable=True)
    api_key_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    credential_target: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    projects: Mapped[list[Project]] = relationship(
        foreign_keys=[Project.llm_profile_id], back_populates="llm_profile"
    )
    vlm_projects: Mapped[list[Project]] = relationship(
        foreign_keys=[Project.vlm_profile_id], back_populates="vlm_profile"
    )

    __table_args__ = (
        Index("idx_api_profiles_type_updated", "profile_type", "updated_at"),
        Index("idx_api_profiles_name", "name"),
    )


class TranslationSettings(TimestampMixin, Base):
    __tablename__ = "translation_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    default_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    sfx_strategy: Mapped[str] = mapped_column(String(24), nullable=False, default="preserve")
    sfx_class_names_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='["sfx", "sound_effect", "sound-effect", "onomatopoeia", "拟声词", "効果音"]',
    )

    default_profile: Mapped[APIProfile | None] = relationship()


class VLMSettings(TimestampMixin, Base):
    __tablename__ = "vlm_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    default_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    summary_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    character_name_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.90)
    default_image_max_edge: Mapped[str] = mapped_column(String(16), nullable=False, default="1600")

    default_profile: Mapped[APIProfile | None] = relationship()


class RenderSettings(TimestampMixin, Base):
    __tablename__ = "render_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    repair_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    device: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    mask_padding_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.04)
    mask_dilation_px: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    opencv_radius: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)
    lama_max_edge: Mapped[int] = mapped_column(Integer, nullable=False, default=2048)
    ai_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    font_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default="system:msyh.ttc"
    )
    font_size: Mapped[int] = mapped_column(Integer, nullable=False, default=36)
    auto_font_size: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    min_font_size: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    max_font_size: Mapped[int] = mapped_column(Integer, nullable=False, default=96)
    margin_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.08)
    font_color: Mapped[str] = mapped_column(String(16), nullable=False, default="#000000")
    stroke_color: Mapped[str] = mapped_column(String(16), nullable=False, default="#FFFFFF")
    stroke_width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orientation: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    rotation_degrees: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class PerformanceSettings(TimestampMixin, Base):
    __tablename__ = "performance_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    batch_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    pipeline_window: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    ocr_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    vlm_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    llm_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=3)


class Font(TimestampMixin, Base):
    __tablename__ = "fonts"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    family_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="custom")
    relative_path: Mapped[str | None] = mapped_column(String(500), nullable=True, unique=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ready")

    __table_args__ = (Index("idx_fonts_source_name", "source", "display_name"),)


class PageRenderSettings(TimestampMixin, Base):
    __tablename__ = "page_render_settings"

    page_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pages.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overrides_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class RegionRenderSettings(TimestampMixin, Base):
    __tablename__ = "region_render_settings"

    region_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("detection_regions.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overrides_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class DetectionRegion(TimestampMixin, Base):
    __tablename__ = "detection_regions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    page_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pages.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("detection_models.id", ondelete="SET NULL"), nullable=True
    )
    class_id: Mapped[int] = mapped_column(Integer, nullable=False)
    class_name: Mapped[str] = mapped_column(String(120), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    x1: Mapped[float] = mapped_column(Float, nullable=False)
    y1: Mapped[float] = mapped_column(Float, nullable=False)
    x2: Mapped[float] = mapped_column(Float, nullable=False)
    y2: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="model")
    is_manual_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_text_origin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ocr_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    ocr_provider: Mapped[str | None] = mapped_column(String(24), nullable=True)
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ocr_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    geometry_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ocr_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ocr_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_text_origin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    translation_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    llm_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    llm_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    translation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reading_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sfx_strategy: Mapped[str | None] = mapped_column(String(24), nullable=True)
    translation_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    translated_from_ocr_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    translation_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    speaker_character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
    )
    speaker_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    emotion: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reading_order_origin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    translation_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    translated_from_vlm_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    translated_from_context_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)

    page: Mapped[Page] = relationship(back_populates="regions")
    speaker_character: Mapped[Character | None] = relationship()

    __table_args__ = (Index("idx_detection_regions_page", "page_id"),)


class VLMAnalysis(TimestampMixin, Base):
    __tablename__ = "vlm_analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    page_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pages.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    scene: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dialogue_context: Mapped[str] = mapped_column(Text, nullable=False, default="")
    relationships_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    ambiguities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    dialogue_order_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    page_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    analysis_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    context_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    page: Mapped[Page] = relationship()


class Character(TimestampMixin, Base):
    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    character_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    visual_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    avatar_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    first_seen_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    merged_into_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("project_id", "character_uid", name="uq_characters_project_uid"),
        Index("idx_characters_project", "project_id", "last_seen_page"),
    )


class CharacterAlias(TimestampMixin, Base):
    __tablename__ = "character_aliases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="manual")

    __table_args__ = (UniqueConstraint("character_id", "alias", name="uq_character_alias"),)


class CharacterNameCandidate(TimestampMixin, Base):
    __tablename__ = "character_name_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
    )
    page_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pages.id", ondelete="SET NULL"), nullable=True
    )
    candidate_name: Mapped[str] = mapped_column(String(200), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    __table_args__ = (Index("idx_character_candidates_character", "character_id", "status"),)


class CharacterAppearance(TimestampMixin, Base):
    __tablename__ = "character_appearances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
    )
    page_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pages.id", ondelete="CASCADE"), nullable=False
    )
    x1: Mapped[float | None] = mapped_column(Float, nullable=True)
    y1: Mapped[float | None] = mapped_column(Float, nullable=True)
    x2: Mapped[float | None] = mapped_column(Float, nullable=True)
    y2: Mapped[float | None] = mapped_column(Float, nullable=True)
    emotion: Mapped[str | None] = mapped_column(String(120), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (UniqueConstraint("character_id", "page_id", name="uq_character_appearance"),)


class ChapterSummary(TimestampMixin, Base):
    __tablename__ = "chapter_summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    from_page: Mapped[int] = mapped_column(Integer, nullable=False)
    to_page: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="completed")

    __table_args__ = (
        UniqueConstraint("project_id", "from_page", "to_page", name="uq_chapter_summary_range"),
        Index("idx_chapter_summaries_project_to", "project_id", "to_page"),
    )
