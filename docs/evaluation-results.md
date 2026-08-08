# SmartReco evaluation evidence 

Updated on 2026-08-08. Every number below came from an unchanged reproducible evaluator. The current real Mesh-semantic result is kept separate from the earlier deterministic/hash result.

## Current real Mesh-semantic comparison

Command:

```powershell
python scripts/evaluate_recommendations.py --semantic --json
```

The evaluator verified `SEMANTIC` retrieval using Qdrant vectors produced by `mesh_api/openai/text-embedding-3-small`. All three systems used the same 50-course catalog, ten synthetic journeys, predeclared category relevance labels, exclusions, and `K=3`. No ranking weight or relevance label was changed for this run.

| System | Status | Precision@3 | Recall@3 | NDCG@3 | Diversity | Coverage | Separation | Exclusions | Hallucinated IDs | Mesh embedding calls | Recommendation LLM calls |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Popularity | `NOT_APPLICABLE` | 0.1481 | 0.0424 | 0.1633 | 0.9667 | 0.0800 | 0.1000 | 1.0000 | 0.0000 | 0 | 0 |
| Semantic-only | `SEMANTIC` | 0.8148 | 0.2798 | 0.8038 | 0.6667 | 0.4200 | 0.9467 | 1.0000 | 0.0000 | 10 | 0 |
| SmartReco behavioral hybrid | `SEMANTIC` | 0.9630 | 0.3307 | 0.9739 | 0.6667 | 0.4200 | 0.9444 | 1.0000 | 0.0000 | 10 | 0 |

Measured NDCG@3 change:

- SmartReco versus Popularity: `+496.39%`
- SmartReco versus real Semantic-only: `+21.16%`

The complete comparison made 20 real Mesh embedding calls: ten for Semantic-only and ten for SmartReco. It made zero recommendation-copy LLM calls because this evaluator measures retrieval/ranking rather than persuasive narrative generation. Exclusion correctness was 100% and no hallucinated product ID was observed. SmartReco won precision, recall, and NDCG; real Semantic-only had slightly higher personalization separation (`0.9467` versus `0.9444`), while diversity and coverage were equal.

Recall@3 uses all active products in each journey's declared relevant categories as the denominator. Because only three positions are available, recall is naturally lower than precision.

## Historical deterministic/hash comparison — not semantic

Command:

```powershell
python scripts/evaluate_recommendations.py
```

These earlier values used deterministic hash vectors and remain explicitly `DEGRADED`. They are retained only for reproducible CI/offline comparison and must not be presented as real semantic evidence.

| System | Precision@3 | Recall@3 | NDCG@3 | Diversity | Coverage | Separation | Exclusions | Hallucinated IDs |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Popularity | 0.1481 | 0.0424 | 0.1633 | 0.9667 | 0.0800 | 0.1000 | 1.0000 | 0.0000 |
| Semantic-only, degraded hash | 0.7778 | 0.2858 | 0.7778 | 0.7667 | 0.4200 | 0.9533 | 1.0000 | 0.0000 |
| SmartReco hybrid, degraded hash | 0.9630 | 0.3307 | 0.9739 | 0.7000 | 0.4600 | 0.9689 | 1.0000 | 0.0000 |

The historical degraded/hash NDCG comparison was `+25.21%` versus Semantic-only. It is not the current live-semantic result.

## AI-call efficiency

Command:

```powershell
python scripts/benchmark_ai_efficiency.py --live-mesh
```

| Counter | Measured |
|---|---:|
| Raw browser events | 100 |
| HTTP batches | 10 |
| Accepted events | 96 |
| Rejected events | 2 |
| Duplicate events | 2 |
| Behavioral signal updates | 90 |
| Trigger evaluations | 10 |
| Recommendation generation runs | 2 |
| Contextual cache hits | 1 |
| Mesh embedding calls | 0 |
| Mesh recommendation-copy calls | 2 |
| Naive per-event LLM calls | 100 |
| Measured LLM-call reduction | **98.0%** |

The benchmark used deterministic local embeddings so it measured copy-call gating without spending embedding calls. It made two real copy requests through the configured Mesh gateway. Offline mode reports the reduction as `not_run_live` rather than treating disabled AI as a 100% saving.

Informational timings from that live run:

- Batched ingestion median: approximately `40.15 ms`.
- Duplicate-containing batch: approximately `33.76 ms`.
- Contextual cache lookup: approximately `8.70 ms`.
- Hybrid retrieval/ranking mean: approximately `14.47 ms`.
- External Mesh copy latency mean: approximately `8,626 ms`.

The ingestion p95 is intentionally not used as an internal latency claim because FastAPI TestClient waits for background work; external Mesh latency is disclosed separately.

## Closed-loop intent evolution

Command:

```powershell
python scripts/evaluate_intent_evolution.py --json
```

The evaluation uses the production event, signal, 72-hour half-life, profile, ranker, and negative-feedback code:

1. Repeated Python activity produced primary intent `python_ai` and Python-led recommendations.
2. Four simulated days later, repeated MLOps search/view/dwell evidence changed primary intent to `mlops` and moved MLOps to rank 1.
3. “Not for me” added that exact product to the negative set; it was absent from the next top three and the ranking changed again.

All assertions passed: primary intent changed, ranking changed after the shift, dismissed-product exclusion passed, and ranking changed after feedback. Retrieval was explicitly `DEGRADED` in this isolated no-Mesh run.

## Live semantic verification

The real semantic evaluation is now recorded above. The active index verified 50 active SQL products against 50 Qdrant vectors with no missing, stale, or incompatible IDs and provenance `mesh_api/openai/text-embedding-3-small`, dimension `1536`, schema `smartreco-product-v1`.

```powershell
python -m alembic upgrade head
python scripts/rebuild_vector_index.py
python scripts/rebuild_vector_index.py --verify-only
python scripts/evaluate_recommendations.py --semantic --json
```

The evaluator still exits without metrics unless Mesh embeddings are enabled and Qdrant provenance verifies as `SEMANTIC`; it never substitutes deterministic/hash numbers for this section.
