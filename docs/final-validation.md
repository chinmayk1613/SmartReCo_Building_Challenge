# SmartReco final validation

Validated on 8 August 2026. The existing FastAPI, SQLAlchemy, Jinja, Qdrant, Mesh, LangGraph, LangSmith, and APScheduler architecture remains the application path.

## Verification summary

| Check | Result |
|---|---|
| Baseline suite before hardening | 164 passed |
| Final suite with CI-compatible coverage | 184 passed, 0 failed, 82% total coverage |
| Python compilation | Passed |
| Submission/secret hygiene audit | Passed |
| Active SQL products | 50 |
| Verified Qdrant products | 50 |
| Vector provenance compatibility | Passed |
| Current vector status | `SEMANTIC` — verified Mesh `openai/text-embedding-3-small` vectors |
| Live semantic evaluation | Completed successfully on 2026-08-08 |

The active index verifies 50 active SQL products against 50 Qdrant vectors with no missing, stale, or incompatible IDs. Provenance is `mesh_api/openai/text-embedding-3-small`, dimension `1536`, schema `smartreco-product-v1`.

## Recommender comparison

All systems used the same 50-product catalog, ten journeys, predeclared labels and exclusions, and `K=3`.

| System | Precision@3 | Recall@3 | NDCG@3 | Diversity | Coverage | Separation |
|---|---:|---:|---:|---:|---:|---:|
| Popularity | 0.1481 | 0.0424 | 0.1633 | 0.9667 | 0.0800 | 0.1000 |
| Semantic-only, real Mesh semantic | 0.8148 | 0.2798 | 0.8038 | 0.6667 | 0.4200 | 0.9467 |
| SmartReco hybrid, real Mesh semantic | 0.9630 | 0.3307 | 0.9739 | 0.6667 | 0.4200 | 0.9444 |

SmartReco improved NDCG@3 by 496.39% over Popularity and 21.16% over real Semantic-only retrieval. Exclusion correctness and catalog-ID grounding were 100%; no hallucinated product ID was observed. Semantic-only and SmartReco each made ten Mesh embedding calls and zero recommendation-copy LLM calls in this retrieval/ranking evaluation.

## Measured AI efficiency

The opt-in live-Mesh benchmark recorded:

```text
100 browser events -> 10 HTTP batches -> 96 accepted + 2 rejected + 2 duplicates
                   -> 90 signal updates -> 10 trigger evaluations
                   -> 2 recommendation workflows -> 2 Mesh copy calls
                   -> 1 contextual recommendation cache hit
```

Against a naive one-call-per-event design, this is a measured 98.0% reduction in recommendation-copy LLM calls. Internal ranking averaged approximately 14.47 ms; external Mesh latency was reported separately at approximately 8.6 seconds mean for that run.

## Commands before submission

```powershell
python -m alembic upgrade head
python scripts/rebuild_vector_index.py --verify-only
python -m pytest --cov=app --cov-report=term-missing -p no:cacheprovider
python scripts/evaluate_recommendations.py
python scripts/evaluate_intent_evolution.py --json
python scripts/check_submission_hygiene.py
```

Reproduce the verified semantic result with Mesh embeddings enabled:

```powershell
python scripts/rebuild_vector_index.py
python scripts/rebuild_vector_index.py --verify-only
python scripts/evaluate_recommendations.py --semantic --json
```

The semantic evaluator fails closed unless Mesh embeddings are enabled and the complete Qdrant index verifies as `SEMANTIC`.
