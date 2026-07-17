# DCF API Architecture Decisions

Status: accepted for implementation unless superseded by a later dated decision.
These records constrain implementation while keeping replaceable vendors behind
interfaces. Architecture records under `project-docs/` are published with the
repository so implementation decisions are reviewable.

## ADR-001 — Numeric representation and rounding

Date: 2026-07-11

Decision:

- Keep IEEE-754 Python `float` for model calculations and JSON output because the
  source provider and API contract are floating-point, and the DCF is an estimate
  rather than a financial ledger.
- Reject every non-finite input/intermediate/output.
- Do not round intermediate calculations. Round only presentation values in
  clients; the API continues returning raw finite floats unless a future API
  version introduces an explicit precision contract.
- Use tolerance-based comparisons in tests. Never compare calculated money floats
  for exact equality.
- Use integer minor units or `Decimal` later for billing/metering money, isolated
  from the valuation engine.

Consequences: calculations remain fast and compatible, while validation and
reconciliation guard against NaN/infinity and precision misuse.

## ADR-002 — Error-envelope compatibility

Date: 2026-07-11

Decision:

- Introduce one versioned machine-readable envelope with `code`, `message`,
  `request_id`, and optional `fields`.
- Preserve the current v1 `detail` contract until a documented compatibility
  release. Do not silently change existing error JSON.
- Add the new envelope in a new API version or behind an explicit negotiated
  media type, with contract tests for every supported version.
- Never expose provider keys, subscription text, stack traces, or internal storage
  details.

Consequences: generated clients get a stable contract without breaking current
consumers.

## ADR-003 — Authentication and API-key storage

Date: 2026-07-11

Decision:

- Use opaque, high-entropy customer API keys with a public identifier/prefix and a
  secret component shown once.
- Store only a keyed HMAC digest of the secret plus metadata; keep the HMAC pepper
  in Vercel environment/secret management, separate from Postgres.
- Support scopes, expiration, rotation overlap, revocation, last-used timestamp,
  and immutable audit events.
- Perform authentication and quota enforcement against external durable services;
  never rely on Vercel instance memory.
- Redact credentials from logs, traces, errors, URLs, and stored raw payloads.

Consequences: database disclosure alone does not reveal usable customer keys, and
serverless scaling cannot bypass enforcement.

## ADR-004 — Vercel data and cache topology

Date: 2026-07-11

Decision:

- Use managed external Postgres as the durable system of record for customers,
  API keys, normalized data versions, usage, and audit metadata.
- Use managed external Redis for distributed rate limits, single-flight locks, and
  ephemeral caches. Redis is not the sole durable record for billing or identity.
- Use external object storage for immutable raw provider captures.
- Select vendors later, but require Vercel-compatible connection methods, bounded
  serverless pools or HTTP access, encryption, backups, and a region close to the
  deployed function.
- Treat in-process cache as an optional warm-instance accelerator only.
- Keep Postgres, Redis, and object storage behind injectable interfaces.

Consequences: function recycling and horizontal scaling do not lose durable state
or create inconsistent enforcement, and vendors remain replaceable.

## ADR-005 — Model and data versioning

Date: 2026-07-11

Decision:

- `model_version` identifies calculation methodology and resolved-default
  semantics, not ordinary deployments.
- Increment the major version for output-breaking methodology or schema changes;
  increment the minor version for backward-compatible model features; patches may
  fix defects only when the correction is documented and reproducible.
- Assign immutable identifiers to normalized data snapshots and return the data
  version/provenance with every valuation.
- Include model version, data snapshot version, canonical assumptions, and quote
  version/time in valuation cache keys.
- Retain regression fixtures for every supported model version and never mutate
  historical snapshots in place.

Consequences: a valuation can be reproduced and cache invalidation follows actual
method/data changes instead of deployment timestamps.

## ADR-006 — Cache-aside database read-through for normalized financials

Date: 2026-07-13

Decision:

- Resolve financial data in this order: in-process L1 cache, Redis L2 cache,
  then Supabase's latest verified normalized snapshot. The external provider is
  reached from customer traffic only for a confirmed cold database miss; all
  existing-ticker refreshes belong to ADR-007's scheduled cycle.
- Run the database lookup inside distributed single-flight after the Redis miss,
  so concurrent serverless instances do not stampede either Supabase or FMP.
- On a fresh database hit, hydrate the canonical model and repopulate Redis
  before returning. An older database hit is served with explicit daily-refresh
  status; it waits for the scheduled provider cycle and never causes an
  existing ticker to refresh from a customer request.
- On a cold-bootstrap or scheduled provider success, await a transactional
  immutable-snapshot insert and mutable ticker-head upsert before publishing
  Redis and returning/completing the job. Publish the Redis `fund:` entry last
  as the cache commit marker.
- Keep normalized statement/profile documents immutable and quote-free under a
  `snapshot_version`. Maintain a mutable per-ticker head containing the latest
  verified snapshot pointer and latest observed quote/timestamps. The public
  quote-inclusive `data_version` remains a separate response concept.
- A confirmed database **miss** permits a cold bootstrap. A database error is
  not a miss and must not trigger FMP from customer traffic; return a controlled
  storage-unavailable response if no cache can serve the request. Snapshot write
  failures are observable and never presented as successful persistence. This
  does not alter the independently fail-closed API authentication/quota path.
- Validate all database payloads through the same strict canonical decoder used
  for Redis before allowing them into the DCF engine.

Consequences: cold starts and Redis evictions can reuse recently verified
financial documents without unnecessary FMP calls, successful
provider loads become durable before cache publication, immutable history is
preserved, and old rows cannot silently freeze ticker freshness forever.

## ADR-007 — Daily all-ticker FMP refresh at 6 PM Eastern

Date: 2026-07-13

Decision:

- Existing stored tickers never call FMP from a customer request. One secured
  daily run refreshes **every ticker** in `ticker_snapshot_heads`, including
  statements, profile, and quote; there is no activity filter, budget skip, or
  silent deferral.
- Vercel cron is UTC-only, so configure the same guarded endpoint at both
  `0 22 * * *` and `0 23 * * *`. The endpoint proceeds only when
  `America/New_York` local hour is 18 and atomically claims that Eastern date;
  the other invocation is a no-op. This keeps the refresh in the 6 PM Eastern
  hour across EST/EDT. On Hobby it may run anytime from 6:00–6:59 PM; exact
  6:00 PM requires a plan with minute-level cron precision or a timezone-aware
  external scheduler.
- A genuinely cold ticker with no cache or DB snapshot may bootstrap from FMP
  during its first request. That bootstrap does not satisfy or suppress the
  scheduled claim: if the ticker exists when the 6 PM manifest is created, it
  is refreshed with every other database ticker.
- At run start, atomically create one durable run record and a pending claim for
  every ticker in the database snapshot. Process every claim with bounded
  concurrency. Per-run and per-ticker records capture attempted/succeeded/failed
  counts and errors, so completion is auditable.
- Enforce at most one refresh cycle per ticker and Eastern refresh date with
  Supabase claims. Redis locks prevent concurrent work but are not the durable
  idempotency record. Duplicate cron delivery and Redis loss cannot duplicate a
  claimed provider cycle.
- Secure the internal cron route with Vercel's server-side `CRON_SECRET`, keep
  it out of OpenAPI/browser code, and reject missing/mismatched authorization.
- Provider-plan capacity and Vercel duration are deployment gates: the configured
  plan/runtime must be able to refresh the entire database. If capacity is
  insufficient, fail the run visibly and require a provider/runtime upgrade;
  never redefine "all tickers" as a smaller prioritized subset.
- Promote only complete, period-aligned statement sets. A failed/partial daily
  refresh leaves the previous immutable snapshot active with a warning.
- Expire every L1 entry no later than the next 6 PM Eastern boundary and rotate
  the shared per-ticker cache generation after each durable promotion. A warm
  instance cannot silently retain the pre-refresh dataset; response/edge cache
  carryover is bounded to its declared 60-second window with timestamps exposed.

Consequences: FMP cost for existing tickers is predictable and independent of
customer request volume, but grows with database ticker count and each refresh
cycle may contain several FMP endpoint calls/retries. Quotes become daily rather
than intraday. Existing data can be up to roughly one cycle behind a filing
(longer after provider/job failure), and customers see that tradeoff explicitly.

**Superseded in part by ADR-008 (2026-07-16):** the daily refresh cycle no
longer fetches or stores a quote. The current market price is served live from
Finnhub on every request and is never cached. This ADR's statements/profile
daily-refresh policy is otherwise unchanged; only the price/quote portion moves
to ADR-008.

## ADR-008 — Real-time market price from Finnhub, never cached

Date: 2026-07-16

Context: ADR-007 made the market price a daily-refreshed value fetched from FMP
alongside statements and profile. The `current_price` is the only market-derived
input in a valuation, and it is independent of the DCF math: it feeds only
`upside_pct` and the echoed `current_price`/`price_as_of` fields, not intrinsic
value, enterprise value, equity value, or the projections. The user requires the
response to always show a live market price.

Decision:

- The current price is fetched live from **Finnhub** (`/quote`) on **every**
  valuation request. It is **never** read from, or written to, any cache — not
  the in-process quote cache, not Redis (`quote:` is removed), not the valuation
  response cache, not the CDN/edge, and not the ADR-007 daily snapshot.
- FMP remains the source for slow-moving statements and profile; those keep
  ADR-006's cache-aside read-through and ADR-007's daily refresh. Finnhub is the
  sole source of the market price.
- The valuation request/response and the DCF math are **not** cached. The only
  caching kept is the **financial statements** (ADR-006's fundamentals
  read-through: in-process L1 → Redis `fund:`/`profile:` → DB → FMP, plus
  single-flight). That is the sole caching the product wants: multiple valuations
  of the same ticker with different assumptions reuse **one** FMP statement fetch
  instead of N, which benefits widely-used tickers directly. The DCF math is
  recomputed on every request from the cached statements + the live price — it is
  pure, deterministic, and cheap, so caching it adds no value here.
- The Phase 8 Slice B valuation **response cache (`dcf:v1:resp:`) is retired**,
  and so is the Phase 7 ETag/conditional-304 handling for `/v1/valuations/*` —
  both cache the response, which the product explicitly does not want and which a
  live price makes incorrect anyway.
- Because the HTTP response body carries a live, uncacheable price,
  `/v1/valuations/*` responses are `Cache-Control: no-store`.
- Finnhub is behind the same injectable-client, env-var auto-enable pattern as
  FMP/Supabase/Redis (`FINNHUB_API_KEY` absent → price feature off). The DCF
  engine stays pure and I/O-free; price injection happens in the API layer.
- Finnhub failure must never serve a cached/stale price. On a Finnhub outage the
  response returns the DCF math with `current_price`/`price_as_of`/`upside_pct`
  as `null` plus a data-quality warning; the intrinsic value (the core product)
  is still returned. (Alternative considered: fail the whole request 502. Chosen
  the null-price degrade so a provider blip doesn't deny the price-independent
  valuation.)

Consequences: every response shows a real-time price, at the cost of one Finnhub
call per valuation request (re-coupling request volume to Finnhub's rate limit —
60 req/min on the free tier) and the loss of response/edge caching for the
valuation endpoint. The financial-statement cache still absorbs repeated
same-ticker traffic, so N differently-assumed valuations of one ticker cost one
FMP statement fetch. `upside_pct` now reflects the live market, not a value up to
a day old.
