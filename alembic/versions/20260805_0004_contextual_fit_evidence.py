"""persist contextual fit confidence and interest likelihood

Revision ID: 20260805_0004
Revises: 20260805_0003
"""

from alembic import op
import sqlalchemy as sa


revision = "20260805_0004"
down_revision = "20260805_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("recommendation_items") as batch:
        batch.add_column(sa.Column("confidence_score", sa.Float(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("interest_likelihood", sa.Float(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("recommendation_items") as batch:
        batch.drop_column("interest_likelihood")
        batch.drop_column("confidence_score")
