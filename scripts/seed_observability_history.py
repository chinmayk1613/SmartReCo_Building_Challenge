from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import ServiceInvocation, User


BATCH_ID = "demo-observability-2026-08-04-ist-v1"


def utc_at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 4, hour, minute, tzinfo=timezone.utc)


def seed() -> int:
    init_db()
    db = SessionLocal()
    try:
        already_seeded = any(
            (row.invocation_metadata or {}).get("demo_history_batch") == BATCH_ID
            for row in db.scalars(select(ServiceInvocation)).all()
        )
        if already_seeded:
            return 0

        learner = db.scalar(select(User).where(User.email == "learner@smartreco.local"))
        if learner is None:
            raise RuntimeError("Seed the demo learner before observability history.")

        rows: list[ServiceInvocation] = []
        hours = [9, 11, 14, 16, 18]
        for index, hour in enumerate(hours):
            specifications = [
                {
                    "service": "signals",
                    "operation": "derive_behavior_profile",
                    "latency_ms": 28 + index * 5,
                },
                {
                    "service": "rag",
                    "operation": "catalog_semantic_retrieval",
                    "latency_ms": 38 + index * 8,
                },
                {
                    "service": "rag",
                    "operation": "contextual_behavioral_courses",
                    "latency_ms": 52 + index * 9,
                },
                {
                    "service": "mcp",
                    "operation": "get_verified_product_details",
                    "latency_ms": 22 + index * 6,
                },
                {
                    "service": "langgraph",
                    "operation": "recommendation_workflow",
                    "latency_ms": 3900 + index * 620,
                    "status": "failed" if hour == 14 else "succeeded",
                    "error_code": "WorkflowTimeout" if hour == 14 else None,
                },
                {
                    "service": "llm",
                    "operation": "personalized_persuasive_copy_attempt",
                    "model": "minimax/m2-her" if hour == 18 else "tencent/hy3",
                    "input_tokens": 520 + index * 145,
                    "output_tokens": 1280 + index * 310,
                    "estimated_cost": 0.0,
                    "latency_ms": 7200 + index * 940,
                    "status": "failed" if hour == 14 else "succeeded",
                    "error_code": "APIStatusError" if hour == 14 else None,
                },
            ]
            for offset, specification in enumerate(specifications):
                started_at = utc_at(hour, offset * 3)
                latency_ms = specification["latency_ms"]
                status = specification.get("status", "succeeded")
                rows.append(ServiceInvocation(
                    user_id=learner.id,
                    service=specification["service"],
                    operation=specification["operation"],
                    status=status,
                    model=specification.get("model"),
                    input_tokens=specification.get("input_tokens", 0),
                    output_tokens=specification.get("output_tokens", 0),
                    estimated_cost=specification.get("estimated_cost"),
                    latency_ms=latency_ms,
                    invocation_metadata={
                        "demo_history_batch": BATCH_ID,
                        "reporting_timezone": "UTC",
                        "purpose": "Two-day observability trend",
                    },
                    error_code=specification.get("error_code"),
                    error_detail="Seeded historical failure for dashboard trend verification." if status == "failed" else None,
                    started_at=started_at,
                    completed_at=started_at + timedelta(milliseconds=latency_ms),
                ))

        db.add_all(rows)
        db.commit()
        return len(rows)
    finally:
        db.close()


if __name__ == "__main__":
    print(f"inserted={seed()}")
