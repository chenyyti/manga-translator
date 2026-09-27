"""Phase 9 durable exports and project health metadata."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_export_health"
down_revision: str | None = "0008_reader"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "export_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "idx_export_artifacts_project_created",
        "export_artifacts",
        ["project_id", "created_at"],
    )
    op.create_index("idx_export_artifacts_status", "export_artifacts", ["status"])
    op.create_index("idx_export_artifacts_task", "export_artifacts", ["task_id"])
    op.create_index(
        "uq_export_artifacts_active_project_format",
        "export_artifacts",
        ["project_id", "format"],
        unique=True,
        sqlite_where=sa.text("status IN ('pending', 'running', 'pausing', 'paused')"),
    )


def downgrade() -> None:
    op.drop_index("uq_export_artifacts_active_project_format", table_name="export_artifacts")
    op.drop_index("idx_export_artifacts_task", table_name="export_artifacts")
    op.drop_index("idx_export_artifacts_status", table_name="export_artifacts")
    op.drop_index("idx_export_artifacts_project_created", table_name="export_artifacts")
    op.drop_table("export_artifacts")
