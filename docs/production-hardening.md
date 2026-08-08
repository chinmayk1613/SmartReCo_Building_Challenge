# SmartReco production-hardening design

This document records the boundaries introduced by the incremental hardening program. The FastAPI, SQLAlchemy, Qdrant, Mesh, LangGraph, LangSmith, APScheduler, MCP, and Jinja/JavaScript architecture remains unchanged.

## Semantic retrieval contract

SQL remains business truth and Qdrant remains a rebuildable candidate index. Catalog and query embeddings use the configured embedding endpoint through Mesh in production. Every retrieval reports one state:

- `SEMANTIC`: Mesh embedding and Qdrant query succeeded.
- `DEGRADED`: the deterministic local hash fallback was used in development/tests.
- `UNAVAILABLE`: the provider or Qdrant could not serve retrieval.

Vector IDs are never trusted. Active SQL rows verify each ID; unknown, archived, and excluded products are discarded. Production startup fails when Mesh embeddings are disabled, preventing a hash fallback from being presented as semantic RAG.

Every synchronized SQL state and Qdrant payload now records embedding provider, model, vector dimension, index schema version, product version, and content checksum. Runtime retrieval verifies the complete active index before embedding the query. A legacy/hash/mixed/missing/stale index returns `UNAVAILABLE` with `VECTOR_INDEX_REBUILD_REQUIRED`; it cannot silently claim `SEMANTIC`. `scripts/rebuild_vector_index.py` generates all Mesh vectors before changing Qdrant, rebuilds a dimension-incompatible collection only after generation succeeds, removes stale points, preserves SQL products, and verifies exact active SQL/Qdrant counts.

## Contextual idempotency and freshness

The course-page cache key contains learner profile hash, full catalog revision, current product ID/version, and prompt version. Stored output is reusable only while active, unexpired, within the contextual TTL, and backed by active products whose versions equal each `RecommendationItem.product_version`. The browser visit ID is diagnostic only and cannot force an AI call. A unique database idempotency key plus the active-scope unique index provides single-flight behavior.

The full catalog revision is a hash of every product ID, version, and status, so an archive or edit is visible even when it does not change `max(Product.version)`.

## LangGraph decision path

```text
load context
  → retrieve and deterministic rank
  → evaluate retrieval quality
      → sufficient: MCP/SQL verify
      → insufficient: refine query once → retrieve once more
      → still insufficient: persist run state "not enough evidence" and stop
  → Mesh grounded generation
  → output validation
      → valid: persist
      → invalid/provider failure: deterministic grounded fallback → persist
```

The quality decision uses candidate count, best deterministic fit, semantic runtime status, and presence of behavioral evidence. It does not call an LLM. Observability records retrieval attempt, quality, semantic status, refinement reason, and final candidate count.

## Personalization controls, digest opt-in, and retention

Personalization is enabled by default for new learners and is governed by a clear account opt-out rather than an explicit opt-in. That opt-out is authoritative at ingestion and orchestration boundaries: it stops behavior acceptance, signal/profile creation, contextual and overall graph runs, behavior export to Mesh, digest scheduling, and dispatch. Digest/email delivery is a separate setting that is disabled by default and requires explicit opt-in. Opting out does not silently destroy existing history; learners can permanently delete activity, signals, recommendations, workflow telemetry, and queued deliveries through the personalization-history deletion control.

The UTC retention job runs daily and enforces:

| Data | Default |
|---|---:|
| Activity events | 180 days |
| Behavioral signals | 30 days / `expires_at` |
| Inactive recommendations | 90 days after status/age criteria |
| Expired sessions | immediate on retention run |
| Authentication attempts | 30 days |

Active recommendations become `expired` when their `expires_at` is reached. The decayed aggregate interest profile remains so deleting old raw evidence does not reset the learner silently.

## Concurrency and recovery

Catalog outbox and delivery dispatch use conditional database updates to claim only eligible rows. Expired catalog leases are reclaimable. An older outbox event is marked superseded if the SQL product has a newer version, ensuring the final SQL version wins. Delivery dispatch re-checks the current personalization preference, explicit digest opt-in, and recommendation eligibility after claim and before provider contact. Existing run and delivery recovery remains bounded and idempotent.

## Privacy and AI grounding

Hosted LangSmith traces receive allow-listed state summaries, not raw graph state. Learner identifiers are pseudonymized with an application-secret keyed hash. Credential-like keys and values are redacted from local/export metadata and errors. Local SQL telemetry retains the real relational user ID for authorized admin filtering and reconciliation.

If connectivity returns after a trace export gap, reconciliation replays missing correlation-enabled attempts from the durable local invocation ledger. Replayed runs retain their original timestamps, status, error code and token counts, use the original idempotent LangSmith run ID, and are explicitly tagged `historical-backfill`; a failed provider attempt is never rewritten as a success.

Search strings, catalog descriptions, and behavioral fields are explicitly untrusted data. They are bounded, obvious email addresses and phone numbers are redacted before Mesh prompts/embeddings, and the resulting values are serialized inside a structured prompt with a system rule that forbids following embedded instructions. Validation requires the exact ordered product IDs and rejects fabricated price/discount/guarantee claims, sensitive-trait inference, and instruction echoes.

The MCP server is intentionally trusted-local stdio. Catalog verification is reusable and read-only; identity-bearing behavior tools fail closed if that local trust setting is disabled. Network multi-tenant MCP requires principal authentication and is outside the current submission boundary.

## Offline evaluation

`python scripts/evaluate_recommendations.py` evaluates Popularity, Semantic-only, and the unchanged SmartReco hybrid ranker across ten declared synthetic journeys without Mesh generation. Relevance is predeclared by authoritative category sets. It reports Precision@3, Recall@3, NDCG@3, diversity, coverage, personalization separation, negative/consumed exclusions, hallucinated-ID rate, semantic status, ranking latency, embedding calls, and copy-LLM calls. `--semantic` is an explicit opt-in and fails closed unless a provenance-verified Mesh/Qdrant index is active. `--json` produces machine-readable evidence.

`scripts/benchmark_ai_efficiency.py` uses an isolated temporary database/Qdrant store to replay 100 browser events and measure batching, acceptance, duplicates, signals, trigger evaluations, workflows, cache hits, embedding calls, copy calls, and internal/external latency. `--live-mesh` is required before it calculates an LLM-call-reduction percentage. `scripts/evaluate_intent_evolution.py` similarly isolates a Python→MLOps→negative-feedback journey while exercising the production 72-hour decay and ranker.
