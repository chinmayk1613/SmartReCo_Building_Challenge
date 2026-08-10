from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime as SADateTime, Float, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Normalize every application timestamp to an aware UTC datetime."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Store UTC wall time in SQLite and always return timezone-aware UTC values."""

    impl = SADateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(SADateTime(timezone=dialect.name != "sqlite"))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        normalized = ensure_utc(value)
        return normalized.replace(tzinfo=None) if dialect.name == "sqlite" else normalized

    def process_result_value(self, value, dialect):
        return ensure_utc(value) if value is not None else None


# Preserve the declarative model syntax while enforcing UTC for every DateTime column.
DateTime = UTCDateTime


def new_id() -> str:
    return str(uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default="user", index=True)
    timezone: Mapped[str] = mapped_column(String(80), default="UTC")
    personalization_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    digest_time_gmt: Mapped[str] = mapped_column(String(5), default="15:00")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sessions: Mapped[list[UserSession]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class AuthAttempt(Base):
    __tablename__ = "auth_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    ip_address: Mapped[str] = mapped_column(String(64), index=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(240), index=True)
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(120), index=True)
    level: Mapped[str] = mapped_column(String(40), default="All levels", index=True)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    outcomes: Mapped[list[str]] = mapped_column(JSON, default=list)
    price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    rating: Mapped[float] = mapped_column(Float, default=4.5)
    popularity: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_checksum: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CatalogOutbox(Base):
    __tablename__ = "catalog_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    product_version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("product_id", "product_version", "event_type", name="uq_product_outbox_version"),)


class ProductVectorState(Base):
    __tablename__ = "product_vector_state"

    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    product_version: Mapped[int] = mapped_column(Integer)
    point_id: Mapped[str] = mapped_column(String(64))
    embedding_provider: Mapped[str] = mapped_column(String(80), default="unknown")
    embedding_model: Mapped[str] = mapped_column(String(160))
    vector_dimension: Mapped[int] = mapped_column(Integer, default=0)
    index_schema_version: Mapped[str] = mapped_column(String(40), default="legacy")
    content_checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ActivityEvent(Base):
    __tablename__ = "activity_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    search_query: Mapped[str | None] = mapped_column(String(500), nullable=True)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    recommendation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    event_properties: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    __table_args__ = (
        Index("ix_events_user_received", "user_id", "received_at"),
        Index("ix_events_product_type", "product_id", "event_type"),
    )


class BehavioralSignal(Base):
    __tablename__ = "behavioral_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    signal_type: Mapped[str] = mapped_column(String(60), index=True)
    topic: Mapped[str] = mapped_column(String(160), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    strength: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    evidence_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(String(500))
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserInterestProfile(Base):
    __tablename__ = "user_interest_profiles"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    profile_version: Mapped[int] = mapped_column(Integer, default=1)
    source_event_cursor: Mapped[int] = mapped_column(Integer, default=0)
    primary_intent: Mapped[str | None] = mapped_column(String(160), nullable=True)
    secondary_intents: Mapped[list[dict]] = mapped_column(JSON, default=list)
    category_weights: Mapped[dict] = mapped_column(JSON, default=dict)
    topic_weights: Mapped[dict] = mapped_column(JSON, default=dict)
    recent_searches: Mapped[list[str]] = mapped_column(JSON, default=list)
    positive_product_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    negative_product_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    journey_stage: Mapped[str] = mapped_column(String(60), default="exploration")
    confidence: Mapped[float] = mapped_column(Float, default=0)
    trigger_score: Mapped[float] = mapped_column(Float, default=0)
    profile_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RecommendationRun(Base):
    __tablename__ = "recommendation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scope_key: Mapped[str] = mapped_column(String(100), default="overall", index=True)
    context_product_id: Mapped[str | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=True, index=True
    )
    trigger_type: Mapped[str] = mapped_column(String(80))
    trigger_reason: Mapped[str] = mapped_column(String(500))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    profile_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    current_node: Mapped[str | None] = mapped_column(String(80), nullable=True)
    graph_state: Mapped[dict] = mapped_column(JSON, default=dict)
    retrieval_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(40), default="v1")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index(
            "uq_recommendation_runs_user_active",
            "user_id",
            "scope_key",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )


class ServiceInvocation(Base):
    """Durable, user-scoped telemetry for every recommendation dependency."""

    __tablename__ = "service_invocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    recommendation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("recommendation_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    service: Mapped[str] = mapped_column(String(40), index=True)
    operation: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(30), default="started", index=True)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True, index=True)
    workload: Mapped[str] = mapped_column(String(40), default="runtime", index=True)
    attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_receipt: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failover_decision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    langsmith_trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    langsmith_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True, index=True)
    langsmith_run_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    langsmith_export_status: Mapped[str] = mapped_column(String(30), default="not_applicable", index=True)
    langsmith_last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    langsmith_exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    invocation_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_invocations_user_started", "user_id", "started_at"),
        Index("ix_invocations_service_started", "service", "started_at"),
        Index("ix_invocations_export_started", "langsmith_export_status", "started_at"),
    )


class TraceReconciliationRun(Base):
    """Auditable snapshots produced by the LangSmith reconciliation worker."""

    __tablename__ = "trace_reconciliation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_name: Mapped[str] = mapped_column(String(160), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    local_correlated_attempts: Mapped[int] = mapped_column(Integer, default=0)
    remote_provider_spans: Mapped[int] = mapped_column(Integer, default=0)
    matched_attempts: Mapped[int] = mapped_column(Integer, default=0)
    pending_attempts: Mapped[int] = mapped_column(Integer, default=0)
    delayed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    unmatched_remote_spans: Mapped[int] = mapped_column(Integer, default=0)
    legacy_attempts: Mapped[int] = mapped_column(Integer, default=0)
    demo_attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("recommendation_runs.id", ondelete="CASCADE"), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recommendation_type: Mapped[str] = mapped_column(String(30), default="overall", index=True)
    context_product_id: Mapped[str | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=True, index=True
    )
    headline: Mapped[str] = mapped_column(String(240))
    narrative: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    model: Mapped[str] = mapped_column(String(160))
    profile_snapshot: Mapped[dict] = mapped_column(JSON)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RecommendationItem(Base):
    __tablename__ = "recommendation_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    recommendation_id: Mapped[str] = mapped_column(ForeignKey("recommendations.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    semantic_score: Mapped[float] = mapped_column(Float, default=0)
    behavior_score: Mapped[float] = mapped_column(Float, default=0)
    final_score: Mapped[float] = mapped_column(Float)
    confidence_score: Mapped[float] = mapped_column(Float, default=0)
    interest_likelihood: Mapped[float] = mapped_column(Float, default=0)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    explanation: Mapped[str] = mapped_column(Text)
    product_version: Mapped[int] = mapped_column(Integer)

    __table_args__ = (UniqueConstraint("recommendation_id", "rank", name="uq_recommendation_rank"),)


class RecommendationExposure(Base):
    __tablename__ = "recommendation_exposures"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    recommendation_id: Mapped[str] = mapped_column(ForeignKey("recommendations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    displayed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recommendation_id: Mapped[str] = mapped_column(ForeignKey("recommendations.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(30), default="email")
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(30), default="scheduled", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    provider_receipt: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    delivery_id: Mapped[str] = mapped_column(ForeignKey("deliveries.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    provider_status: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    object_type: Mapped[str] = mapped_column(String(80))
    object_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    audit_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
