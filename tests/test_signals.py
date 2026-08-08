from datetime import timedelta
from uuid import uuid4

import pytest

from app.models import ActivityEvent, BehavioralSignal, UserInterestProfile, utcnow
from app.services.signals import EVENT_RULES, derive_signals, normalize_topic


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Agentic AI", "agentic_ai"), ("Advanced Agentic AI Course", "advanced_agentic_ai"), ("Python for AI", "python_ai"),
        ("RAG & Vector Search", "rag_vector_search"), ("  MLOps  ", "mlops"), ("The Course for Testing", "testing"),
        ("Cloud + DevOps", "cloud_devops"), ("Data Engineering", "data_engineering"), ("", "general"), (None, "general"),
        ("LLM Evals", "llm_evals"), ("Prompt-to-Production", "prompt_production"),
    ],
)
def test_topic_normalization(raw, expected):
    assert normalize_topic(raw) == expected


@pytest.mark.parametrize("event_type", sorted(EVENT_RULES))
def test_each_event_rule_derives_expected_signal(db, user, products, event_type):
    duration = 30_000 if event_type == "active_dwell" else None
    event = ActivityEvent(
        event_id=str(uuid4()), user_id=user.id, session_id="session", event_type=event_type,
        product_id=products[0].id if event_type not in {"page_viewed", "search_submitted", "category_selected"} else None,
        search_query="agentic ai" if event_type == "search_submitted" else None,
        category="Agentic AI", duration_ms=duration,
    )
    db.add(event); db.commit()
    signals, profile = derive_signals(db, user.id); db.commit()
    assert len(signals) == 1
    assert signals[0].signal_type == EVENT_RULES[event_type][0]
    assert profile.source_event_cursor == event.id


@pytest.mark.parametrize("duration", [0, 1000, 5000, 14999])
def test_short_dwell_is_not_a_signal(db, user, products, duration):
    db.add(ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="active_dwell", product_id=products[0].id, category="Agentic AI", duration_ms=duration))
    db.commit()
    signals, profile = derive_signals(db, user.id); db.commit()
    assert signals == []
    assert profile.source_event_cursor > 0


@pytest.mark.parametrize("duration", [15000, 30000, 60000, 120000, 300000])
def test_meaningful_dwell_strength_is_bounded(db, user, products, duration):
    db.add(ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="active_dwell", product_id=products[0].id, category="Agentic AI", duration_ms=duration))
    db.commit()
    signals, _ = derive_signals(db, user.id); db.commit()
    assert 0.35 <= signals[0].strength <= 0.9


def test_course_dwell_signal_explains_recorded_seconds(db, user, products):
    db.add(ActivityEvent(event_id=str(uuid4()), user_id=user.id, session_id="course-visit", event_type="active_dwell", product_id=products[0].id, category="Agentic AI", duration_ms=15_000))
    db.commit()
    derive_signals(db, user.id); db.commit()
    db.add(ActivityEvent(event_id=str(uuid4()), user_id=user.id, session_id="course-visit", event_type="active_dwell", product_id=products[0].id, category="Agentic AI", duration_ms=37_400))
    db.commit()
    signals, _ = derive_signals(db, user.id); db.commit()
    assert signals[0].signal_type == "HIGH_ENGAGEMENT"
    assert signals[0].reason == "Course dwell time: 37 seconds of active viewing"
    assert db.query(BehavioralSignal).filter_by(user_id=user.id, signal_type="HIGH_ENGAGEMENT").count() == 1
    assert len(signals[0].evidence_event_ids) == 2


def test_profile_preserves_primary_and_secondary_intent(db, user, products):
    for category, product in [("Agentic AI", products[0]), ("Python for AI", products[2])]:
        db.add(ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="product_clicked", product_id=product.id, category=category))
    db.add(ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="added_to_cart", product_id=products[0].id, category="Agentic AI"))
    db.commit()
    _signals, profile = derive_signals(db, user.id); db.commit()
    assert profile.primary_intent == "agentic_ai"
    assert any(item["topic"] == "python_ai" for item in profile.secondary_intents)
    assert profile.journey_stage == "purchase_intent"


def test_signal_derivation_is_cursor_idempotent(db, user):
    db.add(ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="search_submitted", search_query="RAG evaluation")); db.commit()
    first, profile = derive_signals(db, user.id); db.commit()
    second, profile = derive_signals(db, user.id); db.commit()
    assert len(first) == 1
    assert second == []
    assert db.query(BehavioralSignal).count() == 1
