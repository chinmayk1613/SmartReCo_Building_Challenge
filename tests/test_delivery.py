from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import ssl
from threading import Barrier

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import Delivery, DeliveryAttempt, Recommendation, RecommendationItem, RecommendationRun, utcnow
from app.services import delivery as delivery_service
from app.services.delivery import (
    dispatch_due_deliveries,
    recover_stale_deliveries,
    schedule_admin_digest_slot,
    schedule_due_digests,
)


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


def test_daily_digest_scheduling_is_idempotent(db, user, admin, active_recommendation):
    user.digest_enabled = True
    admin.digest_time_gmt = "18:45"
    db.commit()
    now = datetime(2026, 8, 10, 18, 30, tzinfo=timezone.utc)
    assert schedule_due_digests(now)["created"] == 1
    assert schedule_due_digests(now)["created"] == 0
    assert db.query(Delivery).count() == 1
    delivery = db.scalar(select(Delivery))
    assert delivery.scheduled_for == datetime(2026, 8, 10, 18, 45, tzinfo=timezone.utc)


def test_admin_time_changes_create_at_most_ten_daily_digests(db, user, admin, active_recommendation):
    user.digest_enabled = True
    admin.digest_time_gmt = "15:00"
    db.commit()
    now = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)

    assert schedule_due_digests(now)["created"] == 1
    for index, value in enumerate([f"{hour:02d}:00" for hour in range(11, 20)], start=1):
        assert schedule_admin_digest_slot(value, f"change-{index}", now) == {"created": 1, "capped": 0}
    assert schedule_admin_digest_slot("20:00", "change-10", now) == {"created": 0, "capped": 1}

    deliveries = list(db.scalars(select(Delivery).order_by(Delivery.scheduled_for)).all())
    assert len(deliveries) == 10
    assert len({delivery.idempotency_key for delivery in deliveries}) == 10


def test_normal_scheduler_cannot_exceed_ten_existing_admin_slots(db, user, admin, active_recommendation):
    user.digest_enabled = True
    admin.digest_time_gmt = "15:00"
    db.commit()
    now = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)

    for index, value in enumerate([f"{hour:02d}:00" for hour in range(11, 21)], start=1):
        assert schedule_admin_digest_slot(value, f"change-{index}", now)["created"] == 1

    assert schedule_due_digests(now)["created"] == 0
    assert db.query(Delivery).count() == 10


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


def test_smtp_uses_only_profile_digest_email(db, user, products, active_recommendation, monkeypatch):
    user.digest_email = "daily@personal.example"
    _attach_recommendation_item(db, active_recommendation, products[0])
    delivery = _queue_delivery(db, user, active_recommendation, "digest-recipient")
    captured = {}
    events = []

    class FakeSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self, *, context):
            events.append("starttls")
            captured["tls_context"] = context
            return None

        def login(self, *_args):
            events.append("login")
            return None

        def send_message(self, message):
            events.append("send")
            captured["recipient"] = message["To"]
            captured["message"] = message
            return {}

    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_username", "api")
    monkeypatch.setattr(settings, "smtp_password", "test-secret")
    monkeypatch.setattr(delivery_service.smtplib, "SMTP", FakeSMTP)
    receipt = delivery_service._send_smtp(delivery, user, active_recommendation)

    assert captured["recipient"] == "daily@personal.example"
    assert isinstance(captured["tls_context"], ssl.SSLContext)
    assert captured["tls_context"].verify_mode == ssl.CERT_REQUIRED
    assert captured["tls_context"].check_hostname is True
    assert events == ["starttls", "login", "send"]
    plain_body = captured["message"].get_body(preferencelist=("plain",)).get_content()
    html_body = captured["message"].get_body(preferencelist=("html",)).get_content()
    course_url = f"{settings.app_public_url.rstrip('/')}/products/{products[0].slug}"
    assert f"Hi {user.display_name}" in plain_body
    assert course_url in plain_body
    assert "Why it fits:" in plain_body
    assert "SmartReco daily digest" in html_body
    assert course_url in html_body
    assert "Why this fits your journey:" in html_body
    assert f"Prepared for {user.display_name}" in html_body
    assert receipt.startswith(f"smtp:{delivery.id}:accepted")


def test_tls_verification_failure_never_authenticates_or_sends_and_retries(
    db, user, active_recommendation, monkeypatch
):
    delivery = _queue_delivery(db, user, active_recommendation, "tls-verification-failure")
    events = []
    contexts = []

    class FailingTLS:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self, *, context):
            events.append("starttls")
            contexts.append(context)
            raise ssl.SSLCertVerificationError("certificate verification failed")

        def login(self, *_args):
            events.append("login")

        def send_message(self, _message):
            events.append("send")
            return {}

    settings = get_settings()
    monkeypatch.setattr(settings, "delivery_mode", "smtp")
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_username", "api")
    monkeypatch.setattr(settings, "smtp_password", "test-secret")
    monkeypatch.setattr(delivery_service.smtplib, "SMTP", FailingTLS)

    result = dispatch_due_deliveries()

    db.refresh(delivery)
    attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery.id))
    assert events == ["starttls"]
    assert len(contexts) == 1
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].check_hostname is True
    assert result["sent"] == 0
    assert result["retried"] == 1
    assert delivery.status == "scheduled"
    assert delivery.provider_receipt is None
    assert delivery.sent_at is None
    assert attempt.status == "retry_scheduled"
    assert attempt.provider_status == "error"
    assert "certificate verification failed" in (attempt.error_detail or "")


def test_digest_requires_configured_digest_email(db, user, active_recommendation):
    user.digest_enabled = True
    user.digest_email = None
    db.commit()
    assert schedule_due_digests()["created"] == 0
    assert db.query(Delivery).count() == 0


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


def test_removed_digest_email_cancels_before_provider_contact(db, user, active_recommendation, monkeypatch):
    delivery = _queue_delivery(db, user, active_recommendation, "digest-address-removed")
    user.digest_email = None
    db.commit()
    provider_calls = []
    monkeypatch.setattr(get_settings(), "delivery_mode", "smtp")
    monkeypatch.setattr(delivery_service, "_send_smtp", lambda *_args: provider_calls.append(True))

    result = dispatch_due_deliveries()

    db.refresh(delivery)
    attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery.id))
    assert result["sent"] == 0
    assert provider_calls == []
    assert delivery.status == "cancelled"
    assert attempt.provider_status == "digest_email_missing"


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
