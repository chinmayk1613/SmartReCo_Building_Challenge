import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Demonstrate SmartReco's closed-loop behavioral personalization.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(workspace))
    temporary = tempfile.TemporaryDirectory(prefix="smartreco-intent-", ignore_cleanup_errors=True)
    temp_root = Path(temporary.name)
    os.environ["DATABASE_URL"] = f"sqlite:///{(temp_root / 'intent.db').as_posix()}"
    os.environ["QDRANT_PATH"] = str(temp_root / "qdrant")
    os.environ["SCHEDULER_ENABLED"] = "false"
    os.environ["MESH_API_KEY"] = ""
    os.environ["MESH_EMBEDDINGS_ENABLED"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"

    from app.db import Base, SessionLocal, engine
    from app.models import User, UserInterestProfile
    from app.schemas import ProductInput
    from app.security import hash_password
    from app.services.catalog import create_product
    from app.services.closed_loop_evaluation import evaluate_closed_loop_personalization
    from app.services.vector_store import get_vector_store, sync_pending_catalog
    from scripts.seed_demo import PRODUCTS

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        user = User(email="intent@smartreco.ai", display_name="Intent Evaluation", password_hash=hash_password("IntentPass123!"))
        db.add(user)
        db.flush()
        db.add(UserInterestProfile(user_id=user.id, profile_version=0, journey_stage="exploration"))
        for index, payload in enumerate(PRODUCTS):
            create_product(
                db,
                ProductInput(
                    **payload, currency="USD", status="active",
                    rating=round(4.5 + (index % 5) * 0.1, 1), popularity=max(100, 900 - index * 20),
                ),
            )
        db.commit()
        user_id = user.id
    finally:
        db.close()
    sync_pending_catalog(limit=100)
    report = evaluate_closed_loop_personalization(user_id)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("SmartReco closed-loop intent evolution")
        print(f"Status: {report['status']}")
        if report["status"] == "ok":
            print(f"Stage 1 intent: {report['stage_one_python_interest']['primary_intent']}")
            print(f"Stage 2 intent: {report['stage_two_mlops_shift']['primary_intent']}")
            print(f"Dismissed product excluded: {report['evidence']['dismissed_product_excluded']}")
            print(f"Ranking changed after shift: {report['evidence']['ranking_changed_after_shift']}")
            print(f"Ranking changed after feedback: {report['evidence']['ranking_changed_after_feedback']}")
    try:
        store = get_vector_store()
        close = getattr(store.client, "close", None)
        if close:
            close()
    finally:
        engine.dispose()
        temporary.cleanup()


if __name__ == "__main__":
    main()
