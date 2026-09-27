"""Remove the manual detection review business gate."""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_remove_detection_review_gate"
down_revision: str | None = "0012_project_yolo_model_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE pages SET detection_status = 'detected', reviewed_at = NULL "
        "WHERE detection_status = 'reviewed'"
    )


def downgrade() -> None:
    # A migration cannot infer which detected pages had previously been reviewed.
    pass
