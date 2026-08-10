from __future__ import annotations

import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from uuid import uuid4

from sqlalchemy import func, select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models import Delivery, DeliveryAttempt, Product, Recommendation, RecommendationItem, User, utcnow


MAX_DAILY_DIGESTS = 10


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def configured_digest_time_gmt(db) -> str:
    """Return the administrator-controlled daily digest time in GMT."""
    value = db.scalar(
        select(User.digest_time_gmt)
        .where(User.role == "admin", User.is_active.is_(True))
        .order_by(User.created_at, User.id)
        .limit(1)
    )
    try:
        hour, minute = (int(part) for part in (value or "").split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (TypeError, ValueError):
        hour, minute = max(0, min(23, get_settings().digest_hour_local)), 0
    return f"{hour:02d}:{minute:02d}"


def _active_overall_recommendation(db, user_id: str, now: datetime) -> Recommendation | None:
    return db.scalar(
        select(Recommendation)
        .where(
            Recommendation.user_id == user_id,
            Recommendation.status == "active",
            Recommendation.recommendation_type == "overall",
            Recommendation.context_product_id.is_(None),
            (Recommendation.expires_at.is_(None) | (Recommendation.expires_at > now)),
        )
        .order_by(Recommendation.generated_at.desc())
    )


def _daily_digest_count(db, user_id: str, utc_date) -> int:
    prefix = f"digest:{user_id}:{utc_date.isoformat()}"
    return db.scalar(
        select(func.count(Delivery.id)).where(
            Delivery.user_id == user_id,
            Delivery.idempotency_key.like(f"{prefix}%"),
        )
    ) or 0


def schedule_admin_digest_slot(time_gmt: str, slot_key: str, now: datetime | None = None) -> dict[str, int]:
    """Persist one admin-created same-day slot without exceeding three digests per learner."""
    now = _aware(now or utcnow()).astimezone(timezone.utc)
    hour, minute = (int(part) for part in time_gmt.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    scheduled_for = now if now >= target else target
    db = SessionLocal()
    created = capped = 0
    try:
        users = db.scalars(
            select(User).where(
                User.is_active.is_(True),
                User.personalization_enabled.is_(True),
                User.digest_enabled.is_(True),
                User.digest_email.is_not(None),
            )
        ).all()
        for user in users:
            prefix = f"digest:{user.id}:{now.date().isoformat()}"
            if _daily_digest_count(db, user.id, now.date()) >= MAX_DAILY_DIGESTS:
                capped += 1
                continue
            recommendation = _active_overall_recommendation(db, user.id, now)
            if not recommendation:
                continue
            key = f"{prefix}:admin:{slot_key}"
            if db.scalar(select(Delivery.id).where(Delivery.idempotency_key == key)):
                continue
            db.add(
                Delivery(
                    user_id=user.id,
                    recommendation_id=recommendation.id,
                    channel="email",
                    scheduled_for=scheduled_for,
                    status="scheduled",
                    idempotency_key=key,
                )
            )
            created += 1
        db.commit()
        return {"created": created, "capped": capped}
    finally:
        db.close()


def schedule_due_digests(now: datetime | None = None) -> dict[str, int]:
    """Create at most one daily delivery per opted-in user and recommendation."""
    now = now or utcnow()
    db = SessionLocal()
    created = 0
    try:
        users = db.scalars(
            select(User).where(
                User.is_active.is_(True),
                User.personalization_enabled.is_(True),
                User.digest_enabled.is_(True),
                User.digest_email.is_not(None),
            )
        ).all()
        digest_hour, digest_minute = (int(part) for part in configured_digest_time_gmt(db).split(":"))
        for user in users:
            utc_now = _aware(now).astimezone(timezone.utc)
            utc_target = utc_now.replace(hour=digest_hour, minute=digest_minute, second=0, microsecond=0)
            scheduled_for = utc_now if utc_now >= utc_target else utc_target
            recommendation = _active_overall_recommendation(db, user.id, now)
            if not recommendation:
                continue
            key = f"digest:{user.id}:{utc_now.date().isoformat()}"
            exists = db.scalar(select(Delivery.id).where(Delivery.idempotency_key == key))
            if not exists and _daily_digest_count(db, user.id, utc_now.date()) < MAX_DAILY_DIGESTS:
                db.add(
                    Delivery(
                        user_id=user.id,
                        recommendation_id=recommendation.id,
                        channel="email",
                        scheduled_for=scheduled_for,
                        status="scheduled",
                        idempotency_key=key,
                    )
                )
                created += 1
        db.commit()
        return {"created": created}
    finally:
        db.close()


def _send_sandbox(delivery: Delivery) -> str:
    return f"sandbox:{delivery.id}:{uuid4().hex[:12]}"


def _send_smtp(delivery: Delivery, user: User, recommendation: Recommendation) -> str:
    settings = get_settings()
    if not settings.smtp_host:
        raise RuntimeError("SMTP_HOST is required when DELIVERY_MODE=smtp")
    message = EmailMessage()
    message["Subject"] = recommendation.headline
    message["From"] = settings.smtp_from
    if not user.digest_email:
        raise RuntimeError("Daily digest email address is required")
    message["To"] = user.digest_email
    db = SessionLocal()
    try:
        items = db.execute(
            select(RecommendationItem, Product)
            .join(Product, Product.id == RecommendationItem.product_id)
            .where(RecommendationItem.recommendation_id == recommendation.id, Product.status == "active")
            .order_by(RecommendationItem.rank)
        ).all()
    finally:
        db.close()
    public_url = settings.app_public_url.rstrip("/")
    display_name = (user.display_name or "there").strip()
    learner_name = escape(display_name)
    course_lines = "\n\n".join(
        f"{index}. {product.title}\n"
        f"   Why it fits: {item.explanation}\n"
        f"   View course: {public_url}/products/{product.slug}"
        for index, (item, product) in enumerate(items, start=1)
    )
    message.set_content(
        f"Hi {display_name},\n\n"
        f"{recommendation.narrative}\n\n"
        f"Courses selected for your learning journey:\n\n{course_lines}\n\n"
        f"Explore your recommendations: {public_url}/\n"
        f"Manage email preferences: {public_url}/account"
    )
    course_cards = "".join(
        f"""
        <tr><td style="padding:0 0 16px 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="border:1px solid #e4e2f4;border-radius:14px;background:#ffffff;">
            <tr><td style="padding:20px 22px;">
              <div style="font-size:12px;line-height:18px;font-weight:700;letter-spacing:1px;color:#6557e8;text-transform:uppercase;">
                {escape(product.category)} &middot; {escape(product.level)}
              </div>
              <h2 style="margin:7px 0 9px;font-size:21px;line-height:28px;color:#17182f;">{escape(product.title)}</h2>
              <p style="margin:0 0 16px;font-size:15px;line-height:24px;color:#50536b;">
                <strong style="color:#292b45;">Why this fits your journey:</strong> {escape(item.explanation)}
              </p>
              <a href="{public_url}/products/{escape(product.slug)}"
                 style="display:inline-block;padding:11px 18px;border-radius:9px;background:#5d4fe5;color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;">
                Explore this course &rarr;
              </a>
            </td></tr>
          </table>
        </td></tr>
        """
        for item, product in items
    )
    message.add_alternative(
        f"""<!doctype html>
<html><body style="margin:0;background:#f5f5fb;font-family:Arial,Helvetica,sans-serif;color:#17182f;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5fb;padding:28px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;">
        <tr><td style="padding:28px 30px;border-radius:18px 18px 0 0;background:#4f46d9;color:#ffffff;">
          <div style="font-size:13px;font-weight:700;letter-spacing:1.4px;text-transform:uppercase;opacity:.9;">SmartReco daily digest</div>
          <h1 style="margin:12px 0 8px;font-size:30px;line-height:38px;color:#ffffff;">{escape(recommendation.headline)}</h1>
          <p style="margin:0;font-size:15px;line-height:23px;color:#f4f1ff;">Prepared for {learner_name}, based on your evolving learning interests.</p>
        </td></tr>
        <tr><td style="padding:27px 30px 30px;background:#ffffff;border-radius:0 0 18px 18px;box-shadow:0 10px 28px rgba(35,32,91,.10);">
          <p style="margin:0 0 9px;font-size:16px;line-height:25px;color:#292b45;">Hi {learner_name},</p>
          <p style="margin:0 0 24px;font-size:16px;line-height:26px;color:#454861;">{escape(recommendation.narrative)}</p>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{course_cards}</table>
          <div style="padding-top:8px;text-align:center;">
            <a href="{public_url}/" style="display:inline-block;padding:13px 22px;border-radius:10px;background:#17182f;color:#ffffff;text-decoration:none;font-size:15px;font-weight:700;">View all recommendations</a>
          </div>
          <p style="margin:24px 0 0;text-align:center;font-size:12px;line-height:19px;color:#7b7e94;">
            You receive this digest because you opted in. <a href="{public_url}/account" style="color:#6557e8;">Manage email preferences</a>.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>""",
        subtype="html",
    )
    tls_context = ssl.create_default_context()
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as client:
        client.starttls(context=tls_context)
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password or "")
        response = client.send_message(message)
    return f"smtp:{delivery.id}:{'accepted' if not response else 'partial'}"


def _recommendation_items_are_current(db, recommendation_id: str) -> bool:
    """Require every stored item to match an existing, active catalog version."""
    rows = db.execute(
        select(RecommendationItem, Product)
        .outerjoin(Product, Product.id == RecommendationItem.product_id)
        .where(RecommendationItem.recommendation_id == recommendation_id)
    ).all()
    return all(
        product is not None
        and product.status == "active"
        and item.product_version == product.version
        for item, product in rows
    )


def dispatch_due_deliveries(now: datetime | None = None, limit: int = 25) -> dict[str, int]:
    """Claim and send due work, recording every provider attempt and retry."""
    now = now or utcnow()
    settings = get_settings()
    db = SessionLocal()
    sent = failed = retried = overdue = 0
    try:
        due_ids = list(db.scalars(
            select(Delivery)
            .where(Delivery.status == "scheduled", Delivery.scheduled_for <= now)
            .order_by(Delivery.scheduled_for)
            .limit(limit)
        ).all())
        for candidate in due_ids:
            delivery_id = candidate.id
            if now - _aware(candidate.scheduled_for) > timedelta(hours=settings.delivery_overdue_hours):
                marked = db.execute(
                    update(Delivery).where(Delivery.id == delivery_id, Delivery.status == "scheduled").values(status="overdue")
                )
                overdue += int(marked.rowcount == 1)
                db.commit()
                continue

            claimed = db.execute(
                update(Delivery)
                .where(Delivery.id == delivery_id, Delivery.status == "scheduled", Delivery.scheduled_for <= now)
                .values(status="processing")
            )
            if claimed.rowcount != 1:
                db.rollback()
                continue
            db.commit()
            delivery = db.get(Delivery, delivery_id)
            attempt_number = (db.scalar(select(func.count(DeliveryAttempt.id)).where(DeliveryAttempt.delivery_id == delivery.id)) or 0) + 1
            attempt = DeliveryAttempt(delivery_id=delivery.id, attempt_number=attempt_number, status="processing")
            db.add(attempt)
            db.commit()
            try:
                user = db.get(User, delivery.user_id)
                recommendation = db.get(Recommendation, delivery.recommendation_id)
                if not user or not recommendation:
                    raise RuntimeError("Delivery target no longer exists")
                if not user.is_active or not user.personalization_enabled or not user.digest_enabled:
                    delivery.status = "cancelled"
                    attempt.status = "cancelled"
                    attempt.provider_status = "consent_withdrawn"
                    db.commit()
                    continue
                if not user.digest_email:
                    delivery.status = "cancelled"
                    attempt.status = "cancelled"
                    attempt.provider_status = "digest_email_missing"
                    db.commit()
                    continue
                expires = recommendation.expires_at
                if recommendation.status != "active" or recommendation.recommendation_type != "overall" or (
                    expires and _aware(expires) <= now
                ):
                    delivery.status = "cancelled"
                    attempt.status = "cancelled"
                    attempt.provider_status = "recommendation_ineligible"
                    db.commit()
                    continue
                if not _recommendation_items_are_current(db, recommendation.id):
                    delivery.status = "cancelled"
                    attempt.status = "cancelled"
                    attempt.provider_status = "recommendation_stale"
                    db.commit()
                    continue
                receipt = _send_smtp(delivery, user, recommendation) if settings.delivery_mode == "smtp" else _send_sandbox(delivery)
                delivery.status = "sent"
                delivery.provider_receipt = receipt
                delivery.sent_at = now
                attempt.status = "sent"
                attempt.provider_status = "accepted"
                sent += 1
            except Exception as exc:
                attempt.error_detail = str(exc)[:2000]
                attempt.provider_status = "error"
                if attempt_number >= settings.delivery_max_attempts:
                    delivery.status = "failed"
                    attempt.status = "failed"
                    failed += 1
                else:
                    delay = settings.delivery_retry_base_seconds * (2 ** (attempt_number - 1))
                    retry_at = now + timedelta(seconds=delay)
                    delivery.status = "scheduled"
                    delivery.scheduled_for = retry_at
                    attempt.status = "retry_scheduled"
                    attempt.next_retry_at = retry_at
                    retried += 1
            db.commit()
        return {"sent": sent, "failed": failed, "retried": retried, "overdue": overdue}
    finally:
        db.close()


def recover_stale_deliveries(now: datetime | None = None) -> int:
    """Return abandoned processing claims to the queue after ten minutes."""
    now = now or utcnow()
    db = SessionLocal()
    recovered = 0
    try:
        candidates = db.scalars(select(Delivery).where(Delivery.status == "processing")).all()
        for delivery in candidates:
            latest = db.scalar(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery.id)
                .order_by(DeliveryAttempt.created_at.desc())
            )
            if latest and now - _aware(latest.created_at) > timedelta(minutes=10):
                delivery.status = "scheduled"
                delivery.scheduled_for = now
                latest.status = "abandoned"
                latest.error_detail = "Worker claim expired and was recovered"
                recovered += 1
        db.commit()
        return recovered
    finally:
        db.close()
