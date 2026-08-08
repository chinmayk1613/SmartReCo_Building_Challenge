# Behavioral ranking, retrieval, and generation

SmartReco separates recommendation selection from recommendation wording. Deterministic evidence and catalog retrieval choose the products. The LLM explains those already selected products and is not allowed to invent catalog records.

## 1. Observe

The browser records authenticated, user-scoped events without blocking navigation. Events are queued locally, batched, retried, and idempotently accepted by `event_id`.

Active dwell is foreground course-page time, not wall-clock time. It pauses when the tab is hidden. The browser reports a checkpoint after 15 active seconds and then every 15 seconds. Checkpoints for the same user, session, and course update one `HIGH_ENGAGEMENT` signal rather than flooding the profile.

## 2. Convert events to evidence

| Event | Signal | Strength | Trigger points |
|---|---|---:|---:|
| Page view | Browse | 0.05 | 0.5 |
| Category selected | Topic interest | 0.25 | 1 |
| Search | Explicit intent | 0.70 | 6 |
| Product viewed | Product interest | 0.35 | 3 |
| Product clicked | Product interest | 0.55 | 5 |
| Active dwell | High engagement | dynamic | 4 initially, 1 per later checkpoint |
| Saved/added to cart | Purchase intent | 0.95 | 10 |
| Cart reviewed | Cart consideration | 0.30 | 1.5 |
| Removed from cart | Cart released | 0.00 | 4 |
| Recommendation clicked | Recommendation response | 0.75 | 6 |
| Not for me | Negative feedback | -0.85 | 6 |
| Purchase started/completed | Purchase/conversion | 0.90/1.00 | 10/12 |

For dwell of at least 15 seconds, strength is `min(0.90, 0.35 + log(1 + seconds) / 10)`. A 45-second active visit therefore produces about `0.73` strength. Shorter visits do not create a dwell signal.

## 3. Build the private user profile

Each topic receives a recency-decayed score:

`topic score += signal strength * signal confidence * 2^(-age_hours / 72)`

The 72-hour half-life lets sustained recent behavior outrank old curiosity without immediately forgetting history. The profile stores the primary and secondary topics, recent searches, positive product IDs, negative product IDs, journey stage, confidence, version, and a content hash. Profiles and signals are strictly keyed by the authenticated user.

## 4. Decide whether to run the agent

An LLM is not called for every click. A run requires changed profile evidence, the configured trigger threshold (8 points by default), and either no active cooldown or sufficiently important new evidence. The profile hash prevents duplicate runs. Recommendation runs use durable leases, checkpoint state, stale-run recovery, and retry telemetry.

## 5. Retrieve and rank real products

RAG converts the behavioral profile to a retrieval query containing the primary goal, secondary interests, recent searches, and journey stage. Qdrant returns up to 40 candidate product IDs. In production, embeddings come from the configured model through Mesh and retrieval is labeled `SEMANTIC`. The deterministic local hash fallback is labeled `DEGRADED`; a Qdrant/provider failure is `UNAVAILABLE`. SQL verifies that every product still exists and is active, discarding archived and unknown vector IDs.

Every eligible product receives this deterministic score:

`0.42 * semantic similarity + 0.25 * behavioral topic match + 0.18 * search-term match + 0.08 * quality + 0.07 * popularity`

Prior positive engagement adds `0.08`. Saved, purchased, currently open, and negatively rated products are excluded where appropriate. Opening the cart adds product-specific consideration evidence for related topics, while the exact cart products stay excluded. Removing a cart item immediately makes it eligible again; only the explicit `Not for me` action creates a persistent negative preference. Results are sorted and diversified to at most two courses from one category.

### Course-detail recommendations

The course-detail page uses a contextual-behavioral hybrid, not a category lookup. The open course title, category, level, skills, and description form the Qdrant query. A candidate must first pass a deterministic relevance gate: it is in the same domain, belongs to a catalog-defined adjacent learning path, or shares meaningful skills. Semantic similarity ranks candidates after this gate; it cannot admit a cross-domain result by itself. A strong global preference also cannot admit an unrelated course.

Eligible products are ranked as `40% current-course semantic similarity + 27% learning-path relevance + 18% learner behavior + 10% shared-skill overlap + 5% level progression`. Behavior can change ranking, but it cannot admit a course that fails the current-course relevance gate. Selection permits at most two results from one category, so opening a Java course cannot simply return the other three Java records. A third result must add a defensible adjacent path such as Java to Web Technologies or Data Engineering to MLOps; if no such result exists, the panel returns fewer than three rather than adding an irrelevant item.

A contextual result is cached by learner, behavioral profile hash/version, current course/version, full catalog revision, and prompt version. An unchanged refresh reuses stored output. A changed profile, course, catalog, prompt, expiry, or recommended-product version permits one new single-flight workflow.

The home page and stored persuasive narrative remain behavior-first because their purpose is to summarize the learner's overall journey. The detail page is intentionally conditioned on both the current course and the same learner's accumulated evidence.

## 6. Generate grounded persuasion

LangGraph evaluates retrieval quality deterministically. If evidence is insufficient it refines the query once, retrieves once more, and either continues or records `not enough evidence`; it never spends an LLM call on this decision. It then passes verified candidates to the configured Mesh model. A validation node rejects unknown, missing, duplicate, or reordered IDs and fabricated price/discount/guarantee/sensitive-trait claims. A deterministic, visibly labeled fallback is used if the provider fails or output is unsafe. The validated recommendation and its evidence are persisted.

Mesh generation uses an ordered, configurable three-model failover chain. The default is `minimax/m2-her` (free), `tencent/hy3` (free), then `openai/gpt-4o-mini` (paid). Every attempted model creates its own local observability record with attempt number, latency, token usage, error type, HTTP status when available, and a classified failure scope. Model rejection, rate limiting, provider unavailability, timeout, or invalid structured output advances to the next model. Gateway connection failures and authentication/account errors stop immediately because changing models cannot repair the shared Mesh connection. Only after all eligible attempts fail does deterministic provider fallback generate grounded wording.

Catalog descriptions, searches, and behavior fields are untrusted data rather than instructions. Inputs are bounded, hosted trace exports are sanitized, exact product order and IDs are validated, and unsafe claims trigger deterministic grounded fallback.

MCP exposes read-only profile, signal, ranked-search, and verified-product tools for governed interoperability. It does not replace the in-process ranking path or gain write access. LangSmith receives graph traces when configured, while the local invocation ledger always records LangGraph, RAG, MCP, model calls, tokens, latency, status, and estimated cost.

## Example

A learner searches for `MLOps` (6 trigger points), opens an MLOps course (3), and actively studies its detail page for 45 seconds (about 0.73 strength and 4 initial trigger points). The threshold is crossed. The profile's MLOps weight rises, RAG retrieves semantically relevant active catalog entries, and deterministic ranking selects the strongest eligible courses. The Mesh LLM explains those selected records in language tailored to the learner's observed MLOps direction.

If the learner clicks `Not for me` on one result, SmartReco records a -0.85 product-specific preference. That product enters the negative set and is excluded from the next ranking run; the remaining interests and catalog candidates are re-evaluated.
