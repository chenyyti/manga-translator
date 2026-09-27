"""Phase 7 persistent batch queue and performance settings."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_batch_queue"
down_revision: str | None = "0006_rendering"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("pause_requested", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("parent_task_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("retry_of_task_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("task_revision", sa.Integer(), nullable=False, server_default="0"))
        batch.create_foreign_key("fk_tasks_parent_task", "tasks", ["parent_task_id"], ["id"], ondelete="SET NULL")
        batch.create_foreign_key("fk_tasks_retry_of_task", "tasks", ["retry_of_task_id"], ["id"], ondelete="SET NULL")
        batch.create_index("idx_tasks_parent", ["parent_task_id"])

    with op.batch_alter_table("task_items") as batch:
        batch.add_column(sa.Column("current_stage", sa.String(40), nullable=True))
        batch.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("idx_task_items_stage", ["task_id", "current_stage"])

    op.create_table(
        "task_item_stages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_item_id", sa.String(36), sa.ForeignKey("task_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("child_task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("task_item_id", "stage", name="uq_task_item_stage"),
        sa.Index("idx_task_item_stages_status", "status"),
    )
    op.create_table(
        "performance_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("batch_concurrency", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("pipeline_window", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("ocr_concurrency", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("vlm_concurrency", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("llm_concurrency", sa.Integer(), nullable=False, server_default="3"),
        *_timestamps(),
    )
    # Keep the singleton present immediately after the migration.  The
    # service layer also creates it lazily for databases created by older
    # development builds, but a migrated database should expose the defaults
    # without requiring a first settings request.
    op.execute(
        sa.text(
            "INSERT INTO performance_settings "
            "(id, revision, batch_concurrency, pipeline_window, ocr_concurrency, "
            "vlm_concurrency, llm_concurrency, created_at, updated_at) "
            "VALUES (1, 0, 2, 6, 2, 2, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )


def downgrade() -> None:
    op.drop_table("performance_settings")
    op.drop_table("task_item_stages")
    with op.batch_alter_table("task_items") as batch:
        batch.drop_index("idx_task_items_stage")
        batch.drop_column("finished_at")
        batch.drop_column("started_at")
        batch.drop_column("current_stage")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("idx_tasks_parent")
        batch.drop_constraint("fk_tasks_retry_of_task", type_="foreignkey")
        batch.drop_constraint("fk_tasks_parent_task", type_="foreignkey")
        batch.drop_column("task_revision")
        batch.drop_column("retry_of_task_id")
        batch.drop_column("parent_task_id")
        batch.drop_column("pause_requested")
