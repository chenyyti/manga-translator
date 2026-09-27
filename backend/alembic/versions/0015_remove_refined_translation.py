"""Retire refined translation and clear its project data."""

import sqlalchemy as sa

from alembic import op

revision = "0015_remove_refined_translation"
down_revision = "0014_detection_validation_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_cleanup",
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("target", sa.String(500), primary_key=True),
    )
    op.execute("""
        INSERT OR IGNORE INTO feature_cleanup(kind, target)
        SELECT 'credential', credential_target FROM api_profiles WHERE profile_type = 'vlm'
    """)
    for directory in ("masks", "cache/inpainted", "rendered", "export"):
        op.execute(sa.text("""
            INSERT OR IGNORE INTO feature_cleanup(kind, target)
            SELECT 'directory', workspace_path || '/' || :directory
            FROM projects WHERE translation_mode = 'refined'
        """).bindparams(directory=directory))
    op.execute("""
        INSERT OR IGNORE INTO feature_cleanup(kind, target)
        SELECT 'file', characters.avatar_path FROM characters
        JOIN projects ON projects.id = characters.project_id
        WHERE projects.translation_mode = 'refined'
          AND characters.avatar_path LIKE projects.workspace_path || '/%'
    """)

    old = "SELECT id FROM projects WHERE translation_mode = 'refined'"
    pages = f"SELECT id FROM pages WHERE project_id IN ({old})"
    regions = f"SELECT id FROM detection_regions WHERE page_id IN ({pages})"
    tasks = f"SELECT id FROM tasks WHERE project_id IN ({old}) OR task_type IN ('vlm_analysis', 'refined_translation', 'chapter_summary')"
    items = f"SELECT id FROM task_items WHERE task_id IN ({tasks})"

    # Remove task records before the recovery managers start. Detection boxes
    # and OCR data stay in place; every translation and output is reset.
    op.execute(f"DELETE FROM task_item_stages WHERE task_item_id IN ({items}) OR child_task_id IN ({tasks})")
    op.execute(f"DELETE FROM task_items WHERE task_id IN ({tasks})")
    op.execute(f"UPDATE export_artifacts SET task_id = NULL WHERE task_id IN ({tasks})")
    op.execute(f"DELETE FROM tasks WHERE id IN ({tasks})")
    op.execute(f"DELETE FROM export_artifacts WHERE project_id IN ({old})")
    op.execute(f"""
        UPDATE detection_regions SET
            target_text = NULL, target_text_origin = NULL,
            translation_status = 'pending', translation_error = NULL,
            llm_profile_id = NULL, llm_provider = NULL, llm_model = NULL,
            translation_revision = 0, translated_from_ocr_revision = NULL,
            translation_updated_at = NULL, sfx_strategy = NULL,
            speaker_character_id = NULL, speaker_confidence = NULL, emotion = NULL,
            translation_mode = NULL, translated_from_vlm_revision = NULL,
            translated_from_context_revision = NULL,
            reading_order = CASE WHEN reading_order_origin = 'vlm' THEN NULL ELSE reading_order END,
            reading_order_origin = CASE WHEN reading_order_origin = 'vlm' THEN NULL ELSE reading_order_origin END
        WHERE id IN ({regions})
    """)
    op.execute(f"""
        UPDATE pages SET translation_status = 'pending', refinement_status = 'pending',
            vlm_status = 'pending', vlm_input_revision = 0, vlm_revision = 0,
            repair_status = 'pending', repair_input_revision = 0, repair_revision = 0,
            mask_path = NULL, inpainted_path = NULL, repair_methods_json = NULL,
            repair_error = NULL, render_status = 'pending', render_input_revision = 0,
            render_revision = 0, render_error = NULL, rendered_path = NULL,
            rendered_thumbnail_path = NULL, rendered_thumbnail_width = NULL,
            rendered_thumbnail_height = NULL, rendered_sha256 = NULL,
            rendered_width = NULL, rendered_height = NULL
        WHERE id IN ({pages})
    """)
    op.execute(f"DELETE FROM vlm_analyses WHERE page_id IN ({pages})")
    op.execute(f"DELETE FROM chapter_summaries WHERE project_id IN ({old})")
    characters = f"SELECT id FROM characters WHERE project_id IN ({old})"
    for table in ("character_aliases", "character_name_candidates", "character_appearances"):
        op.execute(f"DELETE FROM {table} WHERE character_id IN ({characters})")
    op.execute(f"DELETE FROM characters WHERE id IN ({characters})")
    op.execute(f"UPDATE projects SET translation_mode = 'quick', vlm_profile_id = NULL, context_revision = 0, render_status = 'pending', cover_page_id = NULL WHERE id IN ({old})")
    op.execute("UPDATE import_sessions SET translation_mode = 'quick', vlm_profile_id = NULL WHERE translation_mode = 'refined'")
    op.execute("DELETE FROM vlm_settings")
    op.execute("DELETE FROM api_profiles WHERE profile_type = 'vlm'")


def downgrade() -> None:
    # Removed user data cannot be reconstructed.
    op.drop_table("feature_cleanup")
