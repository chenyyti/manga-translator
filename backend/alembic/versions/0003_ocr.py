"""OCR providers, region text and resumable region task metadata."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_ocr"
down_revision: str | None = "0002_detection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column("ocr_provider", sa.String(24), nullable=False, server_default="auto")
        )
    with op.batch_alter_table("import_sessions") as batch:
        batch.add_column(
            sa.Column("ocr_provider", sa.String(24), nullable=False, server_default="auto")
        )
    with op.batch_alter_table("detection_regions") as batch:
        batch.add_column(sa.Column("source_text", sa.Text(), nullable=True))
        batch.add_column(sa.Column("source_text_origin", sa.String(16), nullable=True))
        batch.add_column(
            sa.Column("ocr_status", sa.String(24), nullable=False, server_default="pending")
        )
        batch.add_column(sa.Column("ocr_provider", sa.String(24), nullable=True))
        batch.add_column(sa.Column("ocr_confidence", sa.Float(), nullable=True))
        batch.add_column(sa.Column("ocr_error", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("geometry_revision", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("ocr_revision", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("ocr_updated_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("task_items") as batch:
        batch.drop_constraint("uq_task_items_task_page_index", type_="unique")
        batch.add_column(sa.Column("region_id", sa.String(36), nullable=True))
        batch.add_column(
            sa.Column("sequence_index", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("expected_content_revision", sa.Integer(), nullable=False, server_default="0")
        )
        batch.create_foreign_key(
            "fk_task_items_region",
            "detection_regions",
            ["region_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.execute("UPDATE task_items SET sequence_index = page_index")
    with op.batch_alter_table("task_items") as batch:
        batch.create_index("idx_task_items_task_sequence", ["task_id", "sequence_index"])
        batch.create_index("idx_task_items_region", ["region_id"])
    op.create_table(
        "ocr_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("japanese_provider", sa.String(24), nullable=False),
        sa.Column("korean_provider", sa.String(24), nullable=False),
        sa.Column("english_provider", sa.String(24), nullable=False),
        sa.Column("device", sa.String(16), nullable=False),
        *_timestamps(),
    )


def downgrade() -> None:
    op.drop_table("ocr_settings")
    # Phase 2 only allowed one task item per page. Remove Phase 3 OCR tasks before
    # restoring that uniqueness constraint so a downgrade stays deterministic.
    op.execute("DELETE FROM task_items WHERE task_id IN (SELECT id FROM tasks WHERE task_type = 'ocr')")
    op.execute("DELETE FROM tasks WHERE task_type = 'ocr'")
    with op.batch_alter_table("task_items") as batch:
        batch.drop_index("idx_task_items_region")
        batch.drop_index("idx_task_items_task_sequence")
        batch.drop_constraint("fk_task_items_region", type_="foreignkey")
        batch.drop_column("expected_content_revision")
        batch.drop_column("sequence_index")
        batch.drop_column("region_id")
        batch.create_unique_constraint(
            "uq_task_items_task_page_index", ["task_id", "page_index"]
        )
    with op.batch_alter_table("detection_regions") as batch:
        batch.drop_column("ocr_updated_at")
        batch.drop_column("ocr_revision")
        batch.drop_column("geometry_revision")
        batch.drop_column("ocr_error")
        batch.drop_column("ocr_confidence")
        batch.drop_column("ocr_provider")
        batch.drop_column("ocr_status")
        batch.drop_column("source_text_origin")
        batch.drop_column("source_text")
    with op.batch_alter_table("import_sessions") as batch:
        batch.drop_column("ocr_provider")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("ocr_provider")
