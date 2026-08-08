from __future__ import annotations

import math
from dataclasses import dataclass
from time import perf_counter

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Product
from app.services.catalog import canonical_product_text
from app.services.recommendation import build_behavioral_query, retrieve_and_rank
from app.services.mesh import deterministic_embedding, mesh_gateway
from app.services.topics import normalize_topic
from app.services.vector_store import SEMANTIC, UNAVAILABLE, get_vector_store


@dataclass(frozen=True)
class EvaluationJourney:
    name: str
    profile: dict
    relevant_categories: set[str]


def _profile(primary: str | None, secondary: list[str] | None = None, searches: list[str] | None = None) -> dict:
    secondary = secondary or []
    return {
        "primary_intent": normalize_topic(primary),
        "secondary_intents": [{"topic": normalize_topic(value), "strength": 0.5} for value in secondary],
        "category_weights": {
            normalize_topic(value): max(0.2, 1.0 - index * 0.25)
            for index, value in enumerate([primary, *secondary]) if value
        },
        "recent_searches": searches or ([primary] if primary else []),
        "positive_product_ids": [],
        "negative_product_ids": [],
        "excluded_product_ids": [],
        "journey_stage": "exploration",
    }


def build_journeys(products: list[Product]) -> list[EvaluationJourney]:
    by_category: dict[str, list[Product]] = {}
    for product in products:
        by_category.setdefault(product.category, []).append(product)
    negative_id = (by_category.get("Generative AI") or products[:1])[0].id if products else "missing"
    consumed_id = (by_category.get("Python Development") or products[:1])[0].id if products else "missing"
    journeys = [
        EvaluationJourney("Agentic AI learner", _profile("Agentic AI", ["Large Language Models"], ["agent workflows"]), {"Agentic AI", "Large Language Models"}),
        EvaluationJourney("MLOps learner", _profile("MLOps", ["Cloud & DevOps"], ["model deployment monitoring"]), {"MLOps", "Cloud & DevOps"}),
        EvaluationJourney("Java to Web learner", _profile("Web Technologies", ["Java Development"], ["spring web services"]), {"Java Development", "Web Technologies"}),
        EvaluationJourney("Python to Data learner", _profile("Data Engineering", ["Python Development"], ["python data pipelines"]), {"Python Development", "Data Engineering"}),
        EvaluationJourney("Generative AI learner", _profile("Generative AI", ["Large Language Models"], ["production RAG"]), {"Generative AI", "Large Language Models"}),
        EvaluationJourney("Mixed-interest learner", _profile("Data Engineering", ["MLOps", "Scala Development"], ["streaming ml platform"]), {"Data Engineering", "MLOps", "Scala Development"}),
        EvaluationJourney("Intent-shift learner", _profile("Web Technologies", ["Agentic AI"], ["frontend web applications", "typescript"]), {"Web Technologies"}),
        EvaluationJourney("Negative-feedback learner", _profile("Generative AI", ["Agentic AI"]), {"Generative AI", "Agentic AI"}),
        EvaluationJourney("Cold-start learner", _profile(None), set()),
        EvaluationJourney("Consumed-course exclusion learner", _profile("Python Development", ["Web Technologies"]), {"Python Development", "Web Technologies"}),
    ]
    journeys[7].profile["negative_product_ids"] = [negative_id]
    journeys[9].profile["excluded_product_ids"] = [consumed_id]
    return journeys


def _dcg(relevances: list[int]) -> float:
    return sum(value / math.log2(index + 2) for index, value in enumerate(relevances))


def _excluded(profile: dict) -> set[str]:
    return set(profile.get("negative_product_ids") or []) | set(profile.get("excluded_product_ids") or [])


def popularity_baseline(products: list[Product], profile: dict, k: int) -> tuple[list[dict], dict]:
    """Evaluation-only non-personalized baseline with the same eligibility exclusions."""
    eligible = [product for product in products if product.id not in _excluded(profile)]
    eligible.sort(key=lambda product: (float(product.popularity or 0), float(product.rating or 0), product.id), reverse=True)
    return [
        {"id": product.id, "category": product.category, "final_score": float(product.popularity or 0)}
        for product in eligible[:k]
    ], {"semantic_status": "NOT_APPLICABLE"}


def semantic_only_baseline(
    products: list[Product], profile: dict, k: int, *, semantic: bool
) -> tuple[list[dict], dict]:
    """Evaluation-only vector-similarity baseline; no behavioral reranking."""
    query = build_behavioral_query(profile)
    excluded = _excluded(profile)
    if semantic:
        result = get_vector_store().search_with_status(query, limit=len(products))
        scores = {item["product_id"]: float(item["semantic_score"]) for item in result.items}
        status = result.status
        provider = result.provider
    else:
        query_vector = deterministic_embedding(query)
        scores = {
            product.id: sum(
                left * right
                for left, right in zip(query_vector, deterministic_embedding(canonical_product_text(product)), strict=True)
            )
            for product in products
        }
        status = "DEGRADED"
        provider = "deterministic-hash-v1"
    ranked = sorted(
        (product for product in products if product.id not in excluded),
        key=lambda product: (scores.get(product.id, -1.0), product.id),
        reverse=True,
    )
    return [
        {"id": product.id, "category": product.category, "final_score": round(scores.get(product.id, 0.0), 6)}
        for product in ranked[:k]
    ], {"semantic_status": status, "semantic_provider": provider}


def _evaluate_system(
    name: str,
    journeys: list[EvaluationJourney],
    products: list[Product],
    category_totals: dict[str, int],
    active_ids: set[str],
    k: int,
    recommender,
) -> dict:
    rows: list[dict] = []
    all_recommended: set[str] = set()
    recommendation_sets: list[set[str]] = []
    counters_before = mesh_gateway.counter_snapshot()
    for journey in journeys:
        journey_before = mesh_gateway.counter_snapshot()
        started = perf_counter()
        ranked, retrieval = recommender(journey.profile, k)
        latency_ms = round((perf_counter() - started) * 1000, 2)
        ids = [item["id"] for item in ranked]
        categories = [item["category"] for item in ranked]
        relevant = [int(category in journey.relevant_categories) for category in categories]
        expected_count = sum(category_totals.get(category, 0) for category in journey.relevant_categories)
        precision = sum(relevant) / max(1, len(ranked)) if journey.relevant_categories else None
        recall = sum(relevant) / max(1, expected_count) if journey.relevant_categories else None
        ideal = [1] * min(k, expected_count)
        ndcg = _dcg(relevant) / _dcg(ideal) if ideal else None
        exclusions = _excluded(journey.profile)
        journey_after = mesh_gateway.counter_snapshot()
        row = {
            "journey": journey.name,
            "recommended_product_ids": ids,
            "recommended_categories": categories,
            "precision_at_k": round(precision, 4) if precision is not None else None,
            "recall_at_k": round(recall, 4) if recall is not None else None,
            "ndcg_at_3": round(ndcg, 4) if ndcg is not None else None,
            "diversity": round(len(set(categories)) / max(1, len(categories)), 4),
            "exclusion_pass": not bool(set(ids) & exclusions),
            "hallucinated_product_ids": sorted(set(ids) - active_ids),
            "semantic_status": retrieval.get("semantic_status"),
            "latency_ms": latency_ms,
            "mesh_embedding_calls": journey_after["mesh_embedding_calls"] - journey_before["mesh_embedding_calls"],
            "recommendation_copy_llm_calls": journey_after["mesh_copy_llm_calls"] - journey_before["mesh_copy_llm_calls"],
        }
        rows.append(row)
        all_recommended.update(ids)
        recommendation_sets.append(set(ids))
    labelled = [row for row in rows if row["precision_at_k"] is not None]
    ndcg_labelled = [row for row in rows if row["ndcg_at_3"] is not None]
    pairwise = [
        1 - len(left & right) / max(1, len(left | right))
        for index, left in enumerate(recommendation_sets)
        for right in recommendation_sets[index + 1:]
    ]
    counters_after = mesh_gateway.counter_snapshot()
    statuses = sorted({row["semantic_status"] for row in rows})
    summary = {
        "system": name,
        "journey_count": len(rows),
        "mean_precision_at_k": round(sum(row["precision_at_k"] for row in labelled) / max(1, len(labelled)), 4),
        "mean_recall_at_k": round(sum(row["recall_at_k"] for row in labelled) / max(1, len(labelled)), 4),
        "mean_ndcg_at_3": round(sum(row["ndcg_at_3"] for row in ndcg_labelled) / max(1, len(ndcg_labelled)), 4),
        "mean_diversity": round(sum(row["diversity"] for row in rows) / len(rows), 4),
        "catalog_coverage": round(len(all_recommended) / len(active_ids), 4),
        "personalization_separation": round(sum(pairwise) / max(1, len(pairwise)), 4),
        "exclusion_pass_rate": round(sum(row["exclusion_pass"] for row in rows) / len(rows), 4),
        "hallucinated_product_id_rate": round(sum(len(row["hallucinated_product_ids"]) for row in rows) / max(1, k * len(rows)), 4),
        "semantic_status": statuses[0] if len(statuses) == 1 else statuses,
        "mesh_embedding_calls": counters_after["mesh_embedding_calls"] - counters_before["mesh_embedding_calls"],
        "recommendation_copy_llm_calls": counters_after["mesh_copy_llm_calls"] - counters_before["mesh_copy_llm_calls"],
        "mean_latency_ms": round(sum(row["latency_ms"] for row in rows) / len(rows), 2),
    }
    return {"journeys": rows, "summary": summary}


def _percent_change(current: float, baseline: float) -> float | None:
    return round((current - baseline) / baseline * 100, 2) if baseline else None


def evaluate_recommendations(k: int = 3, *, semantic: bool = False) -> dict:
    db = SessionLocal()
    try:
        products = list(db.scalars(select(Product).where(Product.status == "active")).all())
    finally:
        db.close()
    if not products:
        return {"status": "no_catalog", "journeys": [], "summary": {}}
    if semantic:
        if not mesh_gateway.embeddings_enabled:
            return {
                "status": "semantic_unavailable",
                "semantic_status": UNAVAILABLE,
                "reason": "Mesh embeddings are not enabled; the offline evaluator remains available.",
                "mesh_generation_invoked": False,
                "summary": {"mesh_embedding_calls": 0, "recommendation_copy_llm_calls": 0},
                "journeys": [],
            }
        verification = get_vector_store().verify_index()
        if verification.status != SEMANTIC or not verification.compatible:
            return {
                "status": "semantic_unavailable",
                "semantic_status": verification.status,
                "reason": verification.message,
                "error_code": verification.error_code,
                "mesh_generation_invoked": False,
                "summary": {"mesh_embedding_calls": 0, "recommendation_copy_llm_calls": 0},
                "journeys": [],
            }
    active_ids = {product.id for product in products}
    category_totals: dict[str, int] = {}
    for product in products:
        category_totals[product.category] = category_totals.get(product.category, 0) + 1

    journeys = build_journeys(products)
    systems = {
        "popularity": _evaluate_system(
            "Popularity", journeys, products, category_totals, active_ids, k,
            lambda profile, limit: popularity_baseline(products, profile, limit),
        ),
        "semantic_only": _evaluate_system(
            "Semantic-only", journeys, products, category_totals, active_ids, k,
            lambda profile, limit: semantic_only_baseline(products, profile, limit, semantic=semantic),
        ),
        "smartreco": _evaluate_system(
            "SmartReco hybrid", journeys, products, category_totals, active_ids, k,
            lambda profile, limit: retrieve_and_rank(profile, limit=limit),
        ),
    }
    summary = systems["smartreco"]["summary"]
    summary.update({
        "fallback_rate": round(
            sum(row["semantic_status"] != SEMANTIC for row in systems["smartreco"]["journeys"]) / len(journeys), 4
        ),
        "mesh_calls": summary["mesh_embedding_calls"],
        "mesh_generation_invoked": False,
        "estimated_ai_cost": None if semantic else 0.0,
    })
    comparison = [systems[key]["summary"] for key in ("popularity", "semantic_only", "smartreco")]
    improvement = {
        "ndcg_vs_popularity_percent": _percent_change(
            summary["mean_ndcg_at_3"], systems["popularity"]["summary"]["mean_ndcg_at_3"]
        ),
        "ndcg_vs_semantic_only_percent": _percent_change(
            summary["mean_ndcg_at_3"], systems["semantic_only"]["summary"]["mean_ndcg_at_3"]
        ),
    }
    return {
        "status": "ok",
        "evaluation_mode": "live_semantic" if semantic else "deterministic_offline",
        "semantic_status": summary["semantic_status"],
        "mesh_generation_invoked": False,
        "metric_definition": "A result is relevant when its authoritative catalog category is in the journey's predeclared relevant-category set.",
        "journeys": systems["smartreco"]["journeys"],
        "summary": summary,
        "systems": systems,
        "comparison": comparison,
        "improvement": improvement,
    }
