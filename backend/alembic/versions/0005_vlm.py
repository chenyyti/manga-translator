"""VLM profiles, page analysis, characters, context summaries and refined tasks."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_vlm"
down_revision: str | None = "0004_translation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    with op.batch_alter_table("api_profiles") as batch:
        batch.add_column(sa.Column("image_max_edge", sa.String(16), nullable=True))

    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("vlm_profile_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("context_revision", sa.Integer(), nullable=False, server_default="0"))
        batch.create_foreign_key(
            "fk_projects_vlm_profile", "api_profiles", ["vlm_profile_id"], ["id"], ondelete="SET NULL"
        )
    with op.batch_alter_table("import_sessions") as batch:
        batch.add_column(sa.Column("vlm_profile_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_import_sessions_vlm_profile",
            "api_profiles",
            ["vlm_profile_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("pages") as batch:
        batch.add_column(sa.Column("refinement_status", sa.String(32), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("vlm_status", sa.String(24), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("vlm_input_revision", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("vlm_revision", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("llm_profile_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("vlm_profile_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_tasks_llm_profile", "api_profiles", ["llm_profile_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_foreign_key(
            "fk_tasks_vlm_profile", "api_profiles", ["vlm_profile_id"], ["id"], ondelete="SET NULL"
        )
    with op.batch_alter_table("task_items") as batch:
        batch.add_column(sa.Column("item_type", sa.String(32), nullable=False, server_default="region"))
        batch.add_column(sa.Column("expected_vlm_revision", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("expected_context_revision", sa.Integer(), nullable=True))

    op.create_table(
        "vlm_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("default_profile_id", sa.String(36), sa.ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("summary_interval", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("character_name_threshold", sa.Float(), nullable=False, server_default="0.90"),
        sa.Column("default_image_max_edge", sa.String(16), nullable=False, server_default="1600"),
        *_timestamps(),
    )
    op.create_table(
        "characters",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("character_uid", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("visual_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("avatar_path", sa.String(500), nullable=True),
        sa.Column("first_seen_page", sa.Integer(), nullable=True),
        sa.Column("last_seen_page", sa.Integer(), nullable=True),
        sa.Column("merged_into_id", sa.String(36), sa.ForeignKey("characters.id", ondelete="SET NULL"), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.UniqueConstraint("project_id", "character_uid", name="uq_characters_project_uid"),
    )
    op.create_index("idx_characters_project", "characters", ["project_id", "last_seen_page"])
    op.create_table(
        "character_aliases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alias", sa.String(200), nullable=False),
        sa.Column("source", sa.String(24), nullable=False, server_default="manual"),
        *_timestamps(),
        sa.UniqueConstraint("character_id", "alias", name="uq_character_alias"),
    )
    op.create_table(
        "character_name_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_id", sa.String(36), sa.ForeignKey("pages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("candidate_name", sa.String(200), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        *_timestamps(),
    )
    op.create_index("idx_character_candidates_character", "character_name_candidates", ["character_id", "status"])
    op.create_table(
        "character_appearances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_id", sa.String(36), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("x1", sa.Float(), nullable=True),
        sa.Column("y1", sa.Float(), nullable=True),
        sa.Column("x2", sa.Float(), nullable=True),
        sa.Column("y2", sa.Float(), nullable=True),
        sa.Column("emotion", sa.String(120), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        sa.UniqueConstraint("character_id", "page_id", name="uq_character_appearance"),
    )
    op.create_table(
        "vlm_analyses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("page_id", sa.String(36), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("scene", sa.Text(), nullable=False, server_default=""),
        sa.Column("dialogue_context", sa.Text(), nullable=False, server_default=""),
        sa.Column("relationships_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("ambiguities_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("dialogue_order_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("page_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("analysis_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("context_revision", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
    )
    op.create_table(
        "chapter_summaries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_page", sa.Integer(), nullable=False),
        sa.Column("to_page", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("api_profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(24), nullable=False, server_default="completed"),
        *_timestamps(),
        sa.UniqueConstraint("project_id", "from_page", "to_page", name="uq_chapter_summary_range"),
    )
    op.create_index("idx_chapter_summaries_project_to", "chapter_summaries", ["project_id", "to_page"])
    with op.batch_alter_table("detection_regions") as batch:
        batch.add_column(sa.Column("speaker_character_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("speaker_confidence", sa.Float(), nullable=True))
        batch.add_column(sa.Column("emotion", sa.String(120), nullable=True))
        batch.add_column(sa.Column("reading_order_origin", sa.String(16), nullable=True))
        batch.add_column(sa.Column("translation_mode", sa.String(16), nullable=True))
        batch.add_column(sa.Column("translated_from_vlm_revision", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("translated_from_context_revision", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_detection_regions_speaker_character",
            "characters",
            ["speaker_character_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.execute(
        "UPDATE detection_regions SET translation_mode = "
        "CASE WHEN target_text_origin = 'manual' THEN 'manual' "
        "WHEN target_text_origin = 'provider' THEN 'quick' ELSE NULL END"
    )


def downgrade() -> None:
    with op.batch_alter_table("detection_regions") as batch:
        batch.drop_constraint("fk_detection_regions_speaker_character", type_="foreignkey")
        for column in (
            "translated_from_context_revision",
            "translated_from_vlm_revision",
            "translation_mode",
            "reading_order_origin",
            "emotion",
            "speaker_confidence",
            "speaker_character_id",
        ):
            batch.drop_column(column)
    op.drop_index("idx_chapter_summaries_project_to", table_name="chapter_summaries")
    op.drop_table("chapter_summaries")
    op.drop_table("vlm_analyses")
    op.drop_index("idx_character_candidates_character", table_name="character_name_candidates")
    op.drop_table("character_name_candidates")
    op.drop_table("character_appearances")
    op.drop_table("character_aliases")
    op.drop_index("idx_characters_project", table_name="characters")
    op.drop_table("characters")
    op.drop_table("vlm_settings")
    with op.batch_alter_table("task_items") as batch:
        batch.drop_column("expected_context_revision")
        batch.drop_column("expected_vlm_revision")
        batch.drop_column("item_type")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint("fk_tasks_vlm_profile", type_="foreignkey")
        batch.drop_constraint("fk_tasks_llm_profile", type_="foreignkey")
        batch.drop_column("vlm_profile_id")
        batch.drop_column("llm_profile_id")
    with op.batch_alter_table("pages") as batch:
        batch.drop_column("vlm_revision")
        batch.drop_column("vlm_input_revision")
        batch.drop_column("vlm_status")
        batch.drop_column("refinement_status")
    with op.batch_alter_table("import_sessions") as batch:
        batch.drop_constraint("fk_import_sessions_vlm_profile", type_="foreignkey")
        batch.drop_column("vlm_profile_id")
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("fk_projects_vlm_profile", type_="foreignkey")
        batch.drop_column("context_revision")
        batch.drop_column("vlm_profile_id")
    with op.batch_alter_table("api_profiles") as batch:
        batch.drop_column("image_max_edge")
