"""record vector-index provenance and compatibility

Revision ID: 20260807_0006
Revises: 20260805_0005
"""

from alembic import op
import sqlalchemy as sa


revision = "20260807_0006"
down_revision = "20260805_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("product_vector_state") as batch:
        batch.add_column(sa.Column("embedding_provider", sa.String(length=80), nullable=False, server_default="unknown"))
        batch.add_column(sa.Column("vector_dimension", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("index_schema_version", sa.String(length=40), nullable=False, server_default="legacy"))


def downgrade() -> None:
    with op.batch_alter_table("product_vector_state") as batch:
        batch.drop_column("index_schema_version")
        batch.drop_column("vector_dimension")
        batch.drop_column("embedding_provider")
