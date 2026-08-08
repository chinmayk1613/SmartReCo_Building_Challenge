from datetime import timedelta

from sqlalchemy import delete, select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    ActivityEvent,
    AuthAttempt,
    BehavioralSignal,
    Delivery,
    DeliveryAttempt,
    Recommendation,
    RecommendationExposure,
    RecommendationItem,
    UserSession,
    utcnow,
)


def enforce_retention(now=None) -> dict[str, int]:
    """Apply documented UTC retention without destroying the aggregate interest profile."""
    now = now or utcnow()
    settings = get_settings()
    db = SessionLocal()
    counts: dict[str, int] = {}
    try:
        expired = db.execute(
            update(Recommendation)
            .where(Recommendation.status == "active", Recommendation.expires_at.is_not(None), Recommendation.expires_at <= now)
            .values(status="expired")
        )
        counts["recommendations_expired"] = expired.rowcount

        old_recommendation_ids = list(db.scalars(
            select(Recommendation.id).where(
                Recommendation.status != "active",
                Recommendation.generated_at < now - timedelta(days=settings.recommendation_retention_days),
            )
        ).all())
        if old_recommendation_ids:
            delivery_ids = list(db.scalars(select(Delivery.id).where(Delivery.recommendation_id.in_(old_recommendation_ids))).all())
            if delivery_ids:
                db.execute(delete(DeliveryAttempt).where(DeliveryAttempt.delivery_id.in_(delivery_ids)))
                db.execute(delete(Delivery).where(Delivery.id.in_(delivery_ids)))
            db.execute(delete(RecommendationExposure).where(RecommendationExposure.recommendation_id.in_(old_recommendation_ids)))
            db.execute(delete(RecommendationItem).where(RecommendationItem.recommendation_id.in_(old_recommendation_ids)))
            removed = db.execute(delete(Recommendation).where(Recommendation.id.in_(old_recommendation_ids)))
            counts["recommendations_deleted"] = removed.rowcount
        else:
            counts["recommendations_deleted"] = 0

        counts["events_deleted"] = db.execute(
            delete(ActivityEvent).where(ActivityEvent.received_at < now - timedelta(days=settings.activity_retention_days))
        ).rowcount
        counts["signals_deleted"] = db.execute(
            delete(BehavioralSignal).where(
                (BehavioralSignal.expires_at.is_not(None) & (BehavioralSignal.expires_at <= now))
                | (BehavioralSignal.last_observed_at < now - timedelta(days=settings.signal_retention_days))
            )
        ).rowcount
        counts["sessions_deleted"] = db.execute(
            delete(UserSession).where(UserSession.expires_at <= now)
        ).rowcount
        counts["auth_attempts_deleted"] = db.execute(
            delete(AuthAttempt).where(AuthAttempt.attempted_at < now - timedelta(days=settings.auth_attempt_retention_days))
        ).rowcount
        db.commit()
        return counts
    finally:
        db.close()
