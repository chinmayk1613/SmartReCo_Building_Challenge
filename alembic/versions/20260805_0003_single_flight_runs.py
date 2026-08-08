"""add recommendation scope and enforce one active run per learner scope

Revision ID: 20260805_0003
Revises: 20260804_0002
"""

from alembic import op
import sqlalchemy as sa


revision = "20260805_0003"
down_revision = "20260804_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("recommendation_runs") as batch:
        batch.add_column(sa.Column("scope_key", sa.String(length=100), nullable=False, server_default="overall"))
        batch.add_column(sa.Column("context_product_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_recommendation_runs_context_product",
            "products",
            ["context_product_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index("ix_recommendation_runs_scope_key", "recommendation_runs", ["scope_key"])
    op.create_index("ix_recommendation_runs_context_product_id", "recommendation_runs", ["context_product_id"])
    with op.batch_alter_table("recommendations") as batch:
        batch.add_column(sa.Column("recommendation_type", sa.String(length=30), nullable=False, server_default="overall"))
        batch.add_column(sa.Column("context_product_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_recommendations_context_product",
            "products",
            ["context_product_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index("ix_recommendations_recommendation_type", "recommendations", ["recommendation_type"])
    op.create_index("ix_recommendations_context_product_id", "recommendations", ["context_product_id"])
    # Older builds could start duplicate workflows milliseconds apart. Retain the
    # newest active run and close older duplicates before enforcing single-flight.
    op.execute(
        sa.text(
            """
            WITH ranked_active AS (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY user_id, scope_key ORDER BY created_at DESC, id DESC
                ) AS position
                FROM recommendation_runs
                WHERE status IN ('queued', 'running')
            )
            UPDATE recommendation_runs
            SET status = 'failed',
                error_code = 'SupersededDuplicateRun',
                error_detail = 'Closed while enabling atomic single-flight workflow execution.'
            WHERE id IN (SELECT id FROM ranked_active WHERE position > 1)
            """
        )
    )
    op.create_index(
        "uq_recommendation_runs_user_active",
        "recommendation_runs",
        ["user_id", "scope_key"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_recommendation_runs_user_active", table_name="recommendation_runs")
    op.drop_index("ix_recommendations_context_product_id", table_name="recommendations")
    op.drop_index("ix_recommendations_recommendation_type", table_name="recommendations")
    with op.batch_alter_table("recommendations") as batch:
        batch.drop_constraint("fk_recommendations_context_product", type_="foreignkey")
        batch.drop_column("context_product_id")
        batch.drop_column("recommendation_type")
    op.drop_index("ix_recommendation_runs_context_product_id", table_name="recommendation_runs")
    op.drop_index("ix_recommendation_runs_scope_key", table_name="recommendation_runs")
    with op.batch_alter_table("recommendation_runs") as batch:
        batch.drop_constraint("fk_recommendation_runs_context_product", type_="foreignkey")
        batch.drop_column("context_product_id")
        batch.drop_column("scope_key")
