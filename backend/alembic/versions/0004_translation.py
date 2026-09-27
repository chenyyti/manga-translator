"""LLM profiles, translation metadata and quick translation task revisions."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_translation"
down_revision: str | None = "0003_ocr"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "api_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("profile_type", sa.String(16), nullable=False, server_default="llm"),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="2048"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("api_key_hint", sa.String(16), nullable=True),
        sa.Column("credential_target", sa.String(255), nullable=False, unique=True),
        *_timestamps(),
    )
    op.create_index("idx_api_profiles_type_updated", "api_profiles", ["profile_type", "updated_at"])
    op.create_index("idx_api_profiles_name", "api_profiles", ["name"])
    op.create_table(
        "translation_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("default_profile_id", sa.String(36), sa.ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sfx_strategy", sa.String(24), nullable=False, server_default="preserve"),
        sa.Column(
            "sfx_class_names_json",
            sa.Text(),
            nullable=False,
            server_default='["sfx", "sound_effect", "sound-effect", "onomatopoeia", "拟声词", "効果音"]',
        ),
        *_timestamps(),
    )
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("llm_profile_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_projects_llm_profile", "api_profiles", ["llm_profile_id"], ["id"], ondelete="SET NULL"
        )
    with op.batch_alter_table("import_sessions") as batch:
        batch.add_column(sa.Column("llm_profile_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_import_sessions_llm_profile",
            "api_profiles",
            ["llm_profile_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("detection_regions") as batch:
        batch.add_column(sa.Column("target_text", sa.Text(), nullable=True))
        batch.add_column(sa.Column("target_text_origin", sa.String(16), nullable=True))
        batch.add_column(
            sa.Column("translation_status", sa.String(24), nullable=False, server_default="pending")
        )
        batch.add_column(sa.Column("llm_profile_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("llm_provider", sa.String(32), nullable=True))
        batch.add_column(sa.Column("llm_model", sa.String(200), nullable=True))
        batch.add_column(sa.Column("translation_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("reading_order", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("sfx_strategy", sa.String(24), nullable=True))
        batch.add_column(
            sa.Column("translation_revision", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("translated_from_ocr_revision", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("translation_updated_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_detection_regions_llm_profile",
            "api_profiles",
            ["llm_profile_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("task_items") as batch:
        batch.add_column(
            sa.Column("expected_output_revision", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("task_items") as batch:
        batch.drop_column("expected_output_revision")
    with op.batch_alter_table("detection_regions") as batch:
        batch.drop_constraint("fk_detection_regions_llm_profile", type_="foreignkey")
        batch.drop_column("translation_updated_at")
        batch.drop_column("translated_from_ocr_revision")
        batch.drop_column("translation_revision")
        batch.drop_column("sfx_strategy")
        batch.drop_column("reading_order")
        batch.drop_column("translation_error")
        batch.drop_column("llm_model")
        batch.drop_column("llm_provider")
        batch.drop_column("llm_profile_id")
        batch.drop_column("translation_status")
        batch.drop_column("target_text_origin")
        batch.drop_column("target_text")
    with op.batch_alter_table("import_sessions") as batch:
        batch.drop_constraint("fk_import_sessions_llm_profile", type_="foreignkey")
        batch.drop_column("llm_profile_id")
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("fk_projects_llm_profile", type_="foreignkey")
        batch.drop_column("llm_profile_id")
    op.drop_table("translation_settings")
    op.drop_index("idx_api_profiles_name", table_name="api_profiles")
    op.drop_index("idx_api_profiles_type_updated", table_name="api_profiles")
    op.drop_table("api_profiles")
