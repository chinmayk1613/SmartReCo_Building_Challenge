"""Run one real LangGraph + Qdrant RAG + Mesh recommendation for the demo learner."""

from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Recommendation, RecommendationItem, RecommendationRun, User, UserInterestProfile
from app.services.recommendation import execute_recommendation_run


def main() -> None:
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == "learner@smartreco.local"))
        if not user:
            raise SystemExit("Demo learner is not seeded")
        profile = db.get(UserInterestProfile, user.id)
        if not profile:
            raise SystemExit("Demo learner has no behavior profile; replay a journey first")
        run = RecommendationRun(
            user_id=user.id,
            trigger_type="stack_verification",
            trigger_reason="Verify live Mesh, RAG, and LangGraph integration",
            idempotency_key=str(uuid4()).replace("-", ""),
            profile_hash=profile.profile_hash,
        )
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()

    execute_recommendation_run(run_id)

    db = SessionLocal()
    try:
        run = db.get(RecommendationRun, run_id)
        recommendation = db.scalar(select(Recommendation).where(Recommendation.run_id == run_id))
        item_count = (
            db.scalar(select(func.count(RecommendationItem.id)).where(RecommendationItem.recommendation_id == recommendation.id))
            if recommendation
            else 0
        )
        print(
            {
                "status": run.status,
                "node": run.current_node,
                "model": run.model,
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "rag_candidates": run.retrieval_metrics.get("semantic_candidates"),
                "recommended_items": item_count,
                "error": run.error_code,
            }
        )
        if run.status != "succeeded" or run.model == "deterministic-local-fallback":
            raise SystemExit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
