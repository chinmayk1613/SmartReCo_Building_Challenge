# SmartReco

SmartReco is an end-to-end behavioral AI recommendation platform. It captures meaningful storefront activity without blocking the browser, derives inspectable behavioral signals, maintains a decayed user-interest profile, retrieves verified catalog products with RAG, and uses LangGraph to generate and persist grounded recommendations(Youtube Explanation Video - https://youtu.be/eejNA2ih5TU).

## Implemented architecture

- Fixed-domain email/password registration (`REGISTRATION_EMAIL_DOMAIN`) with Argon2 hashing, opaque server-side sessions, CSRF protection, and user/admin RBAC. Registration creates the learner's initial personalization profile in the same transaction.
- Jinja2 storefront and an operational admin console.
- Batched, retryable event tracking with active dwell measurement and impression deduplication.
- Separate raw events, derived signals, user profiles, trigger gating, and evidence views.
- SQL catalog as source of truth plus transactional outbox and idempotent Qdrant synchronization.
- Mesh API for grounded recommendation copy, using a configurable free-first model chain.
- `tencent/hy3` free model, `openai/gpt-4o-mini` paid comparator, and `openai/gpt-5.4-mini` premium comparator.
- A visibly labeled deterministic non-AI fallback when no Mesh key is configured.
- RAG retrieval, deterministic ranking, diversity, exclusions, and narrative/product validation.
- A last-ten interaction feed and up to three live next-course recommendations on every course page.
- Context-first course-detail retrieval: the open course is a hard relevance gate and accumulated behavior changes ranking within defensible learning paths; unchanged refreshes reuse stored output.
- A 50-course catalog distributed across 11 categories, with all records synchronized through the SQL-to-vector dual-write outbox.
- A learner cart whose course-review events strengthen related interests while carted and purchased courses remain ineligible for recommendation.
- A home-page top-interest summary derived from the learner's complete, recency-decayed history; actual course recommendations appear only once.
- LangGraph nodes for context, retrieval, deterministic retrieval-quality evaluation, one bounded query refinement, SQL/MCP verification, generation, validation, fallback, and persistence, with durable SQL run/lease state.
- Provider connection failures remain visible as failed LLM invocations but continue through grounded deterministic copy, preventing a temporary Mesh outage from failing the complete recommendation lifecycle.
- Durable, user-filterable LLM/RAG/MCP/LangGraph telemetry with a one-second live admin refresh (including background tabs), plus authenticated LangSmith trace export controlled by environment variables.
- Read-only MCP tools for profiles, ranked catalog search, and authoritative product details, restricted to trusted local stdio for identity-bearing tools.
- APScheduler jobs for catalog synchronization, digest scheduling, durable delivery/retries, and stale-work recovery.
- Default-on personalization with clear opt-out and history-deletion controls, explicit digest/email opt-in, closed-loop “Not for me” feedback, and an admin delivery ledger with attempt status and provider receipts.
- Login throttling, strict host validation, production configuration guards, and browser security headers.

## Data flow

```text
Browser events
  -> batched ingestion
  -> raw activity_events
  -> deterministic signal derivation
  -> user_interest_profiles
  -> threshold/cooldown gate
  -> LangGraph recommendation run
  -> Mesh embedding + Qdrant semantic retrieval (production)
     or explicitly DEGRADED local hash fallback (development/tests)
  -> SQL verification + deterministic ranking
  -> Mesh narrative
  -> grounding validation
  -> stored recommendation
  -> opted-in durable digest delivery (sandbox or SMTP)
  -> impressions/clicks become new feedback
```

The SQL database owns business truth. Qdrant is a rebuildable retrieval index. The LLM never creates arbitrary product IDs and never writes directly to the database.

## Core challenge coverage

The core submission includes authenticated user/admin roles, admin catalog CRUD, transactional SQL/vector dual-write, non-blocking behavioral tracking, RAG candidate retrieval, behavioral/hybrid reranking, persuasive Mesh generation, strict catalog grounding, stored recommendations, feedback, and meaningful trigger/caching controls. The existing browser, FastAPI, SQLAlchemy, Qdrant, LangGraph, Mesh, Jinja, and APScheduler flow remains the production path.

Standout evidence is additive: a bounded LangGraph retrieval-quality gate, LangSmith reconciliation, local-time digest delivery, personalization opt-out, explicit digest opt-in and retention controls, stale-run recovery, transactional vector outbox, semantic-index provenance, closed-loop feedback evaluation, three-system ranking benchmarks, concurrency tests, and measured AI-call efficiency.

## Why SmartReco is not just semantic search

Semantic similarity answers: **Which products resemble this query?** SmartReco additionally combines repeated behavior, search intent, active dwell time, browsing path, current-course context, explicit negative feedback, cart/purchase exclusions, time-decayed profile evolution, catalog freshness, and deterministic diversity. Qdrant proposes candidates; SQL eligibility and the behavioral ranker decide what may be recommended; Mesh explains the already-selected records.

The current live-semantic comparison uses the same 50-course catalog, ten predeclared journeys, relevance labels, exclusions, and `K=3` for every system. Qdrant provenance verified real Mesh `openai/text-embedding-3-small` vectors:

| System | Precision@3 | Recall@3 | NDCG@3 | Diversity | Coverage | Separation |
|---|---:|---:|---:|---:|---:|---:|
| Popularity | 0.1481 | 0.0424 | 0.1633 | 0.9667 | 0.0800 | 0.1000 |
| Semantic-only (`SEMANTIC`) | 0.8148 | 0.2798 | 0.8038 | 0.6667 | 0.4200 | 0.9467 |
| SmartReco behavioral/hybrid (`SEMANTIC`) | 0.9630 | 0.3307 | 0.9739 | 0.6667 | 0.4200 | 0.9444 |

In this measured real-semantic run, SmartReco improved NDCG@3 by **21.16%** over Semantic-only and **496.39%** over Popularity. Exclusion correctness and catalog-ID grounding were 100%; no hallucinated product ID was observed. Semantic-only and SmartReco each made ten Mesh embedding calls and zero recommendation-copy LLM calls. The older deterministic/hash results remain separately labeled `DEGRADED` in [docs/evaluation-results.md](docs/evaluation-results.md).

## AI efficiency

The isolated live-Mesh benchmark measured the existing production gates rather than simulating a favorable call count:

```text
100 browser events -> 10 HTTP batches -> 96 accepted + 2 rejected + 2 duplicates
                   -> 90 signal updates -> 10 trigger evaluations
                   -> 2 recommendation workflows -> 2 real Mesh copy calls
                   -> 1 confirmed contextual cache hit
```

The naive comparison is one LLM call per raw event. SmartReco made 2 measured Mesh recommendation-copy calls, a **98.0% call reduction**. Median batched-ingestion time was approximately 40 ms in that run; hybrid retrieval/ranking averaged approximately 14.5 ms. External Mesh latency was reported separately (approximately 8.6 seconds mean) and is not disguised as internal application latency. Timings are informational and machine/provider dependent.

## Recommendation safety

- Exact ordered product IDs must survive Mesh generation and SQL/MCP verification.
- Unknown, archived, edited, expired, consumed, carted, or explicitly dismissed products are excluded according to scope.
- Qdrant points carry embedding provider/model/dimension/schema provenance; incompatible indexes are blocked with `VECTOR_INDEX_REBUILD_REQUIRED`.
- Retrieval refinement is bounded to one retry; insufficient evidence produces zero persuasive LLM calls.
- Search/catalog values are untrusted data, bounded, and stripped of obvious email/phone PII before external AI calls.
- Personalization is enabled by default with a clear account opt-out. Opting out stops tracking acceptance, personalization workflows, external AI use, and delivery; digest/email delivery separately requires explicit opt-in. Learners can delete their personalization history from account settings.

The exact behavioral weights, dwell semantics, ranking formula, trigger gate, RAG boundary, and LLM boundary are documented in [docs/behavior-ranking.md](docs/behavior-ranking.md).
The semantic-status, caching, privacy, concurrency, freshness, and evaluation contracts are documented in [docs/production-hardening.md](docs/production-hardening.md).
The latest reproducible local evaluation is recorded in [docs/evaluation-results.md](docs/evaluation-results.md).

## Local setup

```powershell
Copy-Item .env.example .env
# Set APP_SECRET, DEMO_ADMIN_PASSWORD and optionally MESH_API_KEY in .env.
python -m pip install -r requirements.txt
python -m alembic upgrade head
python scripts/seed_demo.py --admin-password "choose-a-strong-password"
python -m uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>.

Set `REGISTRATION_EMAIL_DOMAIN=smartreco.ai` (or the hospital/company domain for a deployment). The registration form accepts only the part before `@`; the server constructs and validates the complete address, so a browser cannot select a different domain.

The seed script creates:

- `admin@smartreco.local` with the password passed to `--admin-password`.
- `learner@smartreco.local` with `DemoUser123!` unless changed using `--user-password`.

Replay a deterministic behavioral journey:

```powershell
python scripts/replay_journey.py
```

Then inspect `/admin/activity`, `/admin/runs`, `/admin/observability`, and the learner storefront.

Digest delivery defaults to a local sandbox: it records a realistic provider receipt without contacting an external service. Opt in from `/account`, then inspect or manually run the dispatcher from `/admin/deliveries`. Set `DELIVERY_MODE=smtp` and the SMTP variables only when using an approved provider sandbox.

### SMTP digest setup

For an approved SMTP sandbox such as Mailtrap, copy the provider credentials into the local `.env` file (never commit them), set the public application address used by course links, and restart SmartReco:

```env
DELIVERY_MODE=smtp
SMTP_HOST=sandbox.smtp.mailtrap.io
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=SmartReco <recommendations@smartreco.local>
APP_PUBLIC_URL=http://127.0.0.1:8000
```

The SMTP client requires verified STARTTLS before authentication or sending; certificate or hostname verification failures follow the normal delivery retry/failure path and never fall back to plaintext. The resulting multipart digest includes the personalized recommendation narrative, a tailored explanation and direct link for every selected course, plus an accessible plain-text version. Learners must enable digest delivery and provide a digest address in `/account`; the dispatcher rechecks consent, address, catalog activity, expiry and product version immediately before provider contact.

The full setup, delivery lifecycle and architecture diagram are included in [the technical handover handbook](docs/SmartReco_End_to_End_Technical_Handover_Handbook.pdf).

## Mesh API

```env
MESH_API_KEY=rsk_...
MESH_BASE_URL=https://api.meshapi.ai/v1
MESH_MODEL_MODE=free
MESH_FREE_MODEL=minimax/m2-her
MESH_PAID_MODEL=openai/gpt-4o-mini
MESH_PREMIUM_MODEL=openai/gpt-5.4-mini
MESH_EMBEDDING_MODEL=openai/text-embedding-3-small
MESH_EMBEDDINGS_ENABLED=false
```

`MESH_EMBEDDINGS_ENABLED=false` is an explicit `DEGRADED` development/test mode using deterministic hashes. Production startup requires Mesh credentials and `MESH_EMBEDDINGS_ENABLED=true`, so local similarity is never presented as successful semantic RAG.

The model lab sends the same profile and fixed selected products to whichever models an admin explicitly selects. It defaults to the free model, reports partial provider failures independently, and never triggers a paid tier implicitly. Exact product-ID grounding is a hard gate before qualitative comparison.

## LangSmith

```env
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=smartreco
```

Trace inputs/outputs are allow-listed, learner IDs are pseudonymized for hosted export, and credentials/session/CSRF values plus secret-like error content are redacted. Local SQL telemetry retains the real user relation for authorized admin filtering.

The application records local observability even when LangSmith is disconnected. Live LangSmith export requires a separate LangSmith API key; the Mesh key is not interchangeable with it.

## MCP

```powershell
python -m app.mcp_server
```

Read-only tools (the LLM can retrieve context but cannot mutate business records):

- `get_behavior_profile`
- `search_product_catalog`
- `get_verified_product_details`
- `get_recent_behavioral_signals`
- `get_personalized_course_candidates`

Identity-bearing tools fail closed unless `MCP_TRUSTED_LOCAL_ONLY=true`. The current server is intentionally a local stdio integration; it is not a multi-tenant network MCP endpoint.

## Production stores

PostgreSQL:

```env
DATABASE_URL=postgresql+psycopg://smartreco:password@postgres:5432/smartreco
```

Remote Qdrant:

```env
QDRANT_URL=https://your-qdrant-host
QDRANT_API_KEY=...
```

Run one APScheduler leader per deployment. Catalog and delivery work use durable compare-and-set claims, leases, retries, and stale-worker recovery without additional infrastructure.

## Tests

```powershell
pytest --cov=app --cov-report=term-missing
```

Live Mesh comparisons are opt-in so CI cannot spend credits unexpectedly.

Run the deterministic offline evaluation without LLM calls:

```powershell
python scripts/evaluate_recommendations.py
python scripts/evaluate_recommendations.py --json
```

Rebuild and verify a real Mesh/Qdrant semantic index, then run the explicit live semantic evaluation:

```powershell
python -m alembic upgrade head
python scripts/rebuild_vector_index.py
python scripts/rebuild_vector_index.py --verify-only
python scripts/evaluate_recommendations.py --semantic
```

The rebuild generates all embeddings before modifying Qdrant, never changes SQL catalog records, removes stale vectors, and refuses to call deterministic fallback “semantic.” Local development may explicitly run `python scripts/rebuild_vector_index.py --allow-degraded`.

Additional evidence commands:

```powershell
python scripts/benchmark_ai_efficiency.py --live-mesh
python scripts/evaluate_intent_evolution.py
python scripts/check_submission_hygiene.py
```

Evaluation reports Precision@3, Recall@3, NDCG@3, diversity, catalog coverage, exclusion correctness, personalization separation, hallucinated-ID rate, semantic status, latency, embedding calls, and copy-LLM calls across declared synthetic journeys. Relevance is defined in advance from authoritative catalog categories; it is not LLM-graded or a manufactured accuracy score.
