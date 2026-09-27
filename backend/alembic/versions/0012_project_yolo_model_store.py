"""Add project-scoped YOLO model metadata and validation errors."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_project_yolo_model_store"
down_revision: str | None = "0011_fixed_ocr_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("detection_models") as batch:
        batch.add_column(
            sa.Column("storage_scope", sa.String(16), nullable=False, server_default="data")
        )
        batch.add_column(
            sa.Column("origin", sa.String(16), nullable=False, server_default="uploaded")
        )
        batch.add_column(sa.Column("error_message", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("detection_models") as batch:
        batch.drop_column("error_message")
        batch.drop_column("origin")
        batch.drop_column("storage_scope")
