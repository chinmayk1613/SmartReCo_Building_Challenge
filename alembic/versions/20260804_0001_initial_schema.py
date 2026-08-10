"""Create the frozen SmartReco schema at revision 20260804_0001.

This historical migration is intentionally independent of the evolving ORM
metadata. Revisions 0002 through 0006 add observability, contextual scope,
fit evidence, LangSmith reconciliation, and vector provenance respectively.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260804_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("timezone", sa.String(length=80), nullable=False),
        sa.Column("personalization_enabled", sa.Boolean(), nullable=False),
        sa.Column("digest_enabled", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_role", "users", ["role"])

    op.create_table(
        "products",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=False),
        sa.Column("level", sa.String(length=40), nullable=False),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("outcomes", sa.JSON(), nullable=False),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Float(), nullable=False),
        sa.Column("popularity", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_checksum", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_products_category", "products", ["category"])
    op.create_index("ix_products_content_checksum", "products", ["content_checksum"])
    op.create_index("ix_products_level", "products", ["level"])
    op.create_index("ix_products_slug", "products", ["slug"], unique=True)
    op.create_index("ix_products_status", "products", ["status"])
    op.create_index("ix_products_title", "products", ["title"])

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])
    op.create_index("ix_user_sessions_token_hash", "user_sessions", ["token_hash"], unique=True)
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])

    op.create_table(
        "catalog_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("product_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "product_version", "event_type", name="uq_product_outbox_version"),
    )
    op.create_index("ix_catalog_outbox_available_at", "catalog_outbox", ["available_at"])
    op.create_index("ix_catalog_outbox_product_id", "catalog_outbox", ["product_id"])
    op.create_index("ix_catalog_outbox_status", "catalog_outbox", ["status"])

    op.create_table(
        "product_vector_state",
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("product_version", sa.Integer(), nullable=False),
        sa.Column("point_id", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=160), nullable=False),
        sa.Column("content_checksum", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("product_id"),
    )
    op.create_index("ix_product_vector_state_status", "product_vector_state", ["status"])

    op.create_table(
        "activity_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=True),
        sa.Column("search_query", sa.String(length=500), nullable=True),
        sa.Column("category", sa.String(length=120), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("page_path", sa.String(length=500), nullable=True),
        sa.Column("recommendation_id", sa.String(length=36), nullable=True),
        sa.Column("event_properties", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_activity_events_event_id", "activity_events", ["event_id"], unique=True)
    op.create_index("ix_activity_events_event_type", "activity_events", ["event_type"])
    op.create_index("ix_activity_events_occurred_at", "activity_events", ["occurred_at"])
    op.create_index("ix_activity_events_product_id", "activity_events", ["product_id"])
    op.create_index("ix_activity_events_received_at", "activity_events", ["received_at"])
    op.create_index("ix_activity_events_recommendation_id", "activity_events", ["recommendation_id"])
    op.create_index("ix_activity_events_session_id", "activity_events", ["session_id"])
    op.create_index("ix_activity_events_user_id", "activity_events", ["user_id"])
    op.create_index("ix_events_product_type", "activity_events", ["product_id", "event_type"])
    op.create_index("ix_events_user_received", "activity_events", ["user_id", "received_at"])

    op.create_table(
        "behavioral_signals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("signal_type", sa.String(length=60), nullable=False),
        sa.Column("topic", sa.String(length=160), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=True),
        sa.Column("strength", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_event_ids", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_behavioral_signals_last_observed_at", "behavioral_signals", ["last_observed_at"])
    op.create_index("ix_behavioral_signals_session_id", "behavioral_signals", ["session_id"])
    op.create_index("ix_behavioral_signals_signal_type", "behavioral_signals", ["signal_type"])
    op.create_index("ix_behavioral_signals_topic", "behavioral_signals", ["topic"])
    op.create_index("ix_behavioral_signals_user_id", "behavioral_signals", ["user_id"])

    op.create_table(
        "user_interest_profiles",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("source_event_cursor", sa.Integer(), nullable=False),
        sa.Column("primary_intent", sa.String(length=160), nullable=True),
        sa.Column("secondary_intents", sa.JSON(), nullable=False),
        sa.Column("category_weights", sa.JSON(), nullable=False),
        sa.Column("topic_weights", sa.JSON(), nullable=False),
        sa.Column("recent_searches", sa.JSON(), nullable=False),
        sa.Column("positive_product_ids", sa.JSON(), nullable=False),
        sa.Column("negative_product_ids", sa.JSON(), nullable=False),
        sa.Column("journey_stage", sa.String(length=60), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("trigger_score", sa.Float(), nullable=False),
        sa.Column("profile_hash", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index("ix_user_interest_profiles_profile_hash", "user_interest_profiles", ["profile_hash"])

    op.create_table(
        "recommendation_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("trigger_type", sa.String(length=80), nullable=False),
        sa.Column("trigger_reason", sa.String(length=500), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("profile_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("current_node", sa.String(length=80), nullable=True),
        sa.Column("graph_state", sa.JSON(), nullable=False),
        sa.Column("retrieval_metrics", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost", sa.Float(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recommendation_runs_idempotency_key", "recommendation_runs", ["idempotency_key"], unique=True)
    op.create_index("ix_recommendation_runs_status", "recommendation_runs", ["status"])
    op.create_index("ix_recommendation_runs_user_id", "recommendation_runs", ["user_id"])

    op.create_table(
        "recommendations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("headline", sa.String(length=240), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("profile_snapshot", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["recommendation_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_recommendations_status", "recommendations", ["status"])
    op.create_index("ix_recommendations_user_id", "recommendations", ["user_id"])

    op.create_table(
        "recommendation_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("recommendation_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("semantic_score", sa.Float(), nullable=False),
        sa.Column("behavior_score", sa.Float(), nullable=False),
        sa.Column("final_score", sa.Float(), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("product_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recommendation_id"], ["recommendations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recommendation_id", "rank", name="uq_recommendation_rank"),
    )
    op.create_index("ix_recommendation_items_product_id", "recommendation_items", ["product_id"])
    op.create_index("ix_recommendation_items_recommendation_id", "recommendation_items", ["recommendation_id"])

    op.create_table(
        "recommendation_exposures",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("recommendation_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("displayed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recommendation_id"], ["recommendations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recommendation_exposures_recommendation_id", "recommendation_exposures", ["recommendation_id"])
    op.create_index("ix_recommendation_exposures_user_id", "recommendation_exposures", ["user_id"])

    op.create_table(
        "deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("recommendation_id", sa.String(length=36), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("provider_receipt", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["recommendation_id"], ["recommendations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_deliveries_scheduled_for", "deliveries", ["scheduled_for"])
    op.create_index("ix_deliveries_status", "deliveries", ["status"])
    op.create_index("ix_deliveries_user_id", "deliveries", ["user_id"])

    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("delivery_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("provider_status", sa.String(length=120), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["delivery_id"], ["deliveries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_delivery_attempts_delivery_id", "delivery_attempts", ["delivery_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("object_type", sa.String(length=80), nullable=False),
        sa.Column("object_id", sa.String(length=80), nullable=True),
        sa.Column("audit_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("delivery_attempts")
    op.drop_table("deliveries")
    op.drop_table("recommendation_exposures")
    op.drop_table("recommendation_items")
    op.drop_table("recommendations")
    op.drop_table("recommendation_runs")
    op.drop_table("user_interest_profiles")
    op.drop_table("behavioral_signals")
    op.drop_table("activity_events")
    op.drop_table("product_vector_state")
    op.drop_table("catalog_outbox")
    op.drop_table("user_sessions")
    op.drop_table("products")
    op.drop_table("users")
