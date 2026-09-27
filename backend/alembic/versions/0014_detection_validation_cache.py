"""Persist YOLO validation fingerprints across application restarts."""
import sqlalchemy as sa

from alembic import op

revision = "0014_detection_validation_cache"
down_revision = "0013_remove_detection_review_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("detection_models") as batch:
        batch.add_column(sa.Column("file_mtime_ns", sa.String(32), nullable=True))
        batch.add_column(sa.Column("validation_environment", sa.String(64), nullable=True))
        batch.add_column(sa.Column("validation_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("detection_models") as batch:
        batch.drop_column("validation_version")
        batch.drop_column("validation_environment")
        batch.drop_column("file_mtime_ns")
