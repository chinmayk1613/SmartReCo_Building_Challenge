from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import Delivery, DeliveryAttempt, Recommendation, RecommendationItem, RecommendationRun, utcnow
from app.services import delivery as delivery_service
from app.services.delivery import dispatch_due_deliveries, recover_stale_deliveries, schedule_due_digests


@pytest.fixture
def active_recommendation(db, user):
    run = RecommendationRun(
        user_id=user.id,
        trigger_type="test",
        trigger_reason="delivery test",
        idempotency_key=f"test-{user.id}",
        profile_hash="profile",
        status="succeeded",
    )
    db.add(run)
    db.flush()
    recommendation = Recommendation(
        run_id=run.id,
        user_id=user.id,
        headline="A useful learning path",
        narrative="Three grounded courses selected from the current catalog.",
        model="deterministic-local-fallback",
        profile_snapshot={},
    )
    db.add(recommendation)
    db.commit()
    return recommendation


def _attach_recommendation_item(db, recommendation, product):
    db.add(
        RecommendationItem(
            recommendation_id=recommendation.id,
            product_id=product.id,
            rank=1,
            final_score=0.9,
            explanation="A catalog-grounded delivery test item.",
            product_version=product.version,
        )
    )
    db.commit()


def _queue_delivery(db, user, recommendation, key):
    user.digest_enabled = True
    delivery = Delivery(
        user_id=user.id,
        recommendation_id=recommendation.id,
        scheduled_for=utcnow(),
        status="scheduled",
        idempotency_key=key,
    )
    db.add(delivery)
    db.commit()
    return delivery


def test_digest_requires_explicit_opt_in(db, user, active_recommendation):
    assert schedule_due_digests()["created"] == 0
    assert db.query(Delivery).count() == 0


def test_daily_digest_scheduling_is_idempotent(db, user, active_recommendation):
    user.digest_enabled = True
    db.commit()
    assert schedule_due_digests()["created"] == 1
    assert schedule_due_digests()["created"] == 0
    assert db.query(Delivery).count() == 1


def test_sandbox_dispatch_records_receipt_and_attempt(db, user, active_recommendation):
    user.digest_enabled = True
    db.commit()
    now = utcnow().replace(hour=16, minute=0, second=0, microsecond=0)
    schedule_due_digests(now)
    result = dispatch_due_deliveries(now)
    db.expire_all()
    delivery = db.scalar(select(Delivery))
    attempt = db.scalar(select(DeliveryAttempt))
    assert result["sent"] == 1
    assert delivery.status == "sent"
    assert delivery.provider_receipt.startswith("sandbox:")
    assert attempt.status == "sent"
    assert attempt.provider_status == "accepted"


def test_edited_product_cancels_stale_recommendation_before_provider_contact(
    db, user, products, active_recommendation, monkeypatch
):
    product = products[0]
    _attach_recommendation_item(db, active_recommendation, product)
    delivery = _queue_delivery(db, user, active_recommendation, "edited-product")
    product.version += 1
    db.commit()
    provider_calls = []
    monkeypatch.setattr(get_settings(), "delivery_mode", "smtp")
    monkeypatch.setattr(delivery_service, "_send_smtp", lambda *_args: provider_calls.append(True))

    result = dispatch_due_deliveries()

    db.expire_all()
    attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery.id))
    assert result["sent"] == 0
    assert db.get(Delivery, delivery.id).status == "cancelled"
    assert attempt.status == "cancelled"
    assert attempt.provider_status == "recommendation_stale"
    assert provider_calls == []


def test_archived_product_cancels_recommendation_before_provider_contact(
    db, user, products, active_recommendation, monkeypatch
):
    product = products[0]
    _attach_recommendation_item(db, active_recommendation, product)
    delivery = _queue_delivery(db, user, active_recommendation, "archived-product")
    product.status = "archived"
    product.version += 1
    db.commit()
    provider_calls = []
    monkeypatch.setattr(get_settings(), "delivery_mode", "smtp")
    monkeypatch.setattr(delivery_service, "_send_smtp", lambda *_args: provider_calls.append(True))

    result = dispatch_due_deliveries()

    db.expire_all()
    attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery.id))
    assert result["sent"] == 0
    assert db.get(Delivery, delivery.id).status == "cancelled"
    assert attempt.status == "cancelled"
    assert attempt.provider_status == "recommendation_stale"
    assert provider_calls == []


def test_unchanged_fresh_recommendation_sends_normally(
    db, user, products, active_recommendation, monkeypatch
):
    _attach_recommendation_item(db, active_recommendation, products[0])
    delivery = _queue_delivery(db, user, active_recommendation, "fresh-product")
    provider_calls = []

    def send_smtp(current_delivery, _user, _recommendation):
        provider_calls.append(current_delivery.id)
        return f"smtp:{current_delivery.id}:fresh"

    monkeypatch.setattr(get_settings(), "delivery_mode", "smtp")
    monkeypatch.setattr(delivery_service, "_send_smtp", send_smtp)

    result = dispatch_due_deliveries()

    db.expire_all()
    attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery.id))
    stored_delivery = db.get(Delivery, delivery.id)
    assert result["sent"] == 1
    assert stored_delivery.status == "sent"
    assert stored_delivery.provider_receipt == f"smtp:{delivery.id}:fresh"
    assert attempt.status == "sent"
    assert attempt.provider_status == "accepted"
    assert provider_calls == [delivery.id]


def test_old_unsent_delivery_becomes_overdue(db, user, active_recommendation):
    delivery = Delivery(user_id=user.id, recommendation_id=active_recommendation.id, scheduled_for=utcnow() - timedelta(days=2), status="scheduled", idempotency_key="old")
    db.add(delivery)
    db.commit()
    result = dispatch_due_deliveries()
    db.refresh(delivery)
    assert result["overdue"] == 1
    assert delivery.status == "overdue"


def test_provider_failure_creates_exponential_retry(db, user, active_recommendation):
    settings = get_settings()
    previous_mode, previous_host = settings.delivery_mode, settings.smtp_host
    settings.delivery_mode, settings.smtp_host = "smtp", None
    try:
        user.digest_enabled = True
        delivery = Delivery(user_id=user.id, recommendation_id=active_recommendation.id, scheduled_for=utcnow(), status="scheduled", idempotency_key="retry")
        db.add(delivery)
        db.commit()
        result = dispatch_due_deliveries()
        db.refresh(delivery)
        attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery.id))
        assert result["retried"] == 1
        assert delivery.status == "scheduled"
        assert attempt.status == "retry_scheduled"
        assert attempt.next_retry_at is not None
        assert "SMTP_HOST" in attempt.error_detail
    finally:
        settings.delivery_mode, settings.smtp_host = previous_mode, previous_host


def test_stale_processing_claim_is_recovered(db, user, active_recommendation):
    delivery = Delivery(user_id=user.id, recommendation_id=active_recommendation.id, scheduled_for=utcnow(), status="processing", idempotency_key="stale")
    db.add(delivery)
    db.flush()
    attempt = DeliveryAttempt(delivery_id=delivery.id, attempt_number=1, status="processing", created_at=utcnow() - timedelta(minutes=11))
    db.add(attempt)
    db.commit()
    assert recover_stale_deliveries() == 1
    db.refresh(delivery)
    db.refresh(attempt)
    assert delivery.status == "scheduled"
    assert attempt.status == "abandoned"


def test_opt_out_after_queue_cancels_before_provider_contact(db, user, active_recommendation):
    user.personalization_enabled = True
    user.digest_enabled = True
    delivery = Delivery(
        user_id=user.id,
        recommendation_id=active_recommendation.id,
        scheduled_for=utcnow(),
        status="scheduled",
        idempotency_key="consent-withdrawn",
    )
    db.add(delivery)
    db.commit()
    user.digest_enabled = False
    db.commit()
    result = dispatch_due_deliveries()
    db.refresh(delivery)
    attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery.id))
    assert result["sent"] == 0
    assert delivery.status == "cancelled"
    assert attempt.provider_status == "consent_withdrawn"


def test_two_dispatchers_cannot_send_one_delivery_twice(db, user, active_recommendation):
    user.digest_enabled = True
    delivery = Delivery(
        user_id=user.id,
        recommendation_id=active_recommendation.id,
        scheduled_for=utcnow(),
        status="scheduled",
        idempotency_key="concurrent-dispatch",
    )
    db.add(delivery)
    db.commit()
    barrier = Barrier(2)

    def dispatch():
        barrier.wait()
        return dispatch_due_deliveries(limit=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: dispatch(), range(2)))
    db.expire_all()
    assert sum(result["sent"] for result in results) == 1
    assert db.get(Delivery, delivery.id).status == "sent"
    attempts = list(db.scalars(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery.id)).all())
    assert len(attempts) == 1
    assert attempts[0].status == "sent"
