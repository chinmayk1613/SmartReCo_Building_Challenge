"""add per-user daily digest time in GMT

Revision ID: 20260810_0007
Revises: 20260807_0006
"""

from alembic import op
import sqlalchemy as sa


revision = "20260810_0007"
down_revision = "20260807_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("digest_time_gmt", sa.String(length=5), nullable=False, server_default="15:00"))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("digest_time_gmt")
