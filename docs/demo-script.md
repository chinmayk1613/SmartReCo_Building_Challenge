# SmartReco 3–5 minute judge demo

## Pre-demo preflight

Run these before presenting:

```powershell
python -m alembic upgrade head
python scripts/rebuild_vector_index.py
python scripts/rebuild_vector_index.py --verify-only
python scripts/evaluate_recommendations.py --semantic
python scripts/check_submission_hygiene.py
```

Only describe retrieval as live semantic when verification and evaluation both report `SEMANTIC`. If Mesh embeddings are unavailable, say that the demo is visibly `DEGRADED`; do not use the offline comparison as a live semantic claim.

Prepare two browser sessions:

- Learner: `learner@smartreco.local` / the configured seed learner password.
- Admin: `admin@smartreco.local` / the password supplied to `scripts/seed_demo.py`.

Keep `/admin/activity`, `/admin/runs`, `/admin/observability`, and `/admin/deliveries` ready in the admin session.

## 0:00–0:35 — Behavior without blocking the storefront

1. Sign in as the learner and open an Agentic AI or Python course.
2. Point out that navigation is immediate while the latest-signal panel records the product view.
3. Explain: the browser queues events locally, sends batches asynchronously, retries failures, and uses unique event IDs so retries do not double count.
4. Switch briefly to Admin → Activity. Show that the event belongs to the named learner and that the authoritative product/category is stored in SQL.

Suggested line: “SmartReco does not call an LLM for this click. It first turns authenticated behavior into inspectable evidence.”

## 0:35–1:10 — Intent and meaningful dwell

1. Search twice for a new topic such as `MLOps`.
2. Open a relevant MLOps course and keep it foregrounded for at least 15 seconds.
3. Show the dwell signal and explain that hidden-tab time is excluded; later checkpoints update one engagement signal instead of flooding the profile.
4. Mention the production profile formula: signal strength × confidence × a 72-hour half-life.

Suggested line: “Repeated search, real active attention, and the browsing path outweigh an old one-off curiosity.”

## 1:10–1:55 — Agentic recommendation and grounding

1. Open a course detail page and show the immediate generating state.
2. In Admin → Agent runs, show the live LangGraph nodes:
   `load → retrieve → evaluate quality → optional one refinement → verify → generate → validate → persist`.
3. Explain that Qdrant proposes semantically related IDs, behavior reranks them, and SQL/MCP verifies active catalog records.
4. Return to the learner page when the recommendation is current. Show the narrative connecting the open course with observed interests and the three specific next courses.
5. Point to fit confidence as an interpretable ranking score, not a calibrated purchase probability.

Suggested line: “The LLM writes the explanation; it does not choose arbitrary inventory. Exact product IDs must survive validation.”

## 1:55–2:25 — Quality gate and safe failure

Use Admin → Agent runs or the architecture explanation to show both bounded paths:

- Weak first retrieval: refine once, retrieve once more, then continue if evidence becomes sufficient.
- Still weak: stop as insufficient evidence, save the decision, and make zero persuasive Mesh calls.

Mention that tests prove the loop cannot continue indefinitely and that invalid model output falls back to verified deterministic copy rather than hallucinating products.

## 2:25–2:50 — Closed-loop feedback

1. Click **Not for me** on one recommended course.
2. Show the signal/activity appearing for this learner.
3. Refresh or trigger the next eligible recommendation and show that the exact dismissed product is excluded.
4. If time is short, run/show:

```powershell
python scripts/evaluate_intent_evolution.py
```

State the measured journey: intent moved from `python_ai` to `mlops`; rankings changed; the dismissed rank-1 product did not reappear.

## 2:50–3:25 — Catalog dual-write and operations

1. In Admin → Catalog, edit a product title or outcome.
2. Explain that SQL commits first with a versioned transactional outbox record.
3. Show pending/synchronized state. APScheduler claims the outbox with a lease; two workers cannot process the same operation independently, and an expired lease is recoverable.
4. Explain that the Qdrant payload contains embedding provider, model, dimension, schema, product version, and checksum. A mixed or legacy index is blocked with `VECTOR_INDEX_REBUILD_REQUIRED`.

## 3:25–3:50 — Privacy controls, digest opt-in, and observability

1. Open learner Preferences and explain that personalization starts enabled with a clear opt-out and history-deletion control, while digest/email delivery is disabled until the learner explicitly opts in.
2. In Admin → Delivery, show scheduled/sent/retry/failed/overdue evidence and provider receipts. The current personalization preference and explicit digest opt-in are checked again immediately before sending.
3. In Observability, show local provider attempts, tokens, latency, failures, and LangSmith reconciliation.
4. Open one safe LangSmith trace: learner identity is pseudonymous; raw graph state, sessions, CSRF, keys, and prompts are hidden/redacted.

## 3:50–4:30 — Finish with measured evidence

Show `docs/evaluation-results.md` or run:

```powershell
python scripts/evaluate_recommendations.py
python scripts/benchmark_ai_efficiency.py --live-mesh
```

Close with:

- Same ten journeys/catalog/labels/exclusions/K=3: Popularity NDCG@3 `0.1633`, real Mesh Semantic-only `0.8038`, SmartReco hybrid with real semantic retrieval `0.9739`.
- Live-semantic SmartReco improvement over Semantic-only: `21.16%` NDCG@3; improvement over Popularity: `496.39%`.
- The comparison made ten Mesh embedding calls per semantic system and zero recommendation-copy LLM calls because it evaluates retrieval/ranking, not narrative generation.
- Exclusion pass rate: `100%`; hallucinated catalog-ID rate: `0%` in the evaluation.
- Live efficiency run: 100 events → 10 batches → 10 trigger evaluations → 2 recommendation workflows → 2 real Mesh copy calls.
- Measured reduction versus naive one-call-per-event architecture: **98.0%**.

Final line: “SmartReco is not a related-course widget. It is a privacy-controlled behavioral loop that retrieves semantically, ranks deterministically, generates persuasively, verifies operationally, learns from feedback, and proves each claim with tests and measured evidence.”
