"""Phase 6 image repair, typesetting and page rendering state."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_rendering"
down_revision: str | None = "0005_vlm"
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
            sa.Column("render_status", sa.String(32), nullable=False, server_default="pending")
        )

    with op.batch_alter_table("pages") as batch:
        batch.add_column(
            sa.Column("repair_status", sa.String(32), nullable=False, server_default="pending")
        )
        batch.add_column(
            sa.Column("repair_input_revision", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("repair_revision", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("mask_path", sa.String(500), nullable=True))
        batch.add_column(sa.Column("inpainted_path", sa.String(500), nullable=True))
        batch.add_column(sa.Column("repair_methods_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("repair_error", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("render_input_revision", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("render_revision", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("render_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("rendered_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("rendered_width", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("rendered_height", sa.Integer(), nullable=True))

    with op.batch_alter_table("task_items") as batch:
        batch.add_column(
            sa.Column("expected_repair_input_revision", sa.Integer(), nullable=True)
        )
        batch.add_column(sa.Column("expected_repair_revision", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("expected_render_input_revision", sa.Integer(), nullable=True)
        )

    op.create_table(
        "render_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("repair_mode", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("device", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("mask_padding_ratio", sa.Float(), nullable=False, server_default="0.04"),
        sa.Column("mask_dilation_px", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("opencv_radius", sa.Float(), nullable=False, server_default="3"),
        sa.Column("lama_max_edge", sa.Integer(), nullable=False, server_default="2048"),
        sa.Column("ai_fallback", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("font_id", sa.String(255), nullable=False, server_default="system:msyh.ttc"),
        sa.Column("font_size", sa.Integer(), nullable=False, server_default="36"),
        sa.Column("auto_font_size", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("min_font_size", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("max_font_size", sa.Integer(), nullable=False, server_default="96"),
        sa.Column("margin_ratio", sa.Float(), nullable=False, server_default="0.08"),
        sa.Column("font_color", sa.String(16), nullable=False, server_default="#000000"),
        sa.Column("stroke_color", sa.String(16), nullable=False, server_default="#FFFFFF"),
        sa.Column("stroke_width", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("orientation", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("rotation_degrees", sa.Float(), nullable=False, server_default="0"),
        *_timestamps(),
    )
    op.create_table(
        "fonts",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("family_name", sa.String(255), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="custom"),
        sa.Column("relative_path", sa.String(500), nullable=True, unique=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(24), nullable=False, server_default="ready"),
        *_timestamps(),
        sa.Index("idx_fonts_source_name", "source", "display_name"),
    )
    op.create_table(
        "page_render_settings",
        sa.Column(
            "page_id",
            sa.String(36),
            sa.ForeignKey("pages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overrides_json", sa.Text(), nullable=False, server_default="{}"),
        *_timestamps(),
    )
    op.create_table(
        "region_render_settings",
        sa.Column(
            "region_id",
            sa.String(36),
            sa.ForeignKey("detection_regions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overrides_json", sa.Text(), nullable=False, server_default="{}"),
        *_timestamps(),
    )

    # Existing projects have no rendered assets yet.  Keep the historical
    # import/detection/OCR/translation status untouched and only initialise
    # the new aggregate rendering status.
    op.execute("UPDATE projects SET render_status = 'pending' WHERE render_status IS NULL")


def downgrade() -> None:
    op.drop_table("region_render_settings")
    op.drop_table("page_render_settings")
    op.drop_index("idx_fonts_source_name", table_name="fonts")
    op.drop_table("fonts")
    op.drop_table("render_settings")
    with op.batch_alter_table("task_items") as batch:
        for column in (
            "expected_render_input_revision",
            "expected_repair_revision",
            "expected_repair_input_revision",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("pages") as batch:
        for column in (
            "rendered_height",
            "rendered_width",
            "rendered_sha256",
            "render_error",
            "render_revision",
            "render_input_revision",
            "repair_error",
            "repair_methods_json",
            "inpainted_path",
            "mask_path",
            "repair_revision",
            "repair_input_revision",
            "repair_status",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("render_status")
