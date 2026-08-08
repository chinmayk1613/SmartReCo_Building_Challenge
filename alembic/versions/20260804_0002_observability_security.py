"""add durable observability and authentication attempt audit

Revision ID: 20260804_0002
Revises: 20260804_0001
"""

from alembic import op
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text


revision = "20260804_0002"
down_revision = "20260804_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_attempts",
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("email", String(320), nullable=False),
        Column("ip_address", String(64), nullable=False),
        Column("succeeded", Boolean, nullable=False, default=False),
        Column("attempted_at", DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_attempts_email", "auth_attempts", ["email"])
    op.create_index("ix_auth_attempts_ip_address", "auth_attempts", ["ip_address"])
    op.create_index("ix_auth_attempts_succeeded", "auth_attempts", ["succeeded"])
    op.create_index("ix_auth_attempts_attempted_at", "auth_attempts", ["attempted_at"])
    op.create_table(
        "service_invocations",
        Column("id", String(36), primary_key=True),
        Column("user_id", String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        Column("recommendation_run_id", String(36), ForeignKey("recommendation_runs.id", ondelete="SET NULL"), nullable=True),
        Column("service", String(40), nullable=False),
        Column("operation", String(120), nullable=False),
        Column("status", String(30), nullable=False),
        Column("model", String(160), nullable=True),
        Column("input_tokens", Integer, nullable=False, default=0),
        Column("output_tokens", Integer, nullable=False, default=0),
        Column("estimated_cost", Float, nullable=True),
        Column("latency_ms", Integer, nullable=True),
        Column("invocation_metadata", JSON, nullable=False),
        Column("error_code", String(120), nullable=True),
        Column("error_detail", Text, nullable=True),
        Column("started_at", DateTime(timezone=True), nullable=False),
        Column("completed_at", DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_service_invocations_user_id", "service_invocations", ["user_id"])
    op.create_index("ix_service_invocations_recommendation_run_id", "service_invocations", ["recommendation_run_id"])
    op.create_index("ix_service_invocations_service", "service_invocations", ["service"])
    op.create_index("ix_service_invocations_operation", "service_invocations", ["operation"])
    op.create_index("ix_service_invocations_status", "service_invocations", ["status"])
    op.create_index("ix_service_invocations_model", "service_invocations", ["model"])
    op.create_index("ix_service_invocations_started_at", "service_invocations", ["started_at"])
    op.create_index("ix_invocations_user_started", "service_invocations", ["user_id", "started_at"])
    op.create_index("ix_invocations_service_started", "service_invocations", ["service", "started_at"])


def downgrade() -> None:
    op.drop_table("service_invocations")
    op.drop_table("auth_attempts")
