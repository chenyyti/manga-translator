"""Phase 1 project, page, import and task schema."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_phase1"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("source_language", sa.String(8), nullable=False),
        sa.Column("target_language", sa.String(8), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cover_page_id", sa.String(36), nullable=True),
        sa.Column("total_pages", sa.Integer(), nullable=False),
        sa.Column("workspace_path", sa.String(255), nullable=False, unique=True),
        *_timestamps(),
    )
    op.create_index("idx_projects_status_updated", "projects", ["status", "updated_at"])

    op.create_table(
        "import_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_name", sa.String(120), nullable=False),
        sa.Column("source_language", sa.String(8), nullable=False),
        sa.Column("target_language", sa.String(8), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("total_bytes", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
    )
    op.create_index(
        "idx_import_sessions_status_expires", "import_sessions", ["status", "expires_at"]
    )

    op.create_table(
        "pages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_index", sa.Integer(), nullable=False),
        sa.Column("source_filename", sa.String(500), nullable=False),
        sa.Column("original_path", sa.String(500), nullable=True),
        sa.Column("preview_path", sa.String(500), nullable=True),
        sa.Column("thumbnail_path", sa.String(500), nullable=True),
        sa.Column("rendered_path", sa.String(500), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("preview_width", sa.Integer(), nullable=True),
        sa.Column("preview_height", sa.Integer(), nullable=True),
        sa.Column("thumbnail_width", sa.Integer(), nullable=True),
        sa.Column("thumbnail_height", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("import_status", sa.String(24), nullable=False),
        sa.Column("detection_status", sa.String(24), nullable=False),
        sa.Column("ocr_status", sa.String(24), nullable=False),
        sa.Column("translation_status", sa.String(24), nullable=False),
        sa.Column("render_status", sa.String(24), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("project_id", "page_index", name="uq_pages_project_index"),
    )
    op.create_index("idx_pages_project_index", "pages", ["project_id", "page_index"])
    op.create_index("idx_pages_project_import_status", "pages", ["project_id", "import_status"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("import_session_id", sa.String(36), sa.ForeignKey("import_sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("task_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("completed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("current_page_id", sa.String(36), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("idx_tasks_status_updated", "tasks", ["status", "updated_at"])

    op.create_table(
        "uploaded_files",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("import_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("stored_path", sa.String(500), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("session_id", "relative_path", name="uq_upload_session_relative_path"),
    )
    op.create_index("idx_uploaded_files_session", "uploaded_files", ["session_id"])

    op.create_table(
        "task_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_id", sa.String(36), sa.ForeignKey("pages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_path", sa.String(500), nullable=False),
        sa.Column("page_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("task_id", "page_index", name="uq_task_items_task_page_index"),
    )
    op.create_index("idx_task_items_task_status", "task_items", ["task_id", "status"])


def downgrade() -> None:
    op.drop_table("task_items")
    op.drop_table("uploaded_files")
    op.drop_table("tasks")
    op.drop_table("pages")
    op.drop_table("import_sessions")
    op.drop_table("projects")
