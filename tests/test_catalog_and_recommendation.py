from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import ActivityEvent, CatalogOutbox, Product, Recommendation, RecommendationItem, RecommendationRun, ServiceInvocation, UserInterestProfile
from app.schemas import ProductInput, RecommendationCopy
from app.services.catalog import archive_product, create_product, update_product
from app.services.recommendation import execute_recommendation_run, process_activity_and_maybe_recommend, profile_to_dict, queue_contextual_recommendation, retrieve_and_rank
from app.services.mesh import MeshResult, extract_first_json_object, mesh_gateway
from app.services.signals import derive_signals


def test_product_create_writes_transactional_outbox(db):
    product = create_product(db, ProductInput(title="Reliable RAG", slug="reliable-rag", description="A complete production course about reliable retrieval systems.", category="Generative AI", price=99))
    db.commit()
    outbox = db.scalar(select(CatalogOutbox).where(CatalogOutbox.product_id == product.id))
    assert outbox.product_version == 1
    assert outbox.event_type == "product.upsert"


def test_mesh_json_extraction_ignores_fences_and_trailing_model_commentary():
    content = '```json\n{"headline":"Path {one}","nested":{"value":"escaped \\\"quote\\\""}}\n```\nExtra explanation'
    assert extract_first_json_object(content) == '{"headline":"Path {one}","nested":{"value":"escaped \\\"quote\\\""}}'


def test_product_update_increments_version_and_outbox(db, products):
    product = products[0]
    data = ProductInput(title=product.title + " Updated", slug=product.slug, description=product.description, category=product.category, price=float(product.price), status="active")
    update_product(db, product, data); db.commit()
    assert product.version == 2
    assert db.query(CatalogOutbox).filter_by(product_id=product.id).count() == 2


def test_archived_product_creates_delete_event(db, products):
    product = products[0]
    data = ProductInput(title=product.title, slug=product.slug, description=product.description, category=product.category, price=float(product.price), status="archived")
    update_product(db, product, data); db.commit()
    event = db.scalar(select(CatalogOutbox).where(CatalogOutbox.product_id == product.id, CatalogOutbox.product_version == 2))
    assert event.event_type == "product.delete"


def test_explicit_archive_action_is_soft_delete_with_outbox(db, products):
    product = products[0]
    archive_product(db, product)
    db.commit()
    event = db.scalar(select(CatalogOutbox).where(CatalogOutbox.product_id == product.id, CatalogOutbox.product_version == 2))
    assert product.status == "archived"
    assert product.version == 2
    assert event.event_type == "product.delete"


@pytest.mark.parametrize("negative_index", [0, 1, 2, 3, 4, 5])
def test_ranking_excludes_negative_products(db, user, products, negative_index):
    from app.models import UserInterestProfile
    profile = UserInterestProfile(user_id=user.id, primary_intent="agentic_ai", category_weights={"agentic_ai": 1.0}, negative_product_ids=[products[negative_index].id], profile_hash=str(uuid4()))
    db.add(profile); db.commit()
    ranked, metrics = retrieve_and_rank(profile_to_dict(profile), limit=5)
    assert products[negative_index].id not in [item["id"] for item in ranked]
    assert metrics["eligible_candidates"] == 5


def test_recommendation_pipeline_persists_grounded_items(db, user, products):
    for event_type, product, duration in [
        ("search_submitted", None, None), ("product_clicked", products[0], None), ("active_dwell", products[0], 60_000), ("added_to_cart", products[0], None)
    ]:
        db.add(ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type=event_type, product_id=product.id if product else None, search_query="agentic ai" if event_type == "search_submitted" else None, category="Agentic AI", duration_ms=duration))
    db.commit()
    result = process_activity_and_maybe_recommend(user.id)
    assert result["triggered"] is True
    recommendation = db.scalar(select(Recommendation).where(Recommendation.user_id == user.id))
    items = list(db.scalars(select(RecommendationItem).where(RecommendationItem.recommendation_id == recommendation.id)).all())
    assert recommendation.model == "deterministic-local-fallback"
    assert 1 <= len(items) <= 5
    assert all(db.get(Product, item.product_id) is not None for item in items)
    assert len({item.product_id for item in items}) == len(items)


def test_course_scoped_graph_persists_source_course_and_verified_items(db, user, products):
    profile = UserInterestProfile(
        user_id=user.id,
        primary_intent="agentic_ai",
        category_weights={"agentic_ai": 1.0, "generative_ai": 0.8},
        recent_searches=["agent workflows"],
        profile_hash=str(uuid4()),
    )
    db.add(profile); db.commit()

    queued = queue_contextual_recommendation(user.id, products[0].id)
    assert queued["created"] is True
    execute_recommendation_run(queued["run_id"])
    db.expire_all()

    run = db.get(RecommendationRun, queued["run_id"])
    recommendation = db.scalar(select(Recommendation).where(Recommendation.run_id == run.id))
    items = list(db.scalars(select(RecommendationItem).where(RecommendationItem.recommendation_id == recommendation.id)).all())
    services = set(db.scalars(select(ServiceInvocation.service).where(ServiceInvocation.recommendation_run_id == run.id)).all())
    assert run.status == "succeeded"
    assert run.scope_key == f"course:{products[0].id}"
    assert run.context_product_id == products[0].id
    assert recommendation.user_id == user.id
    assert recommendation.recommendation_type == "contextual"
    assert recommendation.context_product_id == products[0].id
    assert 1 <= len(items) <= 3
    assert all(0 < item.confidence_score <= 1 for item in items)
    assert all(0 < item.interest_likelihood <= 1 for item in items)
    assert {"rag", "mcp", "llm", "langgraph"}.issubset(services)


def test_course_scoped_queue_is_single_flight(db, user, products):
    db.add(UserInterestProfile(
        user_id=user.id,
        primary_intent="data_engineering",
        category_weights={"data_engineering": 1.0},
        profile_hash=str(uuid4()),
    ))
    db.commit()
    first = queue_contextual_recommendation(user.id, products[4].id)
    second = queue_contextual_recommendation(user.id, products[4].id)
    active = list(db.scalars(select(RecommendationRun).where(
        RecommendationRun.user_id == user.id,
        RecommendationRun.scope_key == f"course:{products[4].id}",
        RecommendationRun.status.in_(["queued", "running"]),
    )).all())
    assert first["created"] is True
    assert second == {"created": False, "reason": "run_in_progress", "run_id": first["run_id"]}
    assert len(active) == 1


def test_unchanged_course_refresh_reuses_contextual_run(db, user, products):
    db.add(UserInterestProfile(
        user_id=user.id,
        primary_intent="data_engineering",
        category_weights={"data_engineering": 1.0},
        profile_hash=str(uuid4()),
    ))
    db.commit()

    first = queue_contextual_recommendation(user.id, products[4].id, visit_id="visit-one")
    duplicate = queue_contextual_recommendation(user.id, products[4].id, visit_id="visit-one")
    assert first["created"] is True
    assert duplicate["reason"] == "run_in_progress"
    execute_recommendation_run(first["run_id"])

    repeated_same_visit = queue_contextual_recommendation(user.id, products[4].id, visit_id="visit-one")
    second_visit = queue_contextual_recommendation(user.id, products[4].id, visit_id="visit-two")
    assert repeated_same_visit["created"] is False
    assert repeated_same_visit["reason"] == "current"
    assert repeated_same_visit["cache"] == "hit"
    assert second_visit["created"] is False
    assert second_visit["run_id"] == first["run_id"]
    assert second_visit["cache"] == "hit"


def test_contextual_workflow_attempts_exactly_one_mesh_model(db, user, products, monkeypatch):
    class ModelUnavailable(Exception):
        status_code = 503

    calls = []

    def fail_once(profile, selected, model=None, *, concise=False):
        calls.append({"model": model, "concise": concise})
        raise ModelUnavailable("provider unavailable")

    monkeypatch.setattr(mesh_gateway, "generate_copy", fail_once)
    db.add(UserInterestProfile(
        user_id=user.id,
        primary_intent="agentic_ai",
        category_weights={"agentic_ai": 1.0},
        profile_hash=str(uuid4()),
    ))
    db.commit()
    queued = queue_contextual_recommendation(user.id, products[0].id, visit_id="one-llm-call")
    execute_recommendation_run(queued["run_id"])

    run = db.get(RecommendationRun, queued["run_id"])
    recommendation = db.scalar(select(Recommendation).where(Recommendation.run_id == run.id))
    assert calls == [{"model": mesh_gateway.settings.mesh_free_model, "concise": True}]
    assert run.status == "succeeded"
    assert recommendation.model == "deterministic-provider-fallback"


def test_provider_connection_failure_uses_grounded_fallback(db, user, products, monkeypatch):
    def provider_down(*_args, **_kwargs):
        raise ConnectionError("provider unavailable")

    monkeypatch.setattr(mesh_gateway, "generate_copy", provider_down)
    db.add_all([
        ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="search_submitted", search_query="agentic ai", category="Agentic AI"),
        ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="product_clicked", product_id=products[0].id, category="Agentic AI"),
    ])
    db.commit()
    result = process_activity_and_maybe_recommend(user.id)
    run = db.scalar(select(RecommendationRun).where(RecommendationRun.id == result["run_id"]))
    recommendation = db.scalar(select(Recommendation).where(Recommendation.run_id == run.id))
    assert run.status == "succeeded"
    assert run.graph_state["provider_fallback"] is True
    assert run.graph_state["model_attempts"][0]["failure_scope"] == "mesh_gateway_unreachable"
    assert len(run.graph_state["model_attempts"]) == 1
    assert recommendation.model == "deterministic-provider-fallback"


def test_model_failure_advances_to_next_mesh_model_with_per_attempt_tracing(db, user, products, monkeypatch):
    class ModelRejected(Exception):
        status_code = 400

    monkeypatch.setattr(mesh_gateway.settings, "mesh_free_model", "mesh/model-a")
    monkeypatch.setattr(mesh_gateway.settings, "mesh_failover_models", "mesh/model-a,mesh/model-b,mesh/model-c")

    def generate_with_failover(profile, selected, model=None):
        if model == "mesh/model-a":
            raise ModelRejected("model is unavailable")
        return MeshResult(
            data=RecommendationCopy(
                headline="A grounded next step",
                narrative="Your behavior supports these verified catalog choices.",
                item_copy=[{"product_id": item["id"], "reason": item["default_reason"]} for item in selected],
            ),
            model=model,
            input_tokens=20,
            output_tokens=10,
        )

    monkeypatch.setattr(mesh_gateway, "generate_copy", generate_with_failover)
    db.add_all([
        ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="search_submitted", search_query="agentic ai", category="Agentic AI"),
        ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="product_clicked", product_id=products[0].id, category="Agentic AI"),
    ])
    db.commit()

    result = process_activity_and_maybe_recommend(user.id)
    run = db.get(RecommendationRun, result["run_id"])
    recommendation = db.scalar(select(Recommendation).where(Recommendation.run_id == run.id))
    attempts = list(db.scalars(
        select(ServiceInvocation)
        .where(ServiceInvocation.recommendation_run_id == run.id, ServiceInvocation.service == "llm")
        .order_by(ServiceInvocation.started_at)
    ).all())

    assert recommendation.model == "mesh/model-b"
    assert run.graph_state["model_failover_used"] is True
    assert [attempt.model for attempt in attempts] == ["mesh/model-a", "mesh/model-b"]
    assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert all(attempt.workload == "recommendation" for attempt in attempts)
    assert len({attempt.correlation_id for attempt in attempts}) == 2
    assert all(attempt.langsmith_export_status == "disabled" for attempt in attempts)
    assert attempts[0].failover_decision == "try_next_model"
    assert attempts[1].failover_decision == "not_needed"
    assert attempts[0].invocation_metadata["failure_scope"] == "model_rejected_request"
