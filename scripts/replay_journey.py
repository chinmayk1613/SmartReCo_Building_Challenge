import argparse
from datetime import timedelta
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import ActivityEvent, Product, User, utcnow
from app.services.recommendation import process_activity_and_maybe_recommend


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a judge-visible behavioral journey")
    parser.add_argument("--email", default="learner@smartreco.local")
    args = parser.parse_args()
    init_db()
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == args.email))
        if not user:
            raise SystemExit("Seed the demo first")
        langgraph = db.scalar(select(Product).where(Product.slug == "agentic-workflows-langgraph"))
        bootcamp = db.scalar(select(Product).where(Product.slug == "agentic-ai-bootcamp"))
        session_id = str(uuid4())
        now = utcnow()
        journey = [
            ("search_submitted", None, "advanced agentic ai", None, 0),
            ("product_viewed", langgraph.id, None, "Agentic AI", 5),
            ("active_dwell", langgraph.id, None, "Agentic AI", 55),
            ("added_to_cart", langgraph.id, None, "Agentic AI", 70),
            ("product_viewed", bootcamp.id, None, "Agentic AI", 80),
        ]
        for event_type, product_id, query, category, offset in journey:
            db.add(ActivityEvent(event_id=str(uuid4()), user_id=user.id, session_id=session_id, event_type=event_type, product_id=product_id, search_query=query, category=category, duration_ms=52_000 if event_type == "active_dwell" else None, occurred_at=now + timedelta(seconds=offset)))
        db.commit()
        user_id = user.id
    finally:
        db.close()
    print(process_activity_and_maybe_recommend(user_id))


if __name__ == "__main__":
    main()

