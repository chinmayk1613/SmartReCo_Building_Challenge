from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    ActivityEvent,
    BehavioralSignal,
    Product,
    Recommendation,
    RecommendationItem,
    RecommendationRun,
    User,
    UserInterestProfile,
    utcnow,
)
from app.services.recommendation import profile_to_dict, retrieve_and_rank
from app.services.signals import derive_signals


def _stage_events(user_id: str, product: Product, topic: str, stage: str, repeats: int = 3) -> list[ActivityEvent]:
    session_id = f"evaluation-{stage}-{uuid4()}"
    events: list[ActivityEvent] = []
    for index in range(repeats):
        events.extend([
            ActivityEvent(
                event_id=str(uuid4()), user_id=user_id, session_id=session_id,
                event_type="search_submitted", search_query=topic, page_path="/", occurred_at=utcnow(),
            ),
            ActivityEvent(
                event_id=str(uuid4()), user_id=user_id, session_id=session_id,
                event_type="product_viewed", product_id=product.id, category=product.category,
                page_path=f"/products/{product.slug}", occurred_at=utcnow(),
            ),
            ActivityEvent(
                event_id=str(uuid4()), user_id=user_id, session_id=f"{session_id}-{index}",
                event_type="active_dwell", product_id=product.id, category=product.category,
                duration_ms=60_000, page_path=f"/products/{product.slug}", occurred_at=utcnow(),
            ),
        ])
    return events


def _ranking_snapshot(profile: UserInterestProfile, limit: int = 3) -> tuple[list[dict], dict]:
    ranked, retrieval = retrieve_and_rank(profile_to_dict(profile), limit=limit)
    return [
        {
            "product_id": item["id"],
            "title": item["title"],
            "category": item["category"],
            "rank": index,
            "final_score": item["final_score"],
        }
        for index, item in enumerate(ranked, start=1)
    ], retrieval


def evaluate_closed_loop_personalization(user_id: str) -> dict:
    """Exercise the real event→signal→profile→rank→feedback loop with synthetic time progression."""
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user or not user.personalization_enabled:
            return {"status": "unavailable", "reason": "An opted-in evaluation learner is required."}
        products = list(db.scalars(select(Product).where(Product.status == "active")).all())
        python_course = next(
            (product for product in products if product.category in {"Python Development", "Python for AI"}), None
        )
        mlops_course = next((product for product in products if product.category == "MLOps"), None)
        if not python_course or not mlops_course:
            return {"status": "unavailable", "reason": "Python and MLOps catalog evidence is required."}
        profile = db.get(UserInterestProfile, user_id) or UserInterestProfile(user_id=user_id, profile_version=0)
        db.add(profile)
        db.add_all(_stage_events(user_id, python_course, python_course.category, "python"))
        db.commit()
        _signals, profile = derive_signals(db, user_id)
        db.commit()
        stage_one_ranking, stage_one_retrieval = _ranking_snapshot(profile)
        stage_one = {
            "primary_intent": profile.primary_intent,
            "topic_weights": dict(profile.topic_weights or {}),
            "recommendations": stage_one_ranking,
        }

        # Simulate four elapsed days so the production 72-hour half-life—not a
        # second evaluation-only scoring system—governs the intent shift.
        old_time = utcnow() - timedelta(hours=96)
        for signal in db.scalars(select(BehavioralSignal).where(BehavioralSignal.user_id == user_id)).all():
            signal.last_observed_at = old_time
        db.add_all(_stage_events(user_id, mlops_course, "MLOps", "mlops", repeats=4))
        db.commit()
        _signals, profile = derive_signals(db, user_id)
        db.commit()
        stage_two_ranking, stage_two_retrieval = _ranking_snapshot(profile)
        stage_two = {
            "primary_intent": profile.primary_intent,
            "topic_weights": dict(profile.topic_weights or {}),
            "recommendations": stage_two_ranking,
        }
        if not stage_two_ranking:
            return {"status": "unavailable", "reason": "No eligible recommendation was produced after intent shift."}

        dismissed = stage_two_ranking[0]
        run = RecommendationRun(
            user_id=user_id,
            trigger_type="closed_loop_evaluation",
            trigger_reason="Persist valid recommendation evidence before feedback",
            idempotency_key=str(uuid4()),
            profile_hash=profile.profile_hash,
            status="succeeded",
            completed_at=utcnow(),
        )
        db.add(run)
        db.flush()
        recommendation = Recommendation(
            run_id=run.id,
            user_id=user_id,
            headline="Evaluation recommendation",
            narrative="A grounded recommendation used to exercise the existing feedback loop.",
            model="evaluation-no-generation",
            profile_snapshot=profile_to_dict(profile),
        )
        db.add(recommendation)
        db.flush()
        product = db.get(Product, dismissed["product_id"])
        db.add(RecommendationItem(
            recommendation_id=recommendation.id,
            product_id=product.id,
            rank=1,
            semantic_score=0,
            behavior_score=0,
            final_score=dismissed["final_score"],
            confidence_score=dismissed["final_score"],
            interest_likelihood=dismissed["final_score"],
            reason_codes=["EVALUATION_EVIDENCE"],
            explanation="Existing ranking selected this active catalog product.",
            product_version=product.version,
        ))
        db.add(ActivityEvent(
            event_id=str(uuid4()), user_id=user_id, session_id=f"evaluation-feedback-{uuid4()}",
            event_type="recommendation_dismissed", product_id=product.id,
            category=product.category, recommendation_id=recommendation.id, occurred_at=utcnow(),
        ))
        db.commit()
        _signals, profile = derive_signals(db, user_id)
        db.commit()
        stage_three_ranking, stage_three_retrieval = _ranking_snapshot(profile)
        stage_three_ids = [item["product_id"] for item in stage_three_ranking]
        return {
            "status": "ok",
            "half_life_hours": 72,
            "stage_one_python_interest": stage_one,
            "stage_two_mlops_shift": stage_two,
            "stage_three_after_not_for_me": {
                "dismissed_product_id": dismissed["product_id"],
                "dismissed_product_title": dismissed["title"],
                "negative_product_ids": list(profile.negative_product_ids or []),
                "recommendations": stage_three_ranking,
            },
            "evidence": {
                "primary_intent_changed": stage_one["primary_intent"] != stage_two["primary_intent"],
                "dismissed_product_excluded": dismissed["product_id"] not in stage_three_ids,
                "ranking_changed_after_shift": [item["product_id"] for item in stage_one_ranking]
                != [item["product_id"] for item in stage_two_ranking],
                "ranking_changed_after_feedback": [item["product_id"] for item in stage_two_ranking]
                != stage_three_ids,
                "semantic_status_by_stage": [
                    stage_one_retrieval.get("semantic_status"),
                    stage_two_retrieval.get("semantic_status"),
                    stage_three_retrieval.get("semantic_status"),
                ],
            },
        }
    finally:
        db.close()
