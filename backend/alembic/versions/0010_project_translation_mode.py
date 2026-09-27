"""Persist the immutable translation mode selected for each project."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_project_translation_mode"
down_revision: str | None = "0009_export_health"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column("translation_mode", sa.String(16), nullable=False, server_default="quick")
        )
    with op.batch_alter_table("import_sessions") as batch:
        batch.add_column(
            sa.Column(
                "translation_mode", sa.String(16), nullable=False, server_default="quick"
            )
        )

    # Preserve the only reliable signal of intent available to projects that
    # predate the project-level mode. A VLM analysis, character/context data,
    # or a refined region means that hiding the visual workflow would lose an
    # existing capability. Projects without those artifacts remain quick.
    op.execute(
        sa.text(
            """
            UPDATE projects
            SET translation_mode = 'refined'
            WHERE EXISTS (
                SELECT 1
                FROM pages
                WHERE pages.project_id = projects.id
                  AND (
                    pages.vlm_revision > 0
                    OR pages.vlm_status <> 'pending'
                    OR pages.refinement_status <> 'pending'
                  )
            )
            OR EXISTS (
                SELECT 1
                FROM vlm_analyses
                JOIN pages ON pages.id = vlm_analyses.page_id
                WHERE pages.project_id = projects.id
            )
            OR EXISTS (
                SELECT 1
                FROM characters
                WHERE characters.project_id = projects.id
            )
            OR EXISTS (
                SELECT 1
                FROM chapter_summaries
                WHERE chapter_summaries.project_id = projects.id
            )
            OR EXISTS (
                SELECT 1
                FROM detection_regions
                JOIN pages ON pages.id = detection_regions.page_id
                WHERE pages.project_id = projects.id
                  AND detection_regions.translation_mode = 'refined'
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE import_sessions
            SET translation_mode = CASE
                WHEN vlm_profile_id IS NOT NULL THEN 'refined'
                ELSE 'quick'
            END
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("import_sessions") as batch:
        batch.drop_column("translation_mode")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("translation_mode")
