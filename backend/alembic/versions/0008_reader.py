"""Phase 8 bookshelf, reader progress and rendered cover thumbnails."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_reader"
down_revision: str | None = "0007_batch_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    with op.batch_alter_table("pages") as batch:
        batch.add_column(sa.Column("rendered_thumbnail_path", sa.String(500), nullable=True))
        batch.add_column(sa.Column("rendered_thumbnail_width", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("rendered_thumbnail_height", sa.Integer(), nullable=True))

    op.create_table(
        "reading_progress",
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "last_page_id",
            sa.String(36),
            sa.ForeignKey("pages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_page_index", sa.Integer(), nullable=True),
        *_timestamps(),
    )
    op.create_index("idx_reading_progress_page", "reading_progress", ["last_page_id"])


def downgrade() -> None:
    op.drop_index("idx_reading_progress_page", table_name="reading_progress")
    op.drop_table("reading_progress")
    with op.batch_alter_table("pages") as batch:
        batch.drop_column("rendered_thumbnail_height")
        batch.drop_column("rendered_thumbnail_width")
        batch.drop_column("rendered_thumbnail_path")
