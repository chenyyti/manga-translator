"""Detection models, regions, settings and resumable task metadata."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_detection"
down_revision: str | None = "0001_phase1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "detection_models",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("class_names_json", sa.Text(), nullable=False),
        sa.Column("framework_version", sa.String(40), nullable=True),
        sa.Column("task_name", sa.String(40), nullable=True),
        *_timestamps(),
    )
    op.create_table(
        "detection_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "default_model_id",
            sa.String(36),
            sa.ForeignKey("detection_models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("device", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("image_size", sa.Integer(), nullable=False),
        *_timestamps(),
    )
    with op.batch_alter_table("pages") as batch:
        batch.add_column(sa.Column("region_revision", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_detection_error", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column(
                "last_detection_model_id",
                sa.String(36),
                nullable=True,
            )
        )
        batch.create_foreign_key(
            "fk_pages_last_detection_model",
            "detection_models",
            ["last_detection_model_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("parameters_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("result_json", sa.Text(), nullable=True))
    with op.batch_alter_table("task_items") as batch:
        batch.add_column(
            sa.Column("expected_revision", sa.Integer(), nullable=False, server_default="0")
        )
    op.create_table(
        "detection_regions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "page_id",
            sa.String(36),
            sa.ForeignKey("pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "model_id",
            sa.String(36),
            sa.ForeignKey("detection_models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("class_id", sa.Integer(), nullable=False),
        sa.Column("class_name", sa.String(120), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("x1", sa.Float(), nullable=False),
        sa.Column("y1", sa.Float(), nullable=False),
        sa.Column("x2", sa.Float(), nullable=False),
        sa.Column("y2", sa.Float(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("is_manual_edited", sa.Boolean(), nullable=False),
        *_timestamps(),
    )
    op.create_index("idx_detection_regions_page", "detection_regions", ["page_id"])


def downgrade() -> None:
    op.drop_table("detection_regions")
    with op.batch_alter_table("task_items") as batch:
        batch.drop_column("expected_revision")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("result_json")
        batch.drop_column("parameters_json")
        batch.drop_column("skipped")
    with op.batch_alter_table("pages") as batch:
        batch.drop_constraint("fk_pages_last_detection_model", type_="foreignkey")
        batch.drop_column("last_detection_model_id")
        batch.drop_column("last_detection_error")
        batch.drop_column("reviewed_at")
        batch.drop_column("region_revision")
    op.drop_table("detection_settings")
    op.drop_table("detection_models")
