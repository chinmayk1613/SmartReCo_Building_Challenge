import hashlib
import json
import math
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ActivityEvent, BehavioralSignal, Product, UserInterestProfile, utcnow
from app.services.topics import normalize_topic


EVENT_RULES = {
    "page_viewed": ("BROWSE", 0.05, 0.5),
    "category_selected": ("TOPIC_INTEREST", 0.25, 1.0),
    "search_submitted": ("EXPLICIT_INTENT", 0.70, 6.0),
    "product_impression": ("EXPOSURE", 0.03, 0.0),
    "product_viewed": ("PRODUCT_INTEREST", 0.35, 3.0),
    "product_clicked": ("PRODUCT_INTEREST", 0.55, 5.0),
    "active_dwell": ("HIGH_ENGAGEMENT", 0.45, 4.0),
    "added_to_cart": ("PURCHASE_INTENT", 0.95, 10.0),
    "cart_viewed": ("CART_REVIEW", 0.30, 1.5),
    "removed_from_cart": ("CART_RELEASED", 0.0, 4.0),
    "recommendation_impression": ("RECOMMENDATION_EXPOSURE", 0.0, 0.0),
    "recommendation_clicked": ("RECOMMENDATION_RESPONSE", 0.75, 6.0),
    "recommendation_dismissed": ("NEGATIVE_FEEDBACK", -0.85, 6.0),
    "purchase_started": ("PURCHASE_INTENT", 0.90, 10.0),
    "purchase_completed": ("CONVERSION", 1.0, 12.0),
}


def derive_signals(db: Session, user_id: str) -> tuple[list[BehavioralSignal], UserInterestProfile]:
    profile = db.get(UserInterestProfile, user_id) or UserInterestProfile(user_id=user_id)
    events = list(
        db.scalars(
            select(ActivityEvent)
            .where(ActivityEvent.user_id == user_id, ActivityEvent.id > (profile.source_event_cursor or 0))
            .order_by(ActivityEvent.id)
        ).all()
    )
    created: list[BehavioralSignal] = []
    trigger_delta = 0.0
    for event in events:
        signal_type, base_strength, trigger_weight = EVENT_RULES[event.event_type]
        reason = f"Derived from {event.event_type.replace('_', ' ')}"
        if event.event_type == "active_dwell":
            seconds = (event.duration_ms or 0) / 1000
            if seconds < 15:
                profile.source_event_cursor = event.id
                continue
            base_strength = min(0.9, 0.35 + math.log1p(min(seconds, 300)) / 10)
            reason = f"Course dwell time: {round(seconds)} seconds of active viewing"
        product = db.get(Product, event.product_id) if event.product_id else None
        raw_topic = event.search_query or event.category or (product.category if product else None)
        topic = normalize_topic(raw_topic)
        signal = None
        if event.event_type == "active_dwell" and event.session_id and event.product_id:
            signal = db.scalar(
                select(BehavioralSignal)
                .where(
                    BehavioralSignal.user_id == user_id,
                    BehavioralSignal.session_id == event.session_id,
                    BehavioralSignal.product_id == event.product_id,
                    BehavioralSignal.signal_type == "HIGH_ENGAGEMENT",
                )
                .order_by(BehavioralSignal.last_observed_at.desc())
                .limit(1)
            )
            if signal:
                signal.strength = base_strength
                signal.confidence = min(1.0, abs(base_strength) + 0.1)
                signal.evidence_event_ids = [*(signal.evidence_event_ids or [])[-19:], event.event_id]
                signal.reason = reason
                signal.last_observed_at = utcnow()
                signal.expires_at = utcnow() + timedelta(days=30)
                trigger_weight = 1.0
        if signal is None:
            signal = BehavioralSignal(
                user_id=user_id,
                session_id=event.session_id,
                signal_type=signal_type,
                topic=topic,
                product_id=event.product_id,
                strength=base_strength,
                confidence=min(1.0, abs(base_strength) + 0.1),
                evidence_event_ids=[event.event_id],
                reason=reason,
                expires_at=utcnow() + timedelta(days=30),
            )
        db.add(signal)
        created.append(signal)
        trigger_delta += trigger_weight
        profile.source_event_cursor = event.id

    db.flush()
    all_signals = list(
        db.scalars(
            select(BehavioralSignal)
            .where(
                BehavioralSignal.user_id == user_id,
                (BehavioralSignal.expires_at.is_(None) | (BehavioralSignal.expires_at > utcnow())),
            )
            .order_by(BehavioralSignal.last_observed_at.desc())
            .limit(500)
        ).all()
    )
    weights: defaultdict[str, float] = defaultdict(float)
    positives: list[str] = []
    negatives: list[str] = []
    searches: list[str] = []
    now = utcnow()
    for signal in all_signals:
        observed = signal.last_observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=now.tzinfo)
        age_hours = max(0.0, (now - observed).total_seconds() / 3600)
        decay = 2 ** (-age_hours / 72)
        weights[signal.topic] += signal.strength * signal.confidence * decay
        if signal.product_id and signal.strength > 0.5 and signal.product_id not in positives:
            positives.append(signal.product_id)
    preference_events = list(
        db.scalars(
            select(ActivityEvent)
            .where(
                ActivityEvent.user_id == user_id,
                ActivityEvent.product_id.is_not(None),
                ActivityEvent.event_type.in_([
                    "recommendation_dismissed", "recommendation_clicked", "added_to_cart", "purchase_completed"
                ]),
            )
            .order_by(ActivityEvent.id)
        ).all()
    )
    negative_state: set[str] = set()
    for event in preference_events:
        if event.event_type == "recommendation_dismissed":
            negative_state.add(event.product_id)
        else:
            negative_state.discard(event.product_id)
    negatives = list(negative_state)
    search_events = list(
        db.scalars(
            select(ActivityEvent)
            .where(ActivityEvent.user_id == user_id, ActivityEvent.event_type == "search_submitted")
            .order_by(ActivityEvent.received_at.desc())
            .limit(10)
        ).all()
    )
    searches = [event.search_query for event in search_events if event.search_query]
    ranked = sorted(weights.items(), key=lambda item: item[1], reverse=True)
    profile.primary_intent = ranked[0][0] if ranked else None
    profile.secondary_intents = [{"topic": topic, "strength": round(score, 4)} for topic, score in ranked[1:4] if score > 0]
    profile.category_weights = {topic: round(score, 4) for topic, score in ranked[:12]}
    profile.topic_weights = dict(profile.category_weights)
    profile.recent_searches = searches
    profile.positive_product_ids = positives[:50]
    profile.negative_product_ids = negatives[:50]
    profile.journey_stage = (
        "conversion" if any(signal.signal_type == "CONVERSION" for signal in created)
        else "purchase_intent" if any(signal.signal_type == "PURCHASE_INTENT" for signal in all_signals[:20])
        else "comparison" if len(set(positives[:5])) >= 2
        else "exploration"
    )
    profile.confidence = min(1.0, sum(max(0, score) for _, score in ranked[:3]) / 2.5)
    profile.trigger_score = max(0.0, (profile.trigger_score or 0.0) + trigger_delta)
    profile.profile_version = (profile.profile_version or 0) + (1 if events else 0)
    payload = {
        "primary": profile.primary_intent,
        "secondary": profile.secondary_intents,
        "weights": profile.category_weights,
        "positive": profile.positive_product_ids,
        "negative": profile.negative_product_ids,
        "journey": profile.journey_stage,
    }
    profile.profile_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    profile.updated_at = utcnow()
    db.add(profile)
    return created, profile


def signal_summary(db: Session, user_id: str, limit: int = 20) -> list[BehavioralSignal]:
    return list(
        db.scalars(
            select(BehavioralSignal)
            .where(
                BehavioralSignal.user_id == user_id,
                (BehavioralSignal.expires_at.is_(None) | (BehavioralSignal.expires_at > utcnow())),
            )
            .order_by(BehavioralSignal.last_observed_at.desc())
            .limit(limit)
        ).all()
    )


def recent_interest_topics(
    db: Session,
    user_id: str,
    interaction_limit: int = 10,
    topic_limit: int = 3,
) -> list[dict]:
    """Summarize the strongest topics in the user's latest meaningful interactions."""
    meaningful_types = {
        "category_selected",
        "search_submitted",
        "product_viewed",
        "product_clicked",
        "active_dwell",
        "added_to_cart",
        "cart_viewed",
        "removed_from_cart",
        "recommendation_clicked",
        "purchase_started",
        "purchase_completed",
    }
    events = list(
        db.scalars(
            select(ActivityEvent)
            .where(ActivityEvent.user_id == user_id, ActivityEvent.event_type.in_(meaningful_types))
            .order_by(ActivityEvent.id.desc())
            .limit(interaction_limit)
        ).all()
    )
    scores: defaultdict[str, float] = defaultdict(float)
    counts: defaultdict[str, int] = defaultdict(int)
    labels: dict[str, str] = {}
    for index, event in enumerate(events):
        product = db.get(Product, event.product_id) if event.product_id else None
        raw_topic = event.search_query or event.category or (product.category if product else None)
        topic = normalize_topic(raw_topic)
        if topic == "general":
            continue
        base_strength = EVENT_RULES[event.event_type][1]
        recency = max(0.55, 1.0 - index * 0.05)
        scores[topic] += base_strength * recency
        counts[topic] += 1
        labels.setdefault(topic, (raw_topic or topic).replace("_", " ").strip().title())
    ranked = sorted(scores.items(), key=lambda item: (item[1], counts[item[0]]), reverse=True)
    return [
        {
            "topic": topic,
            "label": labels[topic],
            "score": round(score, 3),
            "interactions": counts[topic],
        }
        for topic, score in ranked[:topic_limit]
        if score > 0
    ]


def overall_interest_topics(db: Session, user_id: str, topic_limit: int = 3) -> list[dict]:
    """Top interests from the user's complete profile, with built-in recency decay."""
    profile = db.get(UserInterestProfile, user_id)
    if not profile:
        return []
    positive = [(topic, float(score)) for topic, score in (profile.category_weights or {}).items() if float(score) > 0]
    positive.sort(key=lambda item: item[1], reverse=True)
    maximum = positive[0][1] if positive else 1.0
    return [
        {
            "topic": topic,
            "label": topic.replace("_", " ").title(),
            "score": round(score, 3),
            "affinity": round(score / maximum * 100),
            "profile_version": profile.profile_version,
        }
        for topic, score in positive[:topic_limit]
    ]
