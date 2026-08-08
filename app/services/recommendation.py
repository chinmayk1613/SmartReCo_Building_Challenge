import hashlib
import json
import math
import os
import re
from datetime import timedelta
from typing import TypedDict

from app.config import get_settings

_langsmith_settings = get_settings()
if _langsmith_settings.langsmith_connected:
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_API_KEY", _langsmith_settings.langsmith_api_key or "")
    os.environ.setdefault("LANGSMITH_PROJECT", _langsmith_settings.langsmith_project)
    # LangGraph may create framework-level spans in addition to our sanitized
    # @traceable boundaries. Hide their raw state globally; useful metrics live
    # in pseudonymous metadata and the local authorized invocation ledger.
    os.environ.setdefault("LANGSMITH_HIDE_INPUTS", "true")
    os.environ.setdefault("LANGSMITH_HIDE_OUTPUTS", "true")

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langsmith import get_current_run_tree, traceable
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import (
    ActivityEvent,
    Product,
    Recommendation,
    RecommendationItem,
    RecommendationRun,
    User,
    UserInterestProfile,
    utcnow,
)
from app.schemas import RecommendationCopy
from app.services.mesh import MeshResult, classify_mesh_error, mesh_gateway
from app.services.mcp_catalog import get_verified_product_details
from app.services.observability import begin_invocation, estimate_cost, finish_invocation, link_langsmith_span
from app.services.signals import derive_signals
from app.services.catalog import catalog_revision
from app.services.topics import normalize_topic, topics_overlap
from app.services.vector_store import DEGRADED, SEMANTIC, UNAVAILABLE, SemanticSearchResult, get_vector_store


PROMPT_VERSION = "v3-grounded-data"


def _trace_user_id(user_id: str) -> str:
    secret = get_settings().app_secret
    return "learner_" + hashlib.sha256(f"{secret}:{user_id}".encode()).hexdigest()[:16]


class RecommendationState(TypedDict, total=False):
    run_id: str
    user_id: str
    context_product_id: str
    profile: dict
    query: str
    candidates: list[dict]
    selected: list[dict]
    copy: dict
    model: str
    usage: dict
    validation_errors: list[str]
    fallback_used: bool
    retrieval_attempt: int
    retrieval_quality: dict
    refined_query: str


def _trace_state_inputs(inputs: dict) -> dict:
    state = inputs if isinstance(inputs, dict) else {}
    profile = state.get("profile") or {}
    return {
        "run_id": state.get("run_id"),
        "context_product_id": state.get("context_product_id"),
        "profile_version": profile.get("profile_version"),
        "candidate_count": len(state.get("selected") or state.get("candidates") or []),
        "retrieval_attempt": state.get("retrieval_attempt", 0),
    }


def _trace_state_outputs(outputs: dict) -> dict:
    state = outputs if isinstance(outputs, dict) else {}
    return {
        "candidate_count": len(state.get("selected") or state.get("candidates") or []),
        "model": state.get("model"),
        "fallback_used": bool(state.get("fallback_used")),
        "validation_error_count": len(state.get("validation_errors") or []),
    }


def _semantic_search(query: str, limit: int = 40) -> SemanticSearchResult:
    try:
        return get_vector_store().search_with_status(query, limit=limit)
    except Exception as exc:
        return SemanticSearchResult([], UNAVAILABLE, "qdrant", type(exc).__name__)


def profile_to_dict(profile: UserInterestProfile) -> dict:
    return {
        "profile_version": profile.profile_version,
        "primary_intent": profile.primary_intent,
        "secondary_intents": profile.secondary_intents,
        "category_weights": profile.category_weights,
        "recent_searches": profile.recent_searches,
        "positive_product_ids": profile.positive_product_ids,
        "negative_product_ids": profile.negative_product_ids,
        "journey_stage": profile.journey_stage,
        "confidence": profile.confidence,
        "profile_hash": profile.profile_hash,
    }


def saved_and_purchased_product_ids(user_id: str) -> tuple[set[str], set[str]]:
    """Rebuild current cart and purchase state from the learner's event stream."""
    db = SessionLocal()
    try:
        events = list(
            db.scalars(
                select(ActivityEvent)
                .where(
                    ActivityEvent.user_id == user_id,
                    ActivityEvent.product_id.is_not(None),
                    ActivityEvent.event_type.in_(["added_to_cart", "removed_from_cart", "purchase_completed"]),
                )
                .order_by(ActivityEvent.id)
            ).all()
        )
    finally:
        db.close()
    saved: set[str] = set()
    purchased: set[str] = set()
    for event in events:
        if not event.product_id:
            continue
        if event.event_type == "added_to_cart":
            saved.add(event.product_id)
        elif event.event_type == "removed_from_cart":
            saved.discard(event.product_id)
        elif event.event_type == "purchase_completed":
            purchased.add(event.product_id)
    return saved - purchased, purchased


def current_cart_product_ids(user_id: str) -> set[str]:
    saved, _purchased = saved_and_purchased_product_ids(user_id)
    return saved


def saved_or_purchased_product_ids(user_id: str) -> set[str]:
    """Return courses that cannot currently be recommended."""
    saved, purchased = saved_and_purchased_product_ids(user_id)
    return saved | purchased


def build_behavioral_query(profile: dict) -> str:
    primary = (profile.get("primary_intent") or "popular practical courses").replace("_", " ")
    secondary = ", ".join(item["topic"].replace("_", " ") for item in profile.get("secondary_intents", []))
    searches = ", ".join(profile.get("recent_searches", [])[:5])
    return (
        f"Primary learning goal: {primary}. Secondary interests: {secondary or 'none yet'}. "
        f"Recent searches: {searches or 'none'}. Journey stage: {profile.get('journey_stage', 'exploration')}."
    )


def retrieve_and_rank(profile: dict, limit: int = 5, *, query_override: str | None = None) -> tuple[list[dict], dict]:
    db = SessionLocal()
    try:
        query = query_override or build_behavioral_query(profile)
        semantic_result = _semantic_search(query)
        semantic = semantic_result.items
        semantic_map = {item["product_id"]: item["semantic_score"] for item in semantic}
        active = list(db.scalars(select(Product).where(Product.status == "active")).all())
        negative = set(profile.get("negative_product_ids") or []) | set(profile.get("excluded_product_ids") or [])
        weights = profile.get("category_weights") or {}
        scored: list[dict] = []
        for product in active:
            if product.id in negative:
                continue
            category_key = normalize_topic(product.category)
            topic_match = max(
                [float(score) for topic, score in weights.items() if topics_overlap(str(topic), category_key)] or [0.0]
            )
            semantic_score = semantic_map.get(product.id, 0.0)
            search_match = 0.0
            haystack = f"{product.title} {product.description} {' '.join(product.skills or [])}".lower()
            for search in profile.get("recent_searches", []):
                terms = [term for term in search.lower().split() if len(term) > 2]
                if terms:
                    search_match = max(search_match, sum(term in haystack for term in terms) / len(terms))
            popularity = min(1.0, float(product.popularity) / 1000) if product.popularity else 0.0
            quality = float(product.rating) / 5
            behavior_score = min(1.0, max(0.0, topic_match))
            final = 0.42 * semantic_score + 0.25 * behavior_score + 0.18 * search_match + 0.08 * quality + 0.07 * popularity
            reason_codes = []
            if semantic_score > 0.5:
                reason_codes.append("SEMANTIC_MATCH")
            if behavior_score > 0.2:
                reason_codes.append("TOPIC_MATCH")
            if search_match > 0.3:
                reason_codes.append("SEARCH_MATCH")
            if product.id in (profile.get("positive_product_ids") or []):
                final += 0.08
                reason_codes.append("ENGAGEMENT_MATCH")
            scored.append(
                {
                    "id": product.id,
                    "title": product.title,
                    "slug": product.slug,
                    "description": product.description,
                    "category": product.category,
                    "level": product.level,
                    "price": float(product.price),
                    "currency": product.currency,
                    "version": product.version,
                    "semantic_score": round(semantic_score, 5),
                    "behavior_score": round(behavior_score, 5),
                    "final_score": round(final, 5),
                    "reason_codes": reason_codes or ["DIVERSE_DISCOVERY"],
                    "default_reason": f"It connects your recent interest in {(profile.get('primary_intent') or product.category).replace('_', ' ')} with {product.title}.",
                }
            )
        scored.sort(key=lambda item: item["final_score"], reverse=True)
        selected: list[dict] = []
        category_counts: dict[str, int] = {}
        for candidate in scored:
            count = category_counts.get(candidate["category"], 0)
            if count >= 2:
                continue
            selected.append(candidate)
            category_counts[candidate["category"]] = count + 1
            if len(selected) >= limit:
                break
        if len(selected) < min(limit, len(scored)):
            for candidate in scored:
                if candidate not in selected:
                    selected.append(candidate)
                if len(selected) >= limit:
                    break
        metrics = {
            "query": query,
            "semantic_candidates": len(semantic),
            "eligible_candidates": len(scored),
            "selected_count": len(selected),
            "fallback_used": len(semantic) == 0,
            "semantic_status": semantic_result.status,
            "semantic_provider": semantic_result.provider,
            "semantic_error_code": semantic_result.error_code,
            "verified_semantic_candidates": sum(product_id in {p.id for p in active} for product_id in semantic_map),
            "discarded_vector_product_ids": sum(product_id not in {p.id for p in active} for product_id in semantic_map),
        }
        return selected, metrics
    finally:
        db.close()


CATEGORY_LEARNING_PATHS: dict[str, set[str]] = {
    "Agentic AI": {"Large Language Models", "Generative AI", "Python for AI", "Cloud & DevOps"},
    "Cloud & DevOps": {"MLOps", "Data Engineering", "Web Technologies", "Java Development", "Python Development"},
    "Data Engineering": {"Scala Development", "MLOps", "Cloud & DevOps", "Python Development", "Python for AI"},
    "Generative AI": {"Large Language Models", "Agentic AI", "Python for AI", "MLOps"},
    "Java Development": {"Web Technologies", "Cloud & DevOps", "Scala Development", "Python Development"},
    "Large Language Models": {"Generative AI", "Agentic AI", "MLOps", "Python for AI"},
    "MLOps": {"Data Engineering", "Cloud & DevOps", "Python for AI", "Large Language Models"},
    "Python Development": {"Python for AI", "Web Technologies", "Data Engineering", "Cloud & DevOps"},
    "Python for AI": {"Python Development", "MLOps", "Data Engineering", "Generative AI", "Large Language Models"},
    "Scala Development": {"Data Engineering", "Java Development", "Cloud & DevOps"},
    "Web Technologies": {"Java Development", "Python Development", "Cloud & DevOps"},
}

_SKILL_STOP_WORDS = {
    "and", "advanced", "beginner", "building", "course", "development", "for", "foundation",
    "fundamentals", "intermediate", "introduction", "production", "systems", "testing", "the", "with",
}


def _category_key(value: str) -> str:
    return normalize_topic(value)


def _skill_tokens(values: list[str] | None) -> set[str]:
    return {
        token
        for value in (values or [])
        for token in re.findall(r"[a-z0-9+#.]+", value.lower())
        if len(token) > 1 and token not in _SKILL_STOP_WORDS
    }


def _topic_affinity(profile: dict, product: Product) -> float:
    """Measure observed learner interest in this exact candidate, category, or search vocabulary."""
    weights = profile.get("category_weights") or {}
    category_key = _category_key(product.category)
    category_score = max(
        [
            float(score)
            for topic, score in weights.items()
            if _category_key(str(topic)) in category_key or category_key in _category_key(str(topic))
        ]
        or [0.0]
    )
    haystack = f"{product.title} {product.description} {' '.join(product.skills or [])}".lower()
    search_score = 0.0
    for search in profile.get("recent_searches") or []:
        terms = [term for term in re.findall(r"[a-z0-9+#.]+", search.lower()) if len(term) > 2]
        if terms:
            search_score = max(search_score, sum(term in haystack for term in terms) / len(terms))
    engagement_score = 1.0 if product.id in set(profile.get("positive_product_ids") or []) else 0.0
    return min(1.0, max(0.0, 0.65 * min(1.0, category_score) + 0.25 * search_score + 0.10 * engagement_score))


def _level_progression(current_level: str, candidate_level: str) -> float:
    progression = {
        "beginner": {"beginner": 0.70, "intermediate": 1.00, "advanced": 0.45},
        "intermediate": {"beginner": 0.35, "intermediate": 0.80, "advanced": 1.00},
        "advanced": {"beginner": 0.20, "intermediate": 0.65, "advanced": 0.85},
    }
    return progression.get(current_level.lower(), {}).get(candidate_level.lower(), 0.50)


def retrieve_contextual_courses(
    user_id: str,
    current_product_id: str,
    limit: int = 3,
    *,
    record_invocation: bool = True,
    query_override: str | None = None,
) -> tuple[list[dict], dict]:
    """Blend current-course relevance with learner behavior without becoming a category lookup."""
    handle = begin_invocation("rag", "contextual_behavioral_courses", user_id=user_id) if record_invocation else None
    db = SessionLocal()
    try:
        current = db.get(Product, current_product_id)
        if not current or current.status != "active":
            raise ValueError("Current course is not available")
        profile_row = db.get(UserInterestProfile, user_id)
        profile = profile_to_dict(profile_row) if profile_row else {}
        excluded = saved_or_purchased_product_ids(user_id) | set(profile.get("negative_product_ids") or []) | {current.id}
        query = query_override or (
            f"Find courses similar to {current.title}. Domain: {current.category}. Level: {current.level}. "
            f"Skills: {', '.join(current.skills or [])}. Description: {current.description}"
        )
        semantic_result = _semantic_search(query)
        semantic = semantic_result.items
        semantic_map = {item["product_id"]: item["semantic_score"] for item in semantic}
        current_skills = _skill_tokens(current.skills)
        adjacent_categories = CATEGORY_LEARNING_PATHS.get(current.category, set())
        scored: list[dict] = []
        candidates = list(db.scalars(select(Product).where(Product.status == "active", Product.id.not_in(excluded))).all())
        for product in candidates:
            candidate_skills = _skill_tokens(product.skills)
            shared_skills = sorted(current_skills & candidate_skills)
            skill_overlap = len(shared_skills) / max(1, len(current_skills | candidate_skills))
            semantic_score = semantic_map.get(product.id, 0.0)
            same_category = product.category == current.category
            adjacent_category = product.category in adjacent_categories
            if same_category:
                path_relevance = 1.0
            elif adjacent_category:
                path_relevance = 0.72
            elif shared_skills:
                path_relevance = 0.58
            else:
                # Strong global interest is not sufficient: every detail-page result must
                # first be defensibly connected to the course the learner is viewing.
                continue
            behavior_score = _topic_affinity(profile, product)
            progression_score = _level_progression(current.level, product.level)
            final = (
                0.40 * semantic_score
                + 0.27 * path_relevance
                + 0.18 * behavior_score
                + 0.10 * skill_overlap
                + 0.05 * progression_score
            )
            evidence_components = sum(
                [semantic_score >= 0.45, path_relevance >= 0.70, behavior_score >= 0.20, skill_overlap > 0]
            )
            confidence_score = min(
                0.98,
                0.35 * semantic_score
                + 0.35 * path_relevance
                + 0.15 * behavior_score
                + 0.10 * progression_score
                + 0.05 * (evidence_components / 4),
            )
            # Ranking likelihood is an interpretable fit score, not a calibrated
            # purchase probability. It converts the hybrid score to a 0..1 scale.
            interest_likelihood = 1 / (1 + math.exp(-8 * (final - 0.42)))
            if not same_category and behavior_score >= 0.20:
                relation = (
                    f"A personalized bridge from {current.title} into your observed interest in "
                    f"{product.category}."
                )
            elif same_category and behavior_score >= 0.20:
                relation = (
                    f"It builds from {current.title} and is strengthened by your recent "
                    f"{product.category} activity."
                )
            elif shared_skills:
                relation = f"It advances the learning path from {current.title} through {', '.join(shared_skills[:2])}."
            elif adjacent_category:
                relation = f"It is a practical next step from {current.category} into {product.category}."
            else:
                relation = f"It extends a shared skill from {current.title}."
            reason_codes = ["COURSE_CONTEXT"]
            if same_category:
                reason_codes.append("SAME_DOMAIN")
            if adjacent_category:
                reason_codes.append("LEARNING_PATH")
            if behavior_score >= 0.20:
                reason_codes.append("BEHAVIOR_MATCH")
            if shared_skills:
                reason_codes.append("SKILL_OVERLAP")
            scored.append({
                "id": product.id,
                "title": product.title,
                "slug": product.slug,
                "description": product.description,
                "category": product.category,
                "level": product.level,
                "skills": product.skills,
                "outcomes": product.outcomes,
                "price": float(product.price),
                "currency": product.currency,
                "version": product.version,
                "semantic_score": round(semantic_score, 5),
                "behavior_score": round(behavior_score, 5),
                "context_score": round(final, 5),
                "final_score": round(final, 5),
                "confidence_score": round(confidence_score, 5),
                "interest_likelihood": round(interest_likelihood, 5),
                "category_match": same_category,
                "path_relevance": round(path_relevance, 5),
                "shared_skills": shared_skills,
                "reason_codes": reason_codes,
                "explanation": relation,
                "default_reason": relation,
            })
        scored.sort(key=lambda item: item["context_score"], reverse=True)
        selected: list[dict] = []
        category_counts: dict[str, int] = {}
        for candidate in scored:
            # Prevent “the other courses in this department” from occupying the
            # entire panel. The third result must add a related learning-path angle.
            if category_counts.get(candidate["category"], 0) >= 2:
                continue
            selected.append(candidate)
            category_counts[candidate["category"]] = category_counts.get(candidate["category"], 0) + 1
            if len(selected) >= limit:
                break
        same_domain = [item for item in scored if item["category_match"]]
        metrics = {
            "current_product_id": current.id,
            "current_category": current.category,
            "semantic_candidates": len(semantic),
            "eligible_after_relevance_gate": len(scored),
            "same_domain_candidates": len(same_domain),
            "selected_count": len(selected),
            "same_domain_selected": sum(item["category_match"] for item in selected),
            "behavioral_bridge_selected": any(
                not item["category_match"] and item["behavior_score"] >= 0.20 for item in selected
            ),
            "ranking_mode": "contextual_behavioral_hybrid",
            "score_definition": "current-course semantic intent + path relevance + accumulated behavior + shared skills + level progression",
            "likelihood_is_calibrated_probability": False,
            "semantic_status": semantic_result.status,
            "semantic_provider": semantic_result.provider,
            "semantic_error_code": semantic_result.error_code,
            "discarded_vector_product_ids": sum(
                item["product_id"] not in {product.id for product in candidates} | {current.id} for item in semantic
            ),
        }
        if handle:
            finish_invocation(handle, metadata=metrics)
        return selected, metrics
    except Exception as exc:
        if handle:
            finish_invocation(handle, status="failed", error=exc)
        raise
    finally:
        db.close()


def _update_run(run_id: str, node: str, **values) -> None:
    db = SessionLocal()
    try:
        run = db.get(RecommendationRun, run_id)
        if run:
            run.current_node = node
            for key, value in values.items():
                if key == "graph_state":
                    run.graph_state = {**(run.graph_state or {}), **(value or {})}
                else:
                    setattr(run, key, value)
            db.commit()
    finally:
        db.close()


def load_context_node(state: RecommendationState) -> RecommendationState:
    db = SessionLocal()
    try:
        run = db.get(RecommendationRun, state["run_id"])
        profile = db.get(UserInterestProfile, state["user_id"])
        if not profile:
            raise ValueError("Behavioral profile not found")
        payload = profile_to_dict(profile)
        payload["excluded_product_ids"] = sorted(saved_or_purchased_product_ids(state["user_id"]))
        result: RecommendationState = {"profile": payload, "query": build_behavioral_query(payload)}
        if run and run.context_product_id:
            current = db.get(Product, run.context_product_id)
            if not current or current.status != "active":
                raise ValueError("Context course is not available")
            payload["context_course"] = {
                "id": current.id,
                "title": current.title,
                "category": current.category,
                "level": current.level,
                "skills": current.skills,
                "description": current.description,
                "outcomes": current.outcomes,
            }
            result["context_product_id"] = current.id
        _update_run(state["run_id"], "load_context", graph_state={"profile": payload})
        return result
    finally:
        db.close()


@traceable(name="smartreco-rag-retrieval", run_type="retriever", process_inputs=_trace_state_inputs, process_outputs=_trace_state_outputs)
def retrieve_node(state: RecommendationState) -> RecommendationState:
    contextual = bool(state.get("context_product_id"))
    operation = "contextual_catalog_retrieval" if contextual else "catalog_semantic_retrieval"
    _update_run(state["run_id"], "retrieve_and_rank")
    handle = begin_invocation("rag", operation, user_id=state["user_id"], run_id=state["run_id"])
    try:
        attempt = int(state.get("retrieval_attempt", 0)) + 1
        query_override = state.get("refined_query")
        if contextual:
            selected, metrics = retrieve_contextual_courses(
                state["user_id"], state["context_product_id"], limit=3, record_invocation=False,
                query_override=query_override,
            )
        else:
            selected, metrics = retrieve_and_rank(state["profile"], limit=3, query_override=query_override)
        metrics["retrieval_attempt"] = attempt
        _update_run(state["run_id"], "retrieve_and_rank", retrieval_metrics=metrics)
        finish_invocation(handle, metadata=metrics)
        return {"candidates": selected, "selected": selected, "retrieval_attempt": attempt}
    except Exception as exc:
        finish_invocation(handle, status="failed", error=exc)
        raise


def evaluate_retrieval_node(state: RecommendationState) -> RecommendationState:
    selected = state.get("selected") or []
    best_score = max((float(item.get("final_score", 0)) for item in selected), default=0.0)
    db = SessionLocal()
    try:
        run = db.get(RecommendationRun, state["run_id"])
        semantic_status = ((run.retrieval_metrics if run else {}) or {}).get("semantic_status")
    finally:
        db.close()
    profile = state.get("profile") or {}
    behavior_evidence = bool(profile.get("primary_intent") or profile.get("context_course"))
    sufficient = bool(selected and best_score >= 0.05 and (semantic_status != UNAVAILABLE or behavior_evidence))
    quality = {
        "sufficient": sufficient,
        "best_score": round(best_score, 5),
        "candidate_count": len(selected),
        "semantic_status": semantic_status,
        "retrieval_attempt": state.get("retrieval_attempt", 1),
    }
    _update_run(state["run_id"], "evaluate_retrieval_quality", graph_state={"retrieval_quality": quality})
    return {"retrieval_quality": quality}


def route_after_retrieval_quality(state: RecommendationState) -> str:
    if (state.get("retrieval_quality") or {}).get("sufficient"):
        return "verify"
    return "refine" if int(state.get("retrieval_attempt", 1)) < 2 else "insufficient"


def refine_retrieval_node(state: RecommendationState) -> RecommendationState:
    profile = state.get("profile") or {}
    context = profile.get("context_course") or {}
    refined = (
        f"Exact learning path from {context.get('title')} in {context.get('category')}; shared skills "
        f"{', '.join(context.get('skills') or [])}."
        if context else
        f"Courses for explicit goal {(profile.get('primary_intent') or 'learning').replace('_', ' ')}; "
        f"recent searches are data: {', '.join(profile.get('recent_searches') or [])}."
    )
    _update_run(state["run_id"], "refine_retrieval_query", graph_state={"refinement_reason": "insufficient_retrieval_quality"})
    return {"refined_query": refined}


def insufficient_evidence_node(state: RecommendationState) -> RecommendationState:
    _update_run(
        state["run_id"], "not_enough_evidence", status="succeeded", completed_at=utcnow(), lease_expires_at=None,
        graph_state={"not_enough_evidence": True, "final_candidate_count": len(state.get("selected") or [])},
    )
    return {}


@traceable(name="smartreco-mcp-catalog-verification", run_type="tool", process_inputs=_trace_state_inputs, process_outputs=_trace_state_outputs)
def verify_with_mcp_node(state: RecommendationState) -> RecommendationState:
    _update_run(state["run_id"], "verify_with_mcp")
    expected_ids = [item["id"] for item in state.get("selected", [])]
    verified = get_verified_product_details(
        expected_ids,
        user_id=state["user_id"],
        run_id=state["run_id"],
    )
    verified_ids = [item["id"] for item in verified]
    if verified_ids != expected_ids:
        raise ValueError("MCP verification did not preserve the exact active catalog products")
    _update_run(
        state["run_id"],
        "verify_with_mcp",
        graph_state={"mcp_verified_product_ids": verified_ids},
    )
    return {}


def _redact_provider_inputs(inputs: dict) -> dict:
    products = inputs.get("products") or []
    profile = inputs.get("profile") or {}
    return {
        "messages": [{
            "role": "user",
            "content": [{
                "type": "text",
                "text": (
                    f"Generate grounded recommendation copy for {len(products)} catalog courses; "
                    f"behavior profile version {profile.get('profile_version', 'current')}."
                ),
            }],
        }]
    }


def _redact_provider_outputs(result: MeshResult | None) -> dict:
    if result is None:
        return {
            "messages": [{
                "role": "assistant",
                "content": [{"type": "text", "text": "Provider attempt failed before grounded copy was returned."}],
            }],
            "usage_metadata": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
    return {
        "messages": [{
            "role": "assistant",
            "content": [{"type": "text", "text": "Grounded recommendation copy generated and validated locally."}],
        }],
        "usage_metadata": {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.input_tokens + result.output_tokens,
        },
    }


@traceable(
    name="smartreco-mesh-provider-attempt",
    run_type="llm",
    process_inputs=_redact_provider_inputs,
    process_outputs=_redact_provider_outputs,
)
def execute_traced_mesh_attempt(
    *,
    profile: dict,
    products: list[dict],
    model: str,
    concise: bool,
    handle,
    user_id: str,
    recommendation_run_id: str | None,
    attempt_number: int,
    workload: str,
) -> MeshResult:
    """Execute exactly one Mesh request and bind it to exactly one LangSmith LLM span."""
    trace = get_current_run_tree()
    if trace:
        trace.add_metadata({
            "telemetry_schema": "provider-attempt-v1",
            "local_invocation_id": handle.id,
            "local_correlation_id": handle.correlation_id,
            "user_ref": _trace_user_id(user_id),
            "recommendation_run_id": recommendation_run_id,
            "attempt_number": attempt_number,
            "workload": workload,
            "ls_provider": "mesh_api",
            "ls_model_name": model,
        })
        link_langsmith_span(
            handle,
            trace_id=str(trace.trace_id or trace.id),
            run_id=str(trace.id),
        )
    result = (
        mesh_gateway.generate_copy(profile, products, model=model, concise=True)
        if concise else mesh_gateway.generate_copy(profile, products, model=model)
    )
    if trace:
        trace.set(usage_metadata={
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.input_tokens + result.output_tokens,
        })
        trace.add_metadata({"provider_receipt": result.request_id, "provider_model": result.model})
    return result


@traceable(name="smartreco-generate-copy", run_type="chain", process_inputs=_trace_state_inputs, process_outputs=_trace_state_outputs)
def generate_node(state: RecommendationState) -> RecommendationState:
    if not state.get("selected"):
        raise ValueError("No eligible catalog products")
    settings = get_settings()
    # A course-page visit makes exactly one provider call. Overall-profile runs
    # retain the three-model failover chain because they are not user-blocking.
    models = [settings.mesh_free_model] if state.get("context_product_id") else settings.model_failover_chain
    attempts: list[dict] = []
    result = None
    last_error: Exception | None = None
    for attempt_number, model in enumerate(models, start=1):
        _update_run(
            state["run_id"],
            "generate_copy",
            graph_state={"current_model_attempt": attempt_number, "current_model": model},
        )
        handle = begin_invocation(
            "llm",
            "personalized_persuasive_copy_attempt",
            user_id=state["user_id"],
            run_id=state["run_id"],
            model=model,
            workload="recommendation",
            attempt_number=attempt_number,
            metadata={
                "provider": "Mesh API",
                "grounded_products": len(state["selected"]),
                "attempt": attempt_number,
                "chain_size": len(models),
            },
        )
        try:
            candidate_result = execute_traced_mesh_attempt(
                profile=state["profile"],
                products=state["selected"],
                model=model,
                concise=bool(state.get("context_product_id")),
                handle=handle,
                user_id=state["user_id"],
                recommendation_run_id=state["run_id"],
                attempt_number=attempt_number,
                workload="recommendation",
                langsmith_extra={
                    "tags": ["smartreco", "mesh", "provider-attempt"],
                    "metadata": {
                        "telemetry_schema": "provider-attempt-v1",
                        "local_invocation_id": handle.id,
                        "local_correlation_id": handle.correlation_id,
                        "user_ref": _trace_user_id(state["user_id"]),
                        "recommendation_run_id": state["run_id"],
                        "attempt_number": attempt_number,
                        "workload": "recommendation",
                        "ls_provider": "mesh_api",
                        "ls_model_name": model,
                    },
                },
            )
        except Exception as exc:
            last_error = exc
            classification = classify_mesh_error(exc)
            attempt = {
                "attempt": attempt_number,
                "model": model,
                "status": "failed",
                "error_code": type(exc).__name__,
                "failure_scope": classification.failure_scope,
                "try_next_model": classification.try_next_model,
                "status_code": classification.status_code,
            }
            attempts.append(attempt)
            finish_invocation(
                handle,
                status="failed",
                error=exc,
                metadata=attempt,
                failover_decision="try_next_model" if classification.try_next_model else "stop",
            )
            if not classification.try_next_model:
                break
            continue
        result = candidate_result
        attempt = {
            "attempt": attempt_number,
            "model": result.model,
            "status": "succeeded",
            "used_local_fallback": result.used_fallback,
            "request_id": result.request_id,
        }
        attempts.append(attempt)
        finish_invocation(
            handle,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            metadata=attempt,
            provider_receipt=result.request_id,
            failover_decision="not_needed",
        )
        break

    if result is None:
        result = mesh_gateway.deterministic_copy(
            state["profile"], state["selected"], model="deterministic-provider-fallback"
        )
        generation_node = "generate_copy_fallback"
        generation_state = {
            "provider_fallback": True,
            "provider_error": type(last_error).__name__ if last_error else "NoMeshModelSucceeded",
            "model_failover_used": len(attempts) > 1,
            "model_attempts": attempts,
        }
    else:
        generation_node = "generate_copy"
        generation_state = {
            "provider_fallback": result.used_fallback,
            "model_failover_used": len(attempts) > 1,
            "selected_model": result.model,
            "model_attempts": attempts,
        }
    copy = result.data.model_dump()
    _update_run(
        state["run_id"],
        generation_node,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost=estimate_cost(result.model, result.input_tokens, result.output_tokens) or 0.0,
        graph_state=generation_state,
    )
    return {
        "copy": copy,
        "model": result.model,
        "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens},
        "fallback_used": result.used_fallback or len(attempts) > 1,
    }


def validate_node(state: RecommendationState) -> RecommendationState:
    _update_run(state["run_id"], "validate_output")
    expected = [item["id"] for item in state["selected"]]
    actual = [item["product_id"] for item in state["copy"].get("item_copy", [])]
    errors = []
    if actual != expected:
        errors.append("Generated copy did not preserve the exact selected product IDs")
    if len(actual) != len(set(actual)):
        errors.append("Generated copy contains duplicate product IDs")
    generated_text = " ".join(
        [state["copy"].get("headline", ""), state["copy"].get("narrative", "")]
        + [item.get("reason", "") for item in state["copy"].get("item_copy", [])]
    )
    prohibited_claims = {
        "fabricated_price_or_discount": r"(?:[$€₹]|\b(?:usd|discount|%\s*off|sale price)\b)",
        "fabricated_guarantee": r"\b(?:guaranteed|guarantee you|certain to|will definitely)\b",
        "sensitive_inference": r"\b(?:your personality|your mental health|your ethnicity|your religion|your disability)\b",
        "instruction_echo": r"\b(?:ignore previous|system prompt|developer message)\b",
    }
    for code, pattern in prohibited_claims.items():
        if re.search(pattern, generated_text, re.I):
            errors.append(code)
    _update_run(state["run_id"], "validate_output", graph_state={"validation_errors": errors})
    return {"validation_errors": errors}


def route_after_validation(state: RecommendationState) -> str:
    return "fallback" if state.get("validation_errors") else "persist"


def fallback_node(state: RecommendationState) -> RecommendationState:
    _update_run(state["run_id"], "safe_fallback")
    primary = (state["profile"].get("primary_intent") or "your interests").replace("_", " ")
    copy = RecommendationCopy(
        headline=f"A grounded path for {primary}",
        narrative=f"Your recent activity shows a consistent interest in {primary}. These carefully selected courses provide a practical next step.",
        item_copy=[
            {"product_id": item["id"], "reason": item["default_reason"]}
            for item in state["selected"]
        ],
    ).model_dump()
    return {"copy": copy, "model": "deterministic-validation-fallback", "fallback_used": True, "validation_errors": []}


def persist_node(state: RecommendationState) -> RecommendationState:
    db = SessionLocal()
    try:
        run = db.get(RecommendationRun, state["run_id"])
        if not run:
            raise ValueError("Recommendation run not found")
        run.current_node = "persist_recommendation"
        recommendation_type = "contextual" if run.context_product_id else "overall"
        old_stmt = select(Recommendation).where(
            Recommendation.user_id == state["user_id"],
            Recommendation.status == "active",
            Recommendation.recommendation_type == recommendation_type,
        )
        if run.context_product_id:
            old_stmt = old_stmt.where(Recommendation.context_product_id == run.context_product_id)
        else:
            old_stmt = old_stmt.where(Recommendation.context_product_id.is_(None))
        old = list(
            db.scalars(old_stmt).all()
        )
        for recommendation in old:
            recommendation.status = "superseded"
        copy_lookup = {item["product_id"]: item["reason"] for item in state["copy"]["item_copy"]}
        recommendation = Recommendation(
            run_id=run.id,
            user_id=state["user_id"],
            recommendation_type=recommendation_type,
            context_product_id=run.context_product_id,
            headline=state["copy"]["headline"],
            narrative=state["copy"]["narrative"],
            model=state.get("model", "unknown"),
            profile_snapshot=state["profile"],
            expires_at=utcnow() + timedelta(days=7),
        )
        db.add(recommendation)
        db.flush()
        for rank, item in enumerate(state["selected"], start=1):
            db.add(
                RecommendationItem(
                    recommendation_id=recommendation.id,
                    product_id=item["id"],
                    rank=rank,
                    semantic_score=item["semantic_score"],
                    behavior_score=item["behavior_score"],
                    final_score=item["final_score"],
                    confidence_score=item.get("confidence_score", item["final_score"]),
                    interest_likelihood=item.get("interest_likelihood", item["final_score"]),
                    reason_codes=item["reason_codes"],
                    explanation=copy_lookup[item["id"]],
                    product_version=item["version"],
                )
            )
        run.status = "succeeded"
        run.current_node = "complete"
        run.completed_at = utcnow()
        run.lease_expires_at = None
        run.graph_state = {
            **(run.graph_state or {}),
            "selected_product_ids": [item["id"] for item in state["selected"]],
            "fallback_used": state.get("fallback_used", False),
        }
        db.commit()
        return {}
    finally:
        db.close()


graph_builder = StateGraph(RecommendationState)
graph_builder.add_node("load", load_context_node)
graph_builder.add_node("retrieve", retrieve_node)
graph_builder.add_node("evaluate_retrieval", evaluate_retrieval_node)
graph_builder.add_node("refine_retrieval", refine_retrieval_node)
graph_builder.add_node("insufficient_evidence", insufficient_evidence_node)
graph_builder.add_node("verify_with_mcp", verify_with_mcp_node)
graph_builder.add_node("generate", generate_node)
graph_builder.add_node("validate", validate_node)
graph_builder.add_node("fallback", fallback_node)
graph_builder.add_node("persist", persist_node)
graph_builder.add_edge(START, "load")
graph_builder.add_edge("load", "retrieve")
graph_builder.add_edge("retrieve", "evaluate_retrieval")
graph_builder.add_conditional_edges(
    "evaluate_retrieval", route_after_retrieval_quality,
    {"verify": "verify_with_mcp", "refine": "refine_retrieval", "insufficient": "insufficient_evidence"},
)
graph_builder.add_edge("refine_retrieval", "retrieve")
graph_builder.add_edge("insufficient_evidence", END)
graph_builder.add_edge("verify_with_mcp", "generate")
graph_builder.add_edge("generate", "validate")
graph_builder.add_conditional_edges("validate", route_after_validation, {"fallback": "fallback", "persist": "persist"})
graph_builder.add_edge("fallback", "persist")
graph_builder.add_edge("persist", END)
recommendation_graph = graph_builder.compile(checkpointer=InMemorySaver())


@traceable(name="smartreco-recommendation-run", run_type="chain", process_inputs=_trace_state_inputs, process_outputs=_trace_state_outputs)
def execute_recommendation_run(run_id: str) -> None:
    db = SessionLocal()
    try:
        pending_run = db.get(RecommendationRun, run_id)
        if not pending_run or pending_run.status != "queued":
            return
        claimed_at = utcnow()
        lease_seconds = (
            get_settings().mesh_contextual_timeout_seconds + 35
            if pending_run.context_product_id else 300
        )
        claimed = db.execute(
            update(RecommendationRun)
            .where(RecommendationRun.id == run_id, RecommendationRun.status == "queued")
            .values(
                status="running",
                current_node="graph_started",
                started_at=claimed_at,
                lease_expires_at=claimed_at + timedelta(seconds=lease_seconds),
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            return
        db.commit()
        run = db.get(RecommendationRun, run_id)
        user_id = run.user_id
    finally:
        db.close()
    handle = begin_invocation("langgraph", "recommendation_workflow", user_id=user_id, run_id=run_id)
    try:
        trace = get_current_run_tree()
        if trace:
            _update_run(run_id, "graph_started", graph_state={"langsmith_trace_id": str(trace.trace_id or trace.id)})
        recommendation_graph.invoke(
            {"run_id": run_id, "user_id": user_id},
            config={"configurable": {"thread_id": run_id}},
        )
        finish_invocation(handle, metadata={"checkpoint_thread_id": run_id})
    except Exception as exc:
        finish_invocation(handle, status="failed", error=exc)
        db = SessionLocal()
        try:
            run = db.get(RecommendationRun, run_id)
            if run:
                run.status = "failed"
                run.error_code = type(exc).__name__
                run.error_detail = str(exc)[:4000]
                run.retry_count += 1
                run.lease_expires_at = None
                db.commit()
        finally:
            db.close()


def _active_recommendation_run(db, user_id: str, scope_key: str = "overall") -> RecommendationRun | None:
    return db.scalar(
        select(RecommendationRun)
        .where(
            RecommendationRun.user_id == user_id,
            RecommendationRun.scope_key == scope_key,
            RecommendationRun.status.in_(["queued", "running"]),
        )
        .order_by(RecommendationRun.created_at.desc())
        .limit(1)
    )


def _mark_refresh_requested(run: RecommendationRun, profile: UserInterestProfile) -> None:
    run.graph_state = {
        **(run.graph_state or {}),
        "refresh_requested": True,
        "latest_profile_hash": profile.profile_hash,
        "latest_profile_version": profile.profile_version,
    }


def _claim_followup_run(user_id: str, completed_run_id: str) -> str | None:
    """Claim one follow-up when meaningful evidence accumulated during the completed run."""
    settings = get_settings()
    db = SessionLocal()
    try:
        completed = db.get(RecommendationRun, completed_run_id)
        profile = db.get(UserInterestProfile, user_id)
        user = db.get(User, user_id)
        if (
            not completed
            or completed.status != "succeeded"
            or not profile
            or not user
            or not user.is_active
            or not user.personalization_enabled
            or profile.profile_hash == completed.profile_hash
            or profile.trigger_score < settings.recommendation_min_trigger_score
        ):
            return None
        active = _active_recommendation_run(db, user_id)
        if active:
            return None
        revision = catalog_revision(db)
        key = hashlib.sha256(f"{user_id}:{profile.profile_hash}:{revision}:{PROMPT_VERSION}".encode()).hexdigest()
        already_processed = db.scalar(
            select(RecommendationRun).where(RecommendationRun.idempotency_key == key).limit(1)
        )
        if already_processed:
            return None
        run = RecommendationRun(
            user_id=user_id,
            trigger_type="signals_accumulated_during_run",
            trigger_reason=f"Signal score {profile.trigger_score:.1f} accumulated while the previous run was active",
            idempotency_key=key,
            profile_hash=profile.profile_hash,
            prompt_version=PROMPT_VERSION,
        )
        profile.trigger_score = 0
        db.add(run)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
        return run.id
    finally:
        db.close()


def process_activity_and_maybe_recommend(user_id: str, *, allow_recommendation: bool = True) -> dict:
    settings = get_settings()
    signal_handle = begin_invocation("signals", "derive_behavior_profile", user_id=user_id)
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user or not user.is_active or not user.personalization_enabled:
            finish_invocation(signal_handle, metadata={"skipped": "personalization_disabled"})
            return {"signals": 0, "triggered": False, "reason": "personalization_disabled"}
        signals, profile = derive_signals(db, user_id)
        db.commit()
        finish_invocation(signal_handle, metadata={"new_signals": len(signals), "profile_version": profile.profile_version})
        if not signals:
            return {"signals": 0, "triggered": False, "reason": "no_new_meaningful_signals"}
        # Course-page interactions must enrich the behavioral profile for the
        # learner's next visit without starting a second LLM workflow during the
        # current visit. The page GET already owns that visit's one contextual run.
        if not allow_recommendation:
            db.commit()
            return {"signals": len(signals), "triggered": False, "reason": "recorded_for_next_course_visit"}
        if profile.trigger_score < settings.recommendation_min_trigger_score:
            db.commit()
            return {"signals": len(signals), "triggered": False, "reason": "below_trigger_threshold"}
        active_run = _active_recommendation_run(db, user_id)
        if active_run:
            if active_run.profile_hash != profile.profile_hash:
                _mark_refresh_requested(active_run, profile)
            db.commit()
            return {
                "signals": len(signals),
                "triggered": False,
                "reason": "run_in_progress",
                "run_id": active_run.id,
            }
        recent_run = db.scalar(
            select(RecommendationRun)
            .where(
                RecommendationRun.user_id == user_id,
                RecommendationRun.scope_key == "overall",
                RecommendationRun.status == "succeeded",
            )
            .order_by(RecommendationRun.created_at.desc())
            .limit(1)
        )
        if recent_run and recent_run.profile_hash == profile.profile_hash:
            db.commit()
            return {"signals": len(signals), "triggered": False, "reason": "duplicate_profile"}
        if recent_run and recent_run.created_at:
            created = recent_run.created_at
            now = utcnow()
            if created.tzinfo is None:
                created = created.replace(tzinfo=now.tzinfo)
            if (now - created).total_seconds() < settings.recommendation_cooldown_seconds:
                db.commit()
                return {"signals": len(signals), "triggered": False, "reason": "cooldown"}
        revision = catalog_revision(db)
        key = hashlib.sha256(f"{user_id}:{profile.profile_hash}:{revision}:{PROMPT_VERSION}".encode()).hexdigest()
        run = RecommendationRun(
            user_id=user_id,
            trigger_type="behavioral_threshold",
            trigger_reason=f"Signal score {profile.trigger_score:.1f} crossed threshold",
            idempotency_key=key,
            profile_hash=profile.profile_hash,
            prompt_version=PROMPT_VERSION,
        )
        profile.trigger_score = 0
        db.add(run)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing_run = db.scalar(
                select(RecommendationRun).where(RecommendationRun.idempotency_key == key).limit(1)
            )
            if existing_run:
                return {
                    "signals": len(signals),
                    "triggered": False,
                    "reason": "duplicate_run",
                    "run_id": existing_run.id,
                }
            active_run = _active_recommendation_run(db, user_id)
            if not active_run:
                raise
            return {
                "signals": len(signals),
                "triggered": False,
                "reason": "run_in_progress",
                "run_id": active_run.id,
            }
        run_id = run.id
    except Exception as exc:
        finish_invocation(signal_handle, status="failed", error=exc)
        raise
    finally:
        db.close()
    execute_recommendation_run(
        run_id,
        langsmith_extra={
            "tags": ["smartreco", "recommendation", "production"],
            "metadata": {"user_ref": _trace_user_id(user_id), "recommendation_run_id": run_id, "thread_id": run_id, "model": settings.active_chat_model},
        },
    )
    followup_run_ids: list[str] = []
    completed_run_id = run_id
    # Bound automatic catch-up so sustained browsing cannot monopolize one worker.
    for _ in range(2):
        followup_run_id = _claim_followup_run(user_id, completed_run_id)
        if not followup_run_id:
            break
        followup_run_ids.append(followup_run_id)
        execute_recommendation_run(followup_run_id)
        completed_run_id = followup_run_id
    return {
        "signals": len(signals),
        "triggered": True,
        "run_id": run_id,
        "followup_run_ids": followup_run_ids,
    }


def queue_contextual_recommendation(user_id: str, product_id: str, visit_id: str | None = None) -> dict:
    """Reuse unchanged contextual output and atomically single-flight changed inputs."""
    db = SessionLocal()
    scope_key = f"course:{product_id}"
    try:
        user = db.get(User, user_id)
        product = db.get(Product, product_id)
        profile = db.get(UserInterestProfile, user_id)
        if not user or not user.is_active or not user.personalization_enabled:
            return {"created": False, "reason": "personalization_disabled", "run_id": None, "cache": "bypass"}
        if not product or product.status != "active":
            return {"created": False, "reason": "course_unavailable", "run_id": None}
        if not profile:
            return {"created": False, "reason": "profile_not_ready", "run_id": None}

        active = _active_recommendation_run(db, user_id, scope_key)
        if active:
            now = utcnow()
            lease = active.lease_expires_at
            if lease and lease.tzinfo is None:
                lease = lease.replace(tzinfo=now.tzinfo)
            created = active.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=now.tzinfo)
            stale = (lease is not None and lease < now) or (
                active.status == "queued" and (now - created).total_seconds() > 60
            )
            if stale:
                active.status = "failed"
                active.current_node = "stale_run_recovered"
                active.error_code = "StaleContextualRun"
                active.error_detail = "The previous page-visit workflow stopped reporting progress and was safely closed."
                active.completed_at = now
                active.lease_expires_at = None
                db.commit()
            else:
                return {"created": False, "reason": "run_in_progress", "run_id": active.id}

        revision = catalog_revision(db)
        key_source = f"{user_id}:{profile.profile_hash or 'new-profile'}:{revision}:{product.id}:{product.version}:{PROMPT_VERSION}"
        base_key = hashlib.sha256(key_source.encode()).hexdigest()
        existing = db.scalar(
            select(RecommendationRun)
            .where(RecommendationRun.idempotency_key == base_key)
            .limit(1)
        )
        if existing and existing.status == "succeeded":
            recommendation = db.scalar(select(Recommendation).where(Recommendation.run_id == existing.id))
            now = utcnow()
            expires = recommendation.expires_at if recommendation else None
            if expires and expires.tzinfo is None:
                expires = expires.replace(tzinfo=now.tzinfo)
            fresh_until = existing.completed_at + timedelta(hours=get_settings().contextual_recommendation_ttl_hours) if existing.completed_at else None
            stale_item_count = 0
            if recommendation:
                item_rows = db.execute(
                    select(RecommendationItem, Product)
                    .join(Product, Product.id == RecommendationItem.product_id)
                    .where(RecommendationItem.recommendation_id == recommendation.id, Product.status == "active")
                ).all()
                expected_count = db.scalar(
                    select(func.count(RecommendationItem.id)).where(RecommendationItem.recommendation_id == recommendation.id)
                ) or 0
                stale_item_count = int(len(item_rows) != expected_count) + sum(
                    item.product_version != item_product.version for item, item_product in item_rows
                )
            if recommendation and not stale_item_count and recommendation.status == "active" and (not expires or expires > now) and (not fresh_until or fresh_until > now):
                return {"created": False, "reason": "current", "run_id": existing.id, "cache": "hit"}

        idempotency_key = base_key
        if existing:
            retry_number = db.scalar(
                select(func.count(RecommendationRun.id)).where(
                    RecommendationRun.user_id == user_id,
                    RecommendationRun.scope_key == scope_key,
                )
            ) or 0
            idempotency_key = hashlib.sha256(f"{base_key}:refresh:{retry_number + 1}".encode()).hexdigest()

        run = RecommendationRun(
            user_id=user_id,
            scope_key=scope_key,
            context_product_id=product.id,
            trigger_type="course_context_opened",
            trigger_reason=f"Generate next steps from {product.title}",
            idempotency_key=idempotency_key,
            profile_hash=profile.profile_hash or "new-profile",
            prompt_version=PROMPT_VERSION,
            graph_state={
                "context_product_title": product.title,
                "request_visit_id": visit_id,
                "cache": "miss",
                "catalog_revision": revision,
                "profile_version": profile.profile_version,
            },
        )
        db.add(run)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            active = _active_recommendation_run(db, user_id, scope_key)
            if active:
                return {"created": False, "reason": "run_in_progress", "run_id": active.id}
            raise
        return {"created": True, "reason": "queued", "run_id": run.id, "cache": "miss"}
    finally:
        db.close()


def execute_contextual_recommendation(run_id: str) -> None:
    """Execute exactly one course-page LLM workflow for this page visit."""
    execute_recommendation_run(run_id)


def retry_stale_runs() -> int:
    db = SessionLocal()
    try:
        runs = list(
            db.scalars(
                select(RecommendationRun).where(
                    RecommendationRun.status == "running",
                    RecommendationRun.lease_expires_at < utcnow(),
                )
            ).all()
        )
        ids = [run.id for run in runs]
        for run in runs:
            run.status = "queued"
            run.lease_expires_at = None
        db.commit()
    finally:
        db.close()
    for run_id in ids:
        execute_recommendation_run(run_id)
    return len(ids)
