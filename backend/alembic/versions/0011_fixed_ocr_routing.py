"""Fix OCR routing to the single supported provider for each language."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_fixed_ocr_routing"
down_revision: str | None = "0010_project_translation_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fixed_provider_case(column: str) -> str:
    return f"""
        CASE {column}
            WHEN 'ja' THEN 'mangaocr'
            WHEN 'ko' THEN 'paddleocr'
            WHEN 'en' THEN 'paddleocr'
            ELSE 'auto'
        END
    """


def upgrade() -> None:
    op.execute(
        sa.text(
            f"UPDATE projects SET ocr_provider = {_fixed_provider_case('source_language')}"
        )
    )
    op.execute(
        sa.text(
            f"UPDATE import_sessions SET ocr_provider = {_fixed_provider_case('source_language')}"
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ocr_settings
            SET japanese_provider = 'mangaocr',
                korean_provider = 'paddleocr',
                english_provider = 'paddleocr'
            """
        )
    )


def downgrade() -> None:
    # The former values cannot be reconstructed after fixed routing has been
    # applied.  Reset them to the pre-migration automatic sentinel so the
    # previous resolver can resume its configurable behaviour.
    op.execute("UPDATE projects SET ocr_provider = 'auto'")
    op.execute("UPDATE import_sessions SET ocr_provider = 'auto'")
    op.execute(
        sa.text(
            """
            UPDATE ocr_settings
            SET japanese_provider = 'auto',
                korean_provider = 'auto',
                english_provider = 'auto'
            """
        )
    )
