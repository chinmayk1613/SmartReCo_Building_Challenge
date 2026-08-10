"""correlate provider attempts with LangSmith spans

Revision ID: 20260805_0005
Revises: 20260805_0004
"""

from alembic import op
import sqlalchemy as sa


revision = "20260805_0005"
down_revision = "20260805_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("service_invocations") as batch:
        batch.add_column(sa.Column("correlation_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("workload", sa.String(length=40), nullable=False, server_default="legacy"))
        batch.add_column(sa.Column("attempt_number", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("provider_receipt", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("failover_decision", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("langsmith_trace_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("langsmith_run_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("langsmith_run_url", sa.Text(), nullable=True))
        batch.add_column(sa.Column("langsmith_export_status", sa.String(length=30), nullable=False, server_default="legacy"))
        batch.add_column(sa.Column("langsmith_last_checked_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("langsmith_exported_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_index("ix_service_invocations_correlation_id", ["correlation_id"], unique=True)
        batch.create_index("ix_service_invocations_workload", ["workload"])
        batch.create_index("ix_service_invocations_langsmith_trace_id", ["langsmith_trace_id"])
        batch.create_index("ix_service_invocations_langsmith_run_id", ["langsmith_run_id"], unique=True)
        batch.create_index("ix_service_invocations_langsmith_export_status", ["langsmith_export_status"])
        batch.create_index("ix_service_invocations_is_demo", ["is_demo"])
        batch.create_index("ix_invocations_export_started", ["langsmith_export_status", "started_at"])

    # Historical rows cannot be honestly converted into one-attempt/one-span evidence.
    # Preserve them as legacy, while keeping the seeded demo trend visibly separate.
    op.execute(
        sa.text(
            "UPDATE service_invocations SET workload='demo', is_demo=1, langsmith_export_status='demo' "
            "WHERE invocation_metadata LIKE '%demo_history_batch%'"
        )
    )

    op.create_table(
        "trace_reconciliation_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("local_correlated_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("remote_provider_spans", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delayed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unmatched_remote_spans", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("legacy_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("demo_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trace_reconciliation_runs_project_name", "trace_reconciliation_runs", ["project_name"])
    op.create_index("ix_trace_reconciliation_runs_status", "trace_reconciliation_runs", ["status"])
    op.create_index("ix_trace_reconciliation_runs_started_at", "trace_reconciliation_runs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_trace_reconciliation_runs_started_at", table_name="trace_reconciliation_runs")
    op.drop_index("ix_trace_reconciliation_runs_status", table_name="trace_reconciliation_runs")
    op.drop_index("ix_trace_reconciliation_runs_project_name", table_name="trace_reconciliation_runs")
    op.drop_table("trace_reconciliation_runs")
    with op.batch_alter_table("service_invocations") as batch:
        batch.drop_index("ix_invocations_export_started")
        batch.drop_index("ix_service_invocations_is_demo")
        batch.drop_index("ix_service_invocations_langsmith_export_status")
        batch.drop_index("ix_service_invocations_langsmith_run_id")
        batch.drop_index("ix_service_invocations_langsmith_trace_id")
        batch.drop_index("ix_service_invocations_workload")
        batch.drop_index("ix_service_invocations_correlation_id")
        batch.drop_column("is_demo")
        batch.drop_column("langsmith_exported_at")
        batch.drop_column("langsmith_last_checked_at")
        batch.drop_column("langsmith_export_status")
        batch.drop_column("langsmith_run_url")
        batch.drop_column("langsmith_run_id")
        batch.drop_column("langsmith_trace_id")
        batch.drop_column("failover_decision")
        batch.drop_column("provider_receipt")
        batch.drop_column("attempt_number")
        batch.drop_column("workload")
        batch.drop_column("correlation_id")
