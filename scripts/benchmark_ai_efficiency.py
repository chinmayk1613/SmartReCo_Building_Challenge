import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SmartReco's isolated 100-event AI-efficiency benchmark.")
    parser.add_argument("--live-mesh", action="store_true", help="Opt in to real Mesh recommendation-copy calls")
    parser.add_argument("--json", action="store_true", help="Print machine-readable output")
    args = parser.parse_args()

    workspace = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(workspace))
    temporary = tempfile.TemporaryDirectory(prefix="smartreco-efficiency-", ignore_cleanup_errors=True)
    temp_root = Path(temporary.name)
    os.environ["DATABASE_URL"] = f"sqlite:///{(temp_root / 'benchmark.db').as_posix()}"
    os.environ["QDRANT_PATH"] = str(temp_root / "qdrant")
    os.environ["SCHEDULER_ENABLED"] = "false"
    os.environ["MESH_EMBEDDINGS_ENABLED"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGSMITH_API_KEY"] = ""
    if not args.live_mesh:
        os.environ["MESH_API_KEY"] = ""

    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.db import Base, SessionLocal, engine
    from app.dependencies import COOKIE_NAME
    from app.main import app
    from app.models import BehavioralSignal, Product, RecommendationRun, ServiceInvocation, User, UserInterestProfile, utcnow
    from app.schemas import ProductInput
    from app.security import create_session, hash_password
    from app.services.benchmarking import calculate_ai_efficiency, latency_summary
    from app.services.catalog import create_product
    from app.services.mesh import mesh_gateway
    from app.services.recommendation import (
        execute_contextual_recommendation,
        profile_to_dict,
        queue_contextual_recommendation,
        retrieve_and_rank,
    )
    from app.services.vector_store import get_vector_store, sync_pending_catalog
    from scripts.seed_demo import PRODUCTS

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        user = User(email="benchmark@smartreco.ai", display_name="Efficiency Benchmark", password_hash=hash_password("BenchmarkPass123!"))
        db.add(user)
        db.flush()
        db.add(UserInterestProfile(user_id=user.id, profile_version=0, journey_stage="exploration"))
        session, raw_token = create_session(user)
        db.add(session)
        for index, payload in enumerate(PRODUCTS[:20]):
            create_product(
                db,
                ProductInput(
                    **payload,
                    currency="USD",
                    status="active",
                    rating=round(4.5 + (index % 5) * 0.1, 1),
                    popularity=max(100, 900 - index * 35),
                ),
            )
        db.commit()
        user_id = user.id
        csrf = session.csrf_token
    finally:
        db.close()
    sync_pending_catalog(limit=100)

    db = SessionLocal()
    try:
        products = list(db.scalars(select(Product).where(Product.status == "active").order_by(Product.id)).all())
        signal_start = db.scalar(select(func.count(BehavioralSignal.id))) or 0
        run_start = db.scalar(select(func.count(RecommendationRun.id))) or 0
        benchmark_started = utcnow()
    finally:
        db.close()
    counters_before = mesh_gateway.counter_snapshot()

    raw_events: list[dict] = []
    queries = ["agentic AI workflows", "MLOps deployment", "web technologies", "Python backend"]
    for index in range(96):
        product = products[index % len(products)]
        event_type = (
            "search_submitted" if index % 12 == 0 else
            "active_dwell" if index % 10 == 0 else
            "product_clicked" if index % 7 == 0 else
            "product_viewed" if index % 5 == 0 else
            "page_viewed" if index % 3 == 0 else
            "product_impression"
        )
        payload = {
            "event_id": str(uuid4()),
            "event_type": event_type,
            "session_id": "browser-session",
            "product_id": product.id if event_type not in {"search_submitted", "page_viewed"} else None,
            "search_query": queries[(index // 12) % len(queries)] if event_type == "search_submitted" else None,
            "duration_ms": 45_000 + index * 100 if event_type == "active_dwell" else None,
            "page_path": f"/products/{product.slug}" if event_type != "search_submitted" else "/",
            "properties": {"checkpoint": True} if event_type == "active_dwell" else {},
        }
        raw_events.append(payload)
    raw_events.extend([dict(raw_events[0]), dict(raw_events[1])])
    raw_events.extend([
        {
            "event_id": str(uuid4()), "event_type": "active_dwell", "session_id": "browser-session",
            "product_id": products[0].id, "duration_ms": 1_000, "page_path": f"/products/{products[0].slug}",
        },
        {
            "event_id": str(uuid4()), "event_type": "product_viewed", "session_id": "browser-session",
            "product_id": str(uuid4()), "page_path": "/products/missing",
        },
    ])

    accepted = duplicates = rejected = http_batches = 0
    batch_latencies: list[float] = []
    duplicate_batch_latency = None
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, raw_token)
        for start in range(0, len(raw_events), 10):
            request_started = perf_counter()
            response = client.post(
                "/api/events/batch",
                headers={"X-CSRF-Token": csrf},
                json={"events": raw_events[start:start + 10]},
            )
            request_latency = (perf_counter() - request_started) * 1000
            batch_latencies.append(request_latency)
            response.raise_for_status()
            result = response.json()
            http_batches += 1
            accepted += result.get("accepted", 0)
            duplicates += result.get("duplicates", 0)
            rejected += result.get("rejected", 0)
            if result.get("duplicates", 0):
                duplicate_batch_latency = round(request_latency, 2)

    first_context = queue_contextual_recommendation(user_id, products[0].id)
    if first_context.get("created"):
        execute_contextual_recommendation(first_context["run_id"])
    cache_started = perf_counter()
    second_context = queue_contextual_recommendation(user_id, products[0].id)
    cache_latency_ms = round((perf_counter() - cache_started) * 1000, 2)
    contextual_cache_hits = int(second_context.get("cache") == "hit")

    db = SessionLocal()
    try:
        benchmark_profile = db.get(UserInterestProfile, user_id)
        profile_payload = profile_to_dict(benchmark_profile)
    finally:
        db.close()
    ranking_latencies: list[float] = []
    for _ in range(10):
        ranking_started = perf_counter()
        retrieve_and_rank(profile_payload, limit=3)
        ranking_latencies.append((perf_counter() - ranking_started) * 1000)

    counters_after = mesh_gateway.counter_snapshot()
    db = SessionLocal()
    try:
        signal_updates = (db.scalar(select(func.count(BehavioralSignal.id))) or 0) - signal_start
        generation_runs = (db.scalar(select(func.count(RecommendationRun.id))) or 0) - run_start
        new_invocations = list(
            db.scalars(
                select(ServiceInvocation)
                .where(ServiceInvocation.started_at >= benchmark_started)
            ).all()
        )
        trigger_evaluations = sum(
            row.service == "signals" and row.operation == "derive_behavior_profile" for row in new_invocations
        )
    finally:
        db.close()
    mesh_embedding_calls = counters_after["mesh_embedding_calls"] - counters_before["mesh_embedding_calls"]
    mesh_copy_calls = counters_after["mesh_copy_llm_calls"] - counters_before["mesh_copy_llm_calls"]
    external_mesh_latencies = [
        row.latency_ms
        for row in new_invocations
        if row.service == "llm" and row.model and not row.model.startswith("deterministic") and row.latency_ms is not None
    ]
    live_mesh_observed = bool(args.live_mesh and mesh_gateway.enabled)
    report = {
        "status": "ok",
        "mode": "live_mesh" if live_mesh_observed else "offline_no_mesh",
        "raw_browser_events": len(raw_events),
        "http_batches": http_batches,
        "accepted_events": accepted,
        "rejected_events": rejected,
        "duplicate_events": duplicates,
        "behavioral_signal_updates": signal_updates,
        "recommendation_trigger_evaluations": trigger_evaluations,
        "recommendation_generation_runs": generation_runs,
        "contextual_recommendation_cache_hits": contextual_cache_hits,
        "mesh_embedding_calls": mesh_embedding_calls,
        "mesh_recommendation_copy_llm_calls": mesh_copy_calls,
        "performance": {
            "batched_event_ingestion": latency_summary(batch_latencies),
            "duplicate_event_batch_latency_ms": duplicate_batch_latency,
            "cached_context_lookup_latency_ms": cache_latency_ms,
            "hybrid_retrieval_and_ranking": latency_summary(ranking_latencies),
            "external_mesh_llm": latency_summary(external_mesh_latencies),
            "timing_policy": "informational; no machine-dependent threshold",
        },
        **calculate_ai_efficiency(
            raw_events=len(raw_events),
            mesh_copy_llm_calls=mesh_copy_calls,
            live_mesh_observed=live_mesh_observed,
        ),
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("SmartReco isolated AI-efficiency benchmark")
        for key, value in report.items():
            print(f"{key.replace('_', ' ').title()}: {value}")

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
