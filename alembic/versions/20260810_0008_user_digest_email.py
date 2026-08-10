"""add optional external digest email

Revision ID: 20260810_0008
Revises: 20260810_0007
"""

from alembic import op
import sqlalchemy as sa


revision = "20260810_0008"
down_revision = "20260810_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("digest_email", sa.String(length=320), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("digest_email")
