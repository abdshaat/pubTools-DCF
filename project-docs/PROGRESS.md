# Progress Log

## 2026-07-18 — ADR-008 Slices 2+3+4 (live Finnhub price, structural change) + Phase 8 Slice C parts 1–2 (durable snapshots, DB read-through, daily-refresh scheduler)

> The session that implemented this timed out before it could write this entry;
> backfilled at the start of the next session from `issues.MD`'s slice record
> plus a fresh full verification of the working tree. All of it is uncommitted.

The route rewrite, cache retirement, and docs landed as one unit (Slices 2–4 of
the `issues.MD` Finnhub feature). Owner resolutions made before implementation:
(a) **quota flow simplified** — one atomic `check_and_increment` pre-flight
replaces the peek/deferred-consume split (which existed only so 304s could be
free; there are no 304s under `no-store`), and `X-RateLimit-*` headers return on
all valuation responses; (b) a **Finnhub unknown-symbol for an FMP-served ticker
degrades like an outage** (null price + warning, never a 404/502); (c)
**`model_version` 0.1.0 → 0.2.0** for the nullable-price schema change (OpenAPI
snapshot regenerated).

- **Engine/model split:** `BaseFinancials` and `Valuation` carry **no price
  fields at all** (stronger than Optional — a cached snapshot cannot represent
  a price by construction); `compute_dcf` dropped its `current_price > 0`
  precondition and internal upside math; upside is computed in
  `build_valuation_response(quote=...)` from the live quote.
- **Route:** statements via the fundamentals cache → live Finnhub fetch →
  fresh `compute_dcf` → `Cache-Control: no-store`. Any Finnhub failure
  (outage, rate limit, unknown symbol, auth misconfig, feature off) degrades
  to null price/upside + a warning. `app.state.finnhub` is lifespan-wired
  (auto-enable on `FINNHUB_API_KEY`, injectable for tests).
- **Removed:** `app/response_cache.py`, `app/http_cache.py`, the ETag/304
  branch, `Vary`, the peek phase, `FundamentalsService`'s quote cache
  (L1 + Redis `quote:` + stale-quote logic), the FMP `quote` endpoint from
  `fetch_fundamentals` (cold load is now 4 FMP calls, not 5),
  `FMPClient.fetch_quote`, and `normalize_fmp_quote`. Login-limiter tests
  moved to `tests/test_login_rate_limit.py`.
- **Docs:** README + `docs/index.html` "Live price & caching" rewritten;
  examples/version strings at 0.2.0; `base_financials` example is price-free.
- **Live-verified** against real uvicorn with real FMP+Finnhub keys (AAPL live
  price, null-price degrade with the key blanked — details in `issues.MD`).
- **Suite 323 → 291 passing** (net of the removed cache tests), 94.08%
  coverage; ruff/format clean.

**Continuation session (same day, after the timeout):** re-verified the tree
(291 passing, 94.08%, ruff/format clean) and found the one thing the interrupted
session missed — **mypy failed in `scripts/`**: `scripts/smoke_fetch.py` still
printed `valuation.current_price`/`upside_pct` and read `current_price` off
`BaseFinancials`, and `scripts/build_demo_snapshots.py` still passed `quote=` to
`FMPFundamentals`. Both fixed to the price-free model; mypy is now clean across
`app` + `scripts`. Also scrubbed the remaining pre-ADR-008 quote/response-cache
references out of `IMPLEMENTATION_PLAN.md` Phase 8 (key table, migration-003
head columns, single-flight/promotion/exit-criteria text) and added the Phase 7
supersession note, closing the three "cross-doc updates required" boxes in
`issues.MD`.

**Same session — Phase 8 Slice C part 1 implemented (durable snapshots + DB
read-through; scheduler still to come):**

- **`supabase/migrations/003_phase8_snapshots.sql` (new, NOT yet applied):**
  immutable `normalized_snapshots` (content-addressed by `snapshot_version` =
  sha256 over the canonical price-free base payload — same recipe as the
  response `data_version`, so a re-confirmed identical filing dedups via ON
  CONFLICT DO NOTHING; UPDATE/DELETE revoked from every role incl.
  service_role), mutable `ticker_snapshot_heads`, the `financial_refresh_runs`
  / `financial_refresh_claims` ledger for the daily job, and the
  `store_ticker_snapshot` RPC (atomic snapshot insert + head upsert,
  service-role-only execute, same lockdown as 001). Run-orchestration RPCs
  land with the scheduler; the file may be extended until first applied.
- **`SupabaseClient.get_ticker_snapshot`** (head + snapshot in one PostgREST
  FK-embed query; typed `TickerSnapshotRecord`; malformed rows → None so a
  bootstrap repairs them; HTTP errors raise = fail-closed) and
  **`.store_ticker_snapshot`** (the RPC call, awaited before any cache write).
- **`FundamentalsService` read order is now L1 → Redis → DB → FMP(cold only)**
  behind a new injectable `snapshots=` store (None → exactly the old behavior;
  auto-wired to the Supabase client in `create_app`). An existing DB ticker is
  served as stored — **customer requests never trigger FMP for it**, including
  the old request-time profile refresh (gated off when the store is
  configured); stale DB data gets a "scheduled refresh pending" warning with
  its `verified_at`. A store **error is not a miss** (new `SnapshotStoreError`
  → 503 `snapshot_store_unavailable`, `no-store`): degrade to a bounded stale
  cache copy when one exists, otherwise fail closed — never an FMP fallback. A
  cold bootstrap awaits the durable write **before** publishing Redis
  (`fund:` stays the last-written commit marker) and before returning; a
  failed write publishes nothing and caches nothing.
- **Tests: 20 new in `tests/test_snapshots.py`** (DB-hit zero-FMP serve, L1+L2
  hydration across instances, stale-serve warning, no-request-time-refresh with
  aged caches, cold bootstrap persists + dedup on reconfirmation,
  db-write-before-redis ordering via event log, store-failure publishes
  nothing + retry recovers, read-outage fail-closed with cold caches vs
  bounded-stale serve with warm ones, malformed/wrong-ticker/future-dated row
  repair, fingerprint stability, typed-record parsing, route-level 503).
  `tests/fake_supabase.py` gained head/snapshot/store handlers + outage
  toggles; the strict Supabase call-order test now asserts
  lookup → last_used → quota → snapshot_read → snapshot_store → usage.
- **Suite: 291 → 311 passing, 93.73% coverage** (93% floor); ruff/format/mypy
  clean.

**Same session — Slice C part 2 implemented: the daily 6 PM Eastern refresh
scheduler:**

- **Migration 003 extended (now complete, still unapplied):**
  `begin_financial_refresh_run` (atomic Eastern-date claim via the
  `refresh_date` PK insert + full-manifest pending claims for **every**
  `ticker_snapshot_heads` row — no filter, no budget skip; duplicate delivery
  gets `already_claimed`) and `finish_financial_refresh_run` (counts
  reconciled **from the claims**; any still-pending claim forces
  `partial_failed` — a ticker cannot disappear silently). Same
  service-role-only execute lockdown as 001.
- **New `app/refresh.py`:** `DailyRefreshRunner.run_if_in_window()` — converts
  the injectable wall clock to `America/New_York`, proceeds only in the 18:xx
  local hour (so of the two UTC crons exactly one passes in EST and in EDT),
  claims the Eastern date, refreshes each manifest ticker under a 3-slot
  semaphore, marks each claim succeeded/failed (`error_code` = exception class
  name only — provider messages can embed URLs/keys and belong in no ledger),
  and finishes with reconciled counts. A failed claim-completion write is
  swallowed: the pending claim is itself the durable "not confirmed" signal.
  A Redis job lock was deliberately **not** added — the plan allows it only as
  extra concurrency control, and the transactional date claim already makes a
  second same-day run impossible.
- **`FundamentalsService.refresh_from_provider()`** — the scheduled path:
  fetch + normalize, persist via the shared `_persist_snapshot` (status
  `current_as_of_daily_refresh`), then replace L1/Redis (`fund:` last). Never
  called from customer requests; failures propagate so the runner fails that
  claim while the prior head/caches stay active.
- **`GET /internal/cron/refresh-financials`** (excluded from OpenAPI):
  `Authorization: Bearer {CRON_SECRET}` checked with `hmac.compare_digest`,
  one generic 401 for unconfigured/missing/mismatch; 503
  `refresh_not_configured` when Supabase (and thus the ledger) is absent;
  otherwise returns the runner's summary, `no-store`. New **`vercel.json`**
  schedules it at both `0 22 * * *` and `0 23 * * *` (crons are the file's
  only key — serving stays on `[tool.vercel]`). `CRON_SECRET` is read at app
  construction; `tests/conftest.py` isolation now clears it.
- **`tzdata` added as a Windows-only runtime dependency** (platform marker) so
  `zoneinfo.ZoneInfo("America/New_York")` works on the dev machine; Linux/
  Vercel uses the system tz database and installs nothing new.
- **Tests: 14 new in `tests/test_refresh.py`** — EDT window (22 UTC runs,
  23 UTC no-ops) and EST window (23 UTC runs, 22 UTC no-ops) with the exact
  `scheduled_window_at` instants asserted; duplicate delivery spends zero
  provider calls; a 2-ticker manifest run end-to-end through the Supabase fake
  (claims/run/heads/Redis all verified, 4 FMP calls per ticker, no quote); one
  failing ticker → its claim fails with a bounded code, the run ends
  `partial_failed`, its prior head stays active, pending = 0; empty-manifest
  run; `refresh_from_provider` cache replacement + store-required guard;
  endpoint auth (no secret configured / missing / wrong / correct), 503
  unconfigured, and a full endpoint-to-ledger integration run with the
  lifespan-built runner. `tests/fake_supabase.py` gained run/claim handlers
  mirroring the RPC semantics.
- **Suite: 311 → 325 passing, 93.58% coverage** (93% floor); ruff/format/mypy
  clean.

**⚠️ Deploy-ordering gate:** once this code deploys with Supabase configured, a
missing migration-003 table is a **storage error (503 on cold tickers), not a
miss** — apply migration 003 (now final) before deploying, exactly like
001/002, and set `CRON_SECRET` in Vercel Production before relying on the
cron. Nothing is committed yet, so production is unaffected today.

**Next (Slice C part 3 / remaining):** the 6 PM Eastern L1 hard-expiry
boundary (a warm instance must not serve pre-window L1 data as current after
the window; due/running data must not be re-cached as current), structured
freshness fields in the response (`freshness_status`,
`next_refresh_window_at`, last attempt/success — today only
`data_quality_warnings` carries freshness), then live verification — blocked
on TODO §4 (env pull, `CRON_SECRET`, migration apply, FMP capacity gate).

## 2026-07-17 — TODO answers processed; Finnhub Slice 1 implemented (client + normalization)

Processed the owner's answers written into `project-docs/TODO.md`:

- **2.1 Course Portfolio page: dropped permanently** (owner: "completely
  unnecessary"). The nav link was already removed; item closed.
- **2.2 Domain:** owner observed "ashaat.dev now redirects"; live re-verified —
  the apex still 308s to **www** (www remains primary, unchanged from
  2026-07-16). Sign-in works through the query-preserving 308; flipping to
  apex-primary (TODO 1.4c) stays open as a fragility, not a blocker.
- **2.3 Unused images: yes** — added an **AWS Certified Cloud Practitioner**
  card to `docs/portfolio.html`'s certifications section with its logo (copied
  into `docs/Pics/`). **No date was invented — owner must supply the earned
  date (or "in progress")** for the card. The other three images stay skipped
  (two are non-transparent duplicates; the Java logo has no section).
- **2.4 Phase 15: keep on hold** — recorded; plan already says so.

**Started the ADR-008 architecture shift — Slice 1 complete** (the only part
workable without a Finnhub key; no route wiring, so production behavior is
unchanged):

- New `app/providers/finnhub.py`: `FinnhubConfig.from_env()` auto-enable
  pattern, `FinnhubClient.fetch_quote()` (3s timeout, **no retries** — the
  caller will degrade to a null price rather than wait out a ladder),
  401/403 → `ProviderAuthError`, 429/5xx/unsupported → `ProviderError`,
  all-zero body (Finnhub's unknown-symbol shape) → `TickerNotFoundError`.
  Transport exceptions are **chained, never interpolated** into messages so the
  URL-embedded token cannot leak (deliberate deviation from the FMP client).
- New `normalize_finnhub_quote()` in `app/normalization.py`: `c`→price,
  `t`→`price_as_of` (UTC; `t == 0` means absent), non-finite/≤0 rejected.
- 25 tests in `tests/test_finnhub.py` via `httpx.MockTransport` in the repo's
  sync + `asyncio.run` style (the repo has no async pytest plugin — first
  draft used `@pytest.mark.anyio` and was rewritten after checking).
- **Suite 298 → 323 passing, 94.25% coverage; ruff/format/mypy clean.**

**Next:** Slice 2 (decouple `compute_dcf` from `current_price`, live-price
injection in the route, `no-store`) — needs no key to build, but live
verification and production activation wait on TODO §3 (`FINNHUB_API_KEY` in
Vercel + `.env`). Slices 2–3 must land together with the key configured, or
production would lose the FMP quote path with nothing replacing it.

## 2026-07-16 — Phase 9 created and Slices 1–2 implemented: public site (portfolio + API directory)

User asked (urgent) to merge their standalone HTML/CSS portfolio into this
deployment, make it the front page, and give visitors a button to a list of
their developed APIs — then move their domain here. Chosen shape: **one repo/
Vercel project** (over a two-project proxy/rewrite split), portfolio at `/`, DCF
at `/dcf`.

**Plan:** inserted a new **Phase 9 — Public site: portfolio front end, API
directory, and domain migration** immediately after Phase 8 per the user's
request; renumbered old Phases 9–14 → 10–15 (same precedent as the Phase 6
insertion), including cross-references and the delivery-milestone ranges.

**Conflict surfaced and recorded:** new Phase 9 is the direct opposite of old
Phase 14 (now **Phase 15**, "Separate UI/UX from the microservice"), which plans
to strip all bundled UI out and make this headless. Phase 9 supersedes it in
practice; Phase 15 is marked **on hold — must be re-scoped or dropped**, and the
delivery milestones note it. The pubTools multi-product goal survives as the
`/apis` directory (one card per future product).

**Implemented (Slices 1–2):**

- **Routes** (`app/api.py`): `GET /` → `docs/portfolio.html`; `GET /apis` →
  new `docs/apis.html`; `GET /dcf` → `docs/index.html`; `GET /Pics/{filename}`
  → images with `Cache-Control: public, max-age=31536000, immutable` and an
  explicit traversal guard (`_PICS_DIR not in target.parents` → 404). New
  `_DOCS_DIR`/`_PICS_DIR` constants. All three pages keep the existing CSP.
- **CSRF bootstrap moved from `/` to `/dcf`** — the account UI lives there and
  the portfolio has no state-changing calls.
- **Auth callback retargeted** `{base}/` → `{base}/dcf` (success) and
  `{base}/?login_error=` → `{base}/dcf?login_error=`. Caught by tracing, not
  testing: `/` is the portfolio now, which has no account UI and no
  `login_error` handler, so a signed-in user would have silently landed on the
  wrong page. `docs/index.html`'s handler uses relative
  `window.location.pathname`, so it needed no change.
- **`docs/portfolio.html`** restyled earlier this session to `index.html`'s
  design system (verbatim token block, sidebar+main layout, shared components,
  light/dark); now has the **"View my APIs"** CTA → `/apis` plus sidebar links.
- **`docs/apis.html`** (new): same design system; DCF card (live, `GET
  /v1/valuations/{ticker}`, links to `/dcf`) plus a "more tools in progress"
  card framing the shared account/key system.
- **`docs/Pics/`**: copied the 7 referenced images from the source portfolio repo
  (they were never actually added — only `portfolio.html` was). 4 unreferenced
  images deliberately skipped to keep the function bundle lean.
- **Tests:** `_seed_csrf` now seeds from `/dcf` (would have broken every account
  test). Replaced `test_root_serves_customer_landing_page` with a parametrized
  security-header test across `/`, `/apis`, `/dcf`, plus tests for the portfolio
  CTA, the API directory listing DCF, `/dcf` minting the CSRF cookie, and images
  being immutable + traversal-safe. **292 → 298 passing, 94.07% coverage**;
  ruff/format/mypy clean.
- **Live-verified against real uvicorn** (not just TestClient): `/` portfolio
  200, `/apis` 200 linking to `/dcf`, `/dcf` 200 setting `pt_csrf`,
  `/Pics/blob.png` 200 `image/png` 85679 bytes with the immutable header,
  `/health` 200.

**Next / blocked on the user (Slice 3, `issues.MD`):** the domain migration is
dashboard work — move the domain off the portfolio project, attach it to
`pub-tools-dcf`, update Production `PUBLIC_BASE_URL`, add
`https://{new-domain}/v1/auth/callback` to Supabase's redirect allowlist, and
update the hardcoded base URL in `docs/index.html`. **Skipping any of those
breaks sign-in** — the same failure mode as the 2026-07-13 `PUBLIC_BASE_URL`
incident.

## 2026-07-16 — Planning only: real-time current price from Finnhub (ADR-008)

Design/documentation only, no code. User decided the response must always show a
**live** market price fetched from Finnhub, and that **no current price may be
cached anywhere** (not the quote cache, not Redis `quote:`, not the `resp:`
response cache, not the CDN/edge, not the ADR-007 daily snapshot). Grounded the
plan in a fresh read of the actual price flow: `FundamentalsService._get_quote`
(60s L1 / Redis `quote:`), the whole-response `dcf:v1:resp:` cache, and
`compute_dcf`'s internal `current_price > 0` precondition + upside calc.

**User clarified the caching scope (same session):** the *only* caching wanted is
the **financial statements** for widely-used tickers — three different discounts
on AAPL must hit FMP for statements once, not three times. The request/response
and the DCF math are **not** cached; the math is recomputed per request (pure and
cheap) from the cached statements + the live price. This **retires the Phase 8
Slice B response cache (`dcf:v1:resp:`)** and the Phase 7 ETag/304 handling for
this endpoint; the Slice A `fund:`/`profile:` statement cache + single-flight are
kept as exactly the "reuse the statements" mechanism.

Necessary consequences documented and accepted: `/v1/valuations/*` becomes
`Cache-Control: no-store`; one Finnhub call per request (re-couples volume to
Finnhub's 60/min free-tier limit); the cached statement snapshot becomes
price-free; and a Finnhub-outage degrade to null price + warning (never a stale
price).

Wrote: **ADR-008** in `ARCHITECTURE_DECISIONS.md` (with an ADR-007 supersession
note that the daily cycle no longer fetches a quote); a full feature definition
+ 4-slice implementation plan in `project-docs/issues.MD`; and a Phase 8
supersession note in `IMPLEMENTATION_PLAN.md` so Slice C doesn't re-introduce a
cached quote. No application code, config, migrations, or tests changed.

**Read-path affirmed (same session):** user confirmed the statement read order is
cache → database → FMP, computing/returning at the first layer that has the data,
and chose "serve DB copy; scheduled job refreshes" for stale existing tickers —
i.e. a customer request never refreshes an existing ticker from FMP (only the
ADR-007 daily job does); FMP is hit only for a genuinely cold ticker absent from
cache and DB. This is exactly ADR-006 + ADR-007 (no design change). Reconciled the
Phase 8 "Authoritative cache-aside read/write flow", the daily-refresh scope, and
the Slice C task to drop the retired `quote:`/`resp:` caches and state the
cache → DB → FMP path explicitly; added a "Customer-request read path" section to
the `issues.MD` feature. The DB step remains Phase 8 Slice C (unimplemented);
the Finnhub feature can ship before it.

**Next:** implement Slice 1 (Finnhub client + `normalize_finnhub_quote` + unit
tests) once the plan is approved.

## 2026-07-15 — Published project documentation in the repository

User chose to publish `project-docs/`. Updated `.gitignore` so the repository
continues ignoring root/local Markdown notes by default while explicitly
tracking both `.md` and `.MD` files directly under `project-docs/`. Added all six
current documents: architecture decisions, frontend design notes,
implementation plan, issue tracker, progress log, and request-flow reference.

Removed current documentation claims that the directory is local/gitignored and
updated README contributor guidance to describe the documents as published
project context. Older progress entries that mention the previous ignore policy
remain historical records; that policy is superseded by this entry.

## 2026-07-14 — Added end-to-end request-flow reference

Created `project-docs/REQUEST_FLOW.md` from a fresh trace of the current code
(through Phase 8 Slice B). It documents the exact origin lifecycle and code
ownership for: Vercel/ASGI entry, startup dependencies, edge-cache bypass,
request IDs/security headers, API-key auth, non-consuming quota pre-flight,
FastAPI validation, distributed response caching, L1/Redis fundamentals,
single-flight, FMP transport/retries, normalization/latest-compatible filing
selection, DCF/sensitivity calculation, response/ETag construction, atomic
post-route quota consumption, usage metering, and error mapping.

The guide also traces every browser-facing route: landing/CSRF bootstrap,
GitHub PKCE, email magic link, shared callback, silent session refresh,
list/create/revoke/rotate/rename keys, logout, health, generated API docs, and
unmatched routes. It records two non-obvious current behaviors rather than
idealizing them: domain-level DCF validation occurs after financial-data loading
on a response-cache miss, and a Vercel public-cache hit bypasses origin auth and
quota by the accepted Phase 7 design.

To prevent implementation-status confusion, the guide separates the live
Slice A/B path from planned Slice C. It explicitly states that financial
snapshot tables/read-through, the cron endpoint, the daily all-ticker refresh,
and generation rotation's producer do not exist yet. Documentation-only change;
no runtime code, configuration, migrations, or tests changed.

## 2026-07-14 — Phase 8 Slice B implemented (response cache + Redis login limiter)

Implemented per the frozen Slice B spec; no scope drift into Slice C (the
scheduled-refresh/DB work stays next).

- **`app/response_cache.py` (new):** distributed valuation response cache.
  Fingerprint = SHA-256 over the *resolved* `Assumptions` (per-year growth
  tuple, defaults applied) + the `sensitivity` flag + `model_version`, so
  every equivalent request form shares one entry and a model bump can never
  serve stale math. Key `dcf:v1:resp:{TICKER}:{generation}:{fingerprint}`;
  the generation comes from `dcf:v1:gen:{TICKER}` (absent → "0") so Slice
  C's post-promotion rotation instantly orphans every cached
  assumption-variant with no key enumeration — and needs no Slice B code
  change (covered by a test that rotates the key manually). 60s TTL.
  `request_id`/`computed_at` are stripped before store, re-injected on hit;
  the ETag excludes exactly those fields, so a hit reproduces the original
  ETag by construction (asserted). Cached payloads failing pydantic
  validation, or carrying per-request fields, are deleted and recomputed.
- **Route integration:** a hit skips FMP *and* `compute_dcf` (closing Phase
  7's "quota-free ≠ compute-free" caveat within the TTL window), honors
  `If-None-Match` (304-from-cache tested), and is still metered — the
  middleware's quota/usage logic is untouched and a test proves a hit still
  increments both. Errors are never cached (the Slice A negative ticker
  cache already covers definitive rejections).
- **`RedisLoginRateLimiter` (`app/rate_limit.py`):** pipelined
  `INCR`+`EXPIRE` on `dcf:v1:login:{ip}:{utc-date}`; the TTL is refreshed on
  every increment — harmless since the key embeds the date, and it repairs
  the classic "INCR landed but EXPIRE never ran" leak automatically. Fails
  open to the in-process limiter (tested), keeping sign-in available through
  a Redis outage. Wired in `create_app`'s lifespan only when Redis is
  configured, so unconfigured/local setups behave exactly as before. This
  closes the long-standing instance-local login limiter gap in `issues.MD`.
- **Fail-open convention aligned:** the new paths catch broad `Exception`
  like Slice A's fundamentals L2 (not just `RedisError`), so no backend
  misbehavior can 500 a valuation or block a login.
- **Tests:** 16 new in `tests/test_response_cache.py`. No-recompute is
  proven by monkeypatch-counting `compute_dcf` calls, not timing. Coverage:
  cross-instance cache hit with fresh `request_id`/`computed_at`,
  equivalent-form sharing, sensitivity-in-key, 304-from-cache, metering
  unchanged on hits, errors-never-cached, generation rotation,
  corrupt-entry self-heal, TTL expiry, Redis-down fail-open (route still
  200s), fingerprint invariants, cross-instance login limiting (two
  TestClient apps sharing one fake backend hit one shared counter),
  UTC-day reset, limiter fail-open, negative-limit validation.
- **Suite: 276 → 292 passing, 94.04% coverage** (93% floor); ruff, ruff
  format, and mypy all clean.
- **Not done here:** Slice C (migration 003, DB read-through, 6 PM Eastern
  scheduled refresh, generation rotation's real producer) and live Upstash
  verification (needs a deploy; Slice B works locally against the in-memory
  fake and auto-enables on Vercel where the KV_* vars already exist).

## 2026-07-13 — Final architecture: refresh every DB ticker daily at 6 PM Eastern

User finalized the provider policy: one secured scheduled run refreshes the
complete FMP input set (statements, profile, and quote) for **every ticker in
`ticker_snapshot_heads`** each Eastern calendar day. Existing tickers never call
FMP from customer traffic. The only request-time exception is a confirmed cold
ticker with no cache or database snapshot, which bootstraps once and is stored.
That bootstrap does not exempt it from the 6 PM run if it is present when the
daily manifest is created.

Because Vercel cron expressions are UTC-only, Phase 8 now specifies both
`0 22 * * *` and `0 23 * * *` against the same endpoint. An
`America/New_York` local-hour check and atomic Eastern-date run claim allow only
the invocation that falls in the 6 PM Eastern hour to proceed. Vercel Hobby may
start it anywhere from 6:00–6:59 PM; exact 6:00 PM requires minute-precise
scheduling or a timezone-aware external scheduler.

Migration 003/Slice C now includes a durable run row and a per-ticker manifest.
At run start every current database ticker receives a pending claim; every claim
must end success or explicit failure. There is no activity filter, popularity
ordering, budget skip, or silent deferral. Provider-plan capacity and runtime
duration are deployment gates: insufficient capacity fails visibly and requires
an upgrade or durable worker rather than shrinking the manifest. Duplicate cron
delivery or Redis loss cannot repeat a claimed ticker cycle. Successful data is
committed to Supabase before cache publication; failures retain the previous
dataset and become customer-visible freshness warnings.

The design also closes the warm-instance gap: L1 entries hard-expire at the
next 6 PM Eastern boundary, due/running data is not re-cached as current, and a
successful DB promotion rotates the shared ticker generation before new cache
data is used. The existing edge/response cache can carry an older response for
at most its declared 60-second window, with data timestamps still visible.

Cleaned Phase 8, ADR-006/007, `issues.MD`, and this log to remove the superseded
single-23:00-UTC schedule, independent intraday quote path, activity-prioritized
batches, budget deferrals, and database-error-to-FMP fallback. No application
code or migration was written in this design-only update. Slice A still contains
the older request-time/quote TTL behavior; Slice C must replace it before this
policy is operational. Before deployment the owner must configure `CRON_SECRET`
and verify FMP/runtime capacity for the entire ticker manifest.

## 2026-07-13 — Architecture addition: DB read-through before provider

Design-only update requested before continuing Phase 8. The fundamentals path
is now fixed as L1 memory → Redis L2 → Supabase latest verified snapshot → FMP.
A DB hit repopulates cache before returning; a provider success awaits the DB
write and then publishes Redis (`fund:` last) before returning. Old DB rows do
not become permanent cache hits: a mutable per-ticker head records
`verified_at`; this entry originally had stale snapshots trigger request-time
provider refresh. **Superseded by ADR-007 above:** existing snapshots now wait
for the once-daily scheduled provider cycle and expose freshness warnings.

Expanded migration 003 from one immutable table to two roles:
`normalized_snapshots` stores quote-free immutable financial documents keyed by
internal `snapshot_version`; `ticker_snapshot_heads` is a mutable pointer to the
latest verified snapshot and latest observed quote/timestamps. This resolves a
latent inconsistency in the earlier design: the public `data_version` includes
the current quote, while durable quote history is explicitly deferred, so it
cannot honestly be the primary key for immutable statement documents.

Added ADR-006 and expanded Slice C to include DB reads, cache hydration,
transactional snapshot/head writes, freshness/corruption handling, and ordering
tests. **Superseded by the final entry above:** a snapshot DB error is not a
confirmed miss and must not cause customer traffic to call FMP; publication of
new provider data requires a successful durable write. Supabase auth/quota
remains independently fail-closed. No application code or migrations were
created in this design update. Next implementation slice remains Slice B,
followed by the expanded Slice C.

## 2026-07-13 — Phase 8 Slice A implemented (Upstash L2 + distributed single-flight)

Implemented the first Phase 8 slice after the user confirmed Upstash was
connected to the Vercel project. Added `app/redis_cache.py`: environment
auto-detection for current Upstash and legacy Vercel-KV variable names, a
short-timeout Upstash REST client over the existing `httpx` dependency,
versioned JSON envelopes, token-safe compare-and-delete locks, and an
injectable in-memory backend with TTL/NX/counter/pipeline semantics.

`FundamentalsService` now keeps its existing warm-instance L1 caches and adds
Redis L2 entries for normalized fundamentals, profiles, quotes, and definitive
negative outcomes. Redis TTLs preserve the existing fresh-versus-bounded-stale
semantics. Corrupt/unknown/type-invalid entries are deleted and treated as
misses. Redis outages fail open to the provider. Cross-instance cold loads use
`SET NX PX` single-flight; losers poll for at most 3s and then fetch, so a dead
holder cannot block valuations. `fund:` is written last as the commit marker so
waiters never observe it before companion profile/quote entries are available.

Fresh inspection found one incorrect planning assumption: a 10s lock TTL did
not cover the real FMP path (five endpoints, three concurrent slots, up to
three six-second attempts plus delays). The TTL is now 45s; loser wait remains
3s, preserving fast crash fall-through. Phase 8 was corrected accordingly.

Tests added for two service instances sharing one backend/one provider load,
shared negative cache, corrupt entry replacement, Redis-down fail-open,
crashed-holder fall-through, distributed stale-if-error, Upstash command
encoding/error handling, envelope cleanup, and fake TTL/NX/token/counter
semantics. Full verification: **276 passing, 93.82% coverage**, Ruff format and
lint pass, mypy passes. Supabase authentication/quota/metering code was not
changed.

**Next:** Slice B (valuation response cache + Redis-backed login limiter).
Live Upstash verification still requires deploying this code; local Upstash
variables have not been pulled into `.env`, so no production Redis secrets were
read or printed in this session.

## 2026-07-13 (evening) — Planning only: Phase 8 fully specified (Upstash Redis + migration 003)

User said "start the next phase" (superseding the earlier same-day "not
yet" on Phase 8). Asked the two genuinely-open questions first; answers:
**Upstash Redis via Vercel Marketplace** as the provider, and **design
document only this session** — no code. Wrote the full Phase 8 design into
`IMPLEMENTATION_PLAN.md` (same planning-session pattern that preceded
Phase 7's implementation), grounded in a fresh read of
`app/fundamentals.py`'s actual cache semantics rather than memory.

Key design decisions now locked in the plan:

- **Redis is accelerator/coordinator only; the Supabase quota/metering path
  is deliberately untouched** (already atomic, durable, fail-closed —
  ADR-004 says Redis is never the billing record).
- Upstash REST over plain `httpx` (no new runtime dependency, same pattern
  as `app/supabase.py`); auto-enables via env vars exactly like Phase 5
  Supabase; ~1s timeouts; every Redis failure degrades per an explicit
  fail-open/fail-closed matrix (only Supabase auth/quota stays fail-closed).
- Key namespace `dcf:v1:*` with a versioned JSON envelope
  (`{"v":1,"t":stored_at,"d":...}`); corrupt/unknown entries = miss+delete.
  The original Slice A TTL design used 24h statements/15m quotes and 4h/60s
  freshness. **Superseded for Slice C by the final daily-refresh decision
  above:** fundamentals/profile/quote share daily-run freshness metadata and
  a 48h Redis retention window for one-cycle failure fallback.
- Distributed single-flight: originally planned as `SET NX PX 10s`, corrected
  during Slice A implementation to 45s after measuring the actual bounded
  multi-endpoint retry path; token
  compare-and-delete release, losers poll then **fall through and fetch
  anyway** (never block on a crashed winner); layered over the existing
  in-process coalescing, not replacing it.
- Valuation response cache (60s TTL = s-maxage) closes Phase 7's "304 is
  quota-free but not compute-free" caveat; hits still consume quota
  (middleware unchanged); ETag identical by construction since it already
  excludes the two per-request fields.
- Login rate limiting moves to Redis `INCR` per ip+day (fixes the known
  instance-local-limiter gap in issues.MD), fail-open to the in-process
  limiter.
- Migration 003 was originally planned as immutable `normalized_snapshots`
  keyed by response `data_version`. **Superseded by the newer 2026-07-13
  ADR-006 entry above:** quote-free `snapshot_version` plus a mutable ticker
  head and DB read-through. Durable quote history remains deferred to Phase
  11; raw captures stay Phase 9.
- Implementation sliced A (Redis client + fundamentals L2 + single-flight),
  B (response cache + login limiter), C (migration 003 + DB read-through and
  snapshot/head writes),
  each with tests against an in-memory fake — a live Upstash instance is
  only needed for final verification.
- Also updated `issues.MD`: marked the quota-race and no-store findings
  fixed (commit `045d202`), pointed the login-limiter item at Phase 8
  Slice B, and added the user's Upstash provisioning checklist.

**Resolved 2026-07-13:** user provisioned Upstash and approved implementation;
Slice A is complete. Pulling the local env and live deployment verification
remain separate follow-up steps.

## 2026-07-13 (later still) — Removed stale/unused local files and folders

User asked to clean up unused/unnecessary files and folders after the
markdown reorganization above. Investigated each candidate before touching
anything (checked `git status`/`git check-ignore`, pyproject dev-extras,
`.vscode/settings.json`, and code references) rather than deleting on sight;
the permission system itself flagged two borderline items and required
explicit user sign-off before proceeding, which was given. Removed (all
gitignored, none git-tracked, zero git-history impact):

- **`.venv-store-broken/`** (54MB) — an abandoned venv from the Phase 0
  Windows/OneDrive recovery attempt (literally named broken).
- **`.venv/`** — incomplete: missing `ruff`/`mypy` despite being declared in
  `pyproject.toml`'s `[dev]` extra, and superseded in practice by
  `.venv313` (the environment actually used for every command this session
  and named explicitly in this file's "Run tests" line). Recreatable via
  the standard command in `README.md` if ever needed.
- **`node_modules/`** — contained only `@vercel/speed-insights`, an orphaned
  package with no `package.json`/`package-lock.json` anywhere in the repo
  and zero references in `docs/index.html` or any Python file.
- **`dist/`** — an old built wheel/sdist; regenerates via `python -m build`.
- **Four `__pycache__/` dirs** (`app/`, `app/providers/`, `scripts/`,
  `tests/`) — Python bytecode cache, regenerates automatically.
- **`.env.local`** — contained only a Vercel-CLI-generated
  `VERCEL_OIDC_TOKEN`; never read by the app (`app/api.py` only loads
  `.env`); regenerates automatically when the Vercel CLI needs it.
- **`data/raw/`** — 75 accumulated JSON snapshots from past sessions' live
  AAPL smoke tests. `FileRawSink.__call__` does
  `directory.mkdir(parents=True, exist_ok=True)` before every write, so the
  whole tree is recreated automatically the next time a real fetch runs;
  nothing depended on the accumulated history.

**Deliberately left alone** (weighed and rejected, not overlooked):
`.mypy_cache/` (14MB — real incremental-analysis speed value, regenerates
either way), `.pytest_cache/`, `.ruff_cache/`, `.hypothesis/`, `.coverage` —
routine regenerable tool caches that reappear on the next run regardless, so
deleting them has no lasting benefit. `dcf_valuation_api.egg-info/` — small
editable-install byproduct for the active `.venv313`; removing it has no
upside and could confuse `pip show`/metadata lookups until a reinstall.
`.claude/` and `.vercel/` — the Claude Code harness's own local directory
and the Vercel CLI's project-link metadata (actively used), both out of
scope for an app-repo cleanup.

Verified after cleanup: full suite (263 passing, 94.93% coverage), ruff,
and mypy all still pass against `.venv313` untouched.

## 2026-07-13 (later still) — Repo cleanup: moved non-README markdown into project-docs/

User asked to organize the repo so root-level `.md` clutter doesn't grow as
more planning docs get added over time. Moved `ARCHITECTURE_DECISIONS.md`,
`FRONT_END_SKILL.md`, `IMPLEMENTATION_PLAN.md`, `PROGRESS.md` (this file),
and `issues.MD` into a new `project-docs/` folder. Chose that name (over
`docs/`) specifically to avoid colliding with the existing `docs/` folder,
which holds the customer-facing `index.html` — a completely different
audience/purpose.

**Deliberately NOT moved:** `CLAUDE.md` stays at the repo root — Claude
Code auto-loads project instructions from that specific root path, and
relocating it would silently break that (a functional regression, not just
cosmetic). `README.md` stays at the root per the user's explicit request
and standard convention (GitHub renders it on the repo homepage only from
the root).

Updated every cross-reference to the moved files: `CLAUDE.md`'s session-workflow
instructions (now point at `project-docs/PROGRESS.md`), two code comments
(`app/accounts.py`, `app/http_cache.py`) that mentioned `IMPLEMENTATION_PLAN.md`
by bare name, and `README.md`'s two mentions. Added a "Repo layout note" to
`CLAUDE.md` and a `project-docs/` bullet to README's contributor notes so the
convention is documented in both places future sessions read. Updated the
`.gitignore` comment for clarity (no rule change needed — the existing
`*.md`/`*.MD` glob already applies repo-wide regardless of subdirectory, and
`git check-ignore -v` confirmed all five moved files are still ignored, same
as before the move). Verified full suite (263 passing, 94.93% coverage),
ruff, and mypy all still pass after the two comment edits.


## 2026-07-13 (later still) — Picked up IMPLEMENTATION_PLAN.md: Phase 7 quota-race fix + Phase 6 key rename

Resumed general plan implementation (user request, after the sign-in and
account-button bugs above were fixed). Asked the user two open architecture
questions before proceeding further and got answers:
1. **Keep valuation responses `Cache-Control: public` in production** (no
   route split) — accepted as a deliberate, already-documented consequence of
   the cacheable-GET design, not revisited.
2. **Don't start Phase 8 (Postgres/Redis) yet** — "keep hardening what
   exists" instead. Picked well-specified, non-infra, non-decision items from
   there.

Work this session:
- **Phase 7 hardening (was flagged pre-production-mandatory):** the Phase B
  quota-consume result was fetched but never checked, so a caller whose
  atomic consume was correctly refused (a race where the pre-flight peek saw
  a stale "under limit" view) could still receive the already-computed 200.
  Fixed in `app/api.py` — a rejected consume now replaces the response with
  the same 429 the pre-flight gate returns; factored into a shared
  `_over_quota_response()` helper. New regression test
  `test_stale_peek_does_not_let_an_over_limit_consume_serve_a_200` (wraps a
  real `DailyRequestLimiter`, forces `peek()` to lie "allowed", proves the
  real atomic consume still blocks it — fails before the fix, passes after).
- **Found live via curl against production:** pre-flight 401/403/503
  responses carried no `Cache-Control` at all, so Vercel's edge injected its
  own `public, max-age=0, must-revalidate` default — contradicting the
  documented "valuation-path errors are no-store" claim. Fixed: both shared
  response builders (`_auth_error_response`, `_storage_error_response`) now
  set `no-store` unconditionally (they're shared by the valuation pre-flight
  gate and the account/login routes, so this also covers CSRF/session
  failures). Committed as `045d202`.
- **Phase 6: self-service key rename** (the one remaining item from the
  original self-service-keys task, "label is currently write-once"). New
  `POST /v1/account/keys/{id}/rename` — changes only the label (nullable, 64
  chars), never touches the secret/scope/quota; same generic 404 for
  cross-customer/revoked-key attempts as revoke/rotate.
  `SupabaseClient.rename_customer_key()` (PATCH scoped to
  `id`+`customer_id`+`revoked=eq.false`); `accounts.py::rename_key()` records
  a new `account.key_renamed` audit event. `docs/index.html` gets a "Rename"
  button (a `window.prompt()` for the new label — matches the page's
  no-framework vanilla-JS style). No test-fake changes needed — the existing
  generic PATCH handler in `tests/fake_supabase.py` already matched the
  filter shape rotate already used.
- Suite: 253 → **263 passing** (252 at session start), coverage 94.93%
  (floor 93%); ruff/format/mypy clean throughout.
- **Not started this session (explicitly deferred by user choice):** Phase 8
  (Postgres/Redis for distributed state across serverless instances) — next
  major phase, needs infra/provider decisions (e.g. Redis: Upstash) before
  implementation begins.

## 2026-07-13 (later) — Fixed unresponsive account buttons (csrfHeaders scope bug)

- **User-reported (after sign-in was fixed and confirmed working):** on
  production, "Generate key", "Revoke", and "Sign out" did nothing at all —
  no error, no visible effect. Root cause: `cookieValue()`/`csrfHeaders()`
  were defined in `docs/index.html`'s endpoint-builder `<script>` block, but
  every consumer (`createKey`, `revokeKey`, `rotateKey`, `signOut`,
  `sendEmailLogin`) lives in the separate account-block IIFE — a different
  function scope — so each click threw
  `ReferenceError: csrfHeaders is not defined` inside an async handler
  (unhandled rejection, silent no-op). Introduced with the CSRF wiring in
  the "Phase 7 push" bundle; invisible to pytest (tests send `X-CSRF-Token`
  directly, never execute page JS) and to the pre-CSRF live browser tests.
  Server-side CSRF was verified healthy on production via curl first
  (matching token → 200, wrong token → 403), isolating the bug to the page.
- **Fix (commit `162f002`, deployed + verified live):** moved the two
  helpers into the account block; also made `revokeKey`/`signOut` surface an
  error banner on a non-OK response instead of failing silently. Both script
  blocks syntax-checked via Node; 252 tests still pass.
- **Lesson recorded:** `docs/index.html` has TWO separate IIFE script blocks
  (builder ~1226+, account ~1499+); helpers must live in the block that uses
  them — nothing crosses scopes.
- **Still pending user re-test:** create/revoke/rotate/sign-out buttons in a
  real browser session on production (hard-refresh first), plus the earlier
  outstanding items in `issues.MD`.

## 2026-07-13 — Production sign-in root cause found (wrong PUBLIC_BASE_URL domain); plan-vs-code audit

- **User-reported bug: signing in from the production URL only works while a
  local server is running. Root cause found and verified live:** the real
  production deployment is `https://pub-tools-dcf-nu.vercel.app` (Vercel
  project `pub-tools-dcf`; the `-nu` suffix was auto-added because the bare
  name was taken), but Vercel's `PUBLIC_BASE_URL` is set to
  `https://pubtools-dcf.vercel.app` — a domain that serves **no deployment**
  (`DEPLOYMENT_NOT_FOUND`). The OAuth/magic-link `redirect_to` is built from
  `PUBLIC_BASE_URL`, so Supabase sends post-auth browsers to the dead domain
  (or falls back to its Site URL — localhost — which is why sign-in only
  completed when local uvicorn was up). Production `/health` is 200 and the
  login 302's `redirect_to` param proves the bad value.
- **Fixed this session:** `docs/index.html` placeholder base URL
  (`api.example.com` → real production domain, plus removed a leftover
  half-deleted HTML comment); IMPLEMENTATION_PLAN.md Phase 6 deployment
  notes corrected to the real domain; confirmed via `vercel env ls` that
  `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`/`FMP_API_KEY`/
  `API_KEY_HASH_PEPPER`/`PUBLIC_BASE_URL` all exist in Production (Preview
  has only `FMP_API_KEY` + pepper).
- **Fixed with user approval (same session):** Production `PUBLIC_BASE_URL`
  replaced with the real domain, docs fix committed (`ba3b93b`) and pushed
  (git integration redeployed production), and the live login redirect
  re-verified — `redirect_to` now targets
  `https://pub-tools-dcf-nu.vercel.app/v1/auth/callback`. **Still needed
  from the user:** update Supabase's redirect allowlist (+ review Site URL)
  to the real domain, then test both sign-in methods from production with
  the local server stopped — steps in `issues.MD` (new file; also carries
  the session's full issue list).
- **Plan-vs-code audit (same session, user-requested):** every `[x]` plan
  item checked is genuinely implemented; gaps run the other way. The
  "Phase 7 push" commit also shipped CSRF enforcement (`pt_csrf` +
  `X-CSRF-Token`) and peppered API-key hashing (`hmac-sha256-v1`, Phase 13
  item) that no PROGRESS entry recorded (backfilled by this note). Actual
  suite: **252 passing, 95.17% coverage** (not the 247/94.66% recorded),
  ruff + mypy clean. Remaining code-level findings (in `issues.MD`): Phase B
  quota-consume result ignored (plan already flags the 429-on-`allowed=false`
  fix as pre-production-mandatory); middleware pre-flight 401/403/503 lack
  `Cache-Control: no-store`; login rate limiter is instance-local on Vercel.
  A suspected `HEAD`-method auth bypass was tested and disproven (FastAPI
  returns 405).
- **Next step:** user applies the `PUBLIC_BASE_URL` + Supabase URL-config
  fixes, then commit/push (ships the docs fix and redeploys), then re-test
  both sign-in methods from production with the local server stopped.

## 2026-07-13 — Phase 7 implemented: HTTP caching (ETag / conditional GET / public cache)

Implemented Phase 7 per the fully-specified plan (all four design decisions
from the prior planning session).

- **New `app/http_cache.py`** — pure, I/O-free: `compute_etag()` (SHA-256 over
  the response content, excluding only `request_id`/`computed_at`) and
  `if_none_match_satisfied()` (handles `*`, weak `W/` prefix, comma lists).
- **Two-phase quota split** — the middleware now does auth + a **non-consuming
  peek** pre-flight (429 if already over limit, before any FMP fetch/compute),
  and moves the actual **consume + usage-record to after the response is
  built**, skipped entirely for a 304. Added `peek()` to both
  `DailyRequestLimiter` and `SupabaseDailyQuotaLimiter`. **No new RPC/migration
  needed** — the Supabase peek reuses the read-only `get_daily_quota_usage()`
  built in Phase 6.
- **Valuation route** now computes the ETag, returns a bodyless `304` on an
  `If-None-Match` match (flagging the middleware to skip consume/usage), and
  sets `ETag` + `Cache-Control: public, max-age=30, s-maxage=60,
  stale-while-revalidate=30` + `Vary: Accept-Encoding` on 200/304.
- **`X-RateLimit-*` headers removed from valuation 200/304 responses** (kept on
  429) so a shared cache can never serve one customer's quota state to another.
  Valuation-path errors and 429s are `Cache-Control: no-store`.
- **Resolved a latent ambiguity during implementation:** only a **304** is
  free; errors (4xx/5xx) still consume quota, preserving the deliberate
  Phase-5 "invalid requests count" behavior. Fail-closed preserved (quota-store
  failure at peek OR consume → 503, never an unmetered valuation).
- **Docs:** new "Caching" subsection in `docs/index.html` explaining free 304s,
  the public-cache/auth-bypass-on-hit tradeoff, and the removed quota headers.
- **Tests:** new `tests/test_http_cache.py` (unit) + 7 route-level tests
  (ETag equivalence across param-order/default/scalar-vs-expanded forms; free
  304 with no quota/usage; over-quota 429 proven to make zero FMP calls;
  cache-header presence/absence). Updated 6 existing tests that asserted
  `X-RateLimit-*` on 200s, and the Supabase call-order test (now includes the
  peek GET). Suite: 229 → **247 passing, 94.66% coverage**; ruff + mypy clean.
- **Live-verified against a real uvicorn server** (not just TestClient): 200
  carries the ETag + public Cache-Control + Vary and no `X-RateLimit`; a
  conditional GET returns `304 Not Modified` with matching ETag; a 422 error is
  `no-store`.
- **gitignore:** reviewed; nothing new to add — Phase 7 introduces no generated
  or temporary files (the new `.md` skill file is already covered by the `*.md`
  rule).
- **Still open (not blocking the code):** the one exit criterion that needs a
  deployed Vercel preview — confirming the edge network actually caches and
  honors `s-maxage`, and whether a separate `CDN-Cache-Control` header is
  required. Blocked on the same Vercel env-var setup outstanding since
  Phase 5/6.

## 2026-07-12 (night) — Planning only: Phase 7 fully specified, no ambiguity left

User asked to pause Phase 6 and break down Phase 7 (HTTP caching/canonical
requests) in full detail, with clarifying questions asked *before* any
implementation. Phase 7 as originally written (8 terse bullets) predates
Phase 5/6's auth — it didn't address how caching should interact with
`X-API-Key`/quota at all. Asked and got answers on the four real forks:

1. **Cache audience: public/shared** (not private/client-only). Explicit,
   load-bearing consequence written into the plan: a CDN/edge cache hit never
   reaches the origin, so it never runs auth — once a canonical URL is
   cached, anyone hitting that exact URL (no key required) can get it until
   it expires. Accepted as reasonable since valuations are a deterministic
   function of public data + caller assumptions, not per-customer-secret.
2. **304s are free** (don't consume quota / don't write a usage event) — this
   is the most architecturally significant answer: it requires splitting the
   current single-step "check-and-consume" quota RPC into a pre-flight
   non-consuming *peek* (still rejects already-over-limit callers with 429
   before any compute) and a post-computation *consume* (only on a genuine
   fresh 200). Documented the known accepted race this introduces
   (peek-then-consume has a small window under concurrent bursts) rather
   than glossing over it.
3. **Canonical URLs normalize internally, no redirect** — accepted the
   consequence that a CDN will cache non-canonical variants as separate
   entries despite being semantically identical, since it only sees the raw
   URL; our own ETag logic still works, just doesn't merge across variants
   at the CDN layer.
4. **Phase 7 scope is HTTP semantics only** — no new caching store this
   phase (that's Phase 8/Redis); every request still runs the full
   fetch+compute pipeline, this phase only adds correct headers/ETag/
   conditional-request handling around it.
- Also specified precisely (not left to interpretation later): ETag = SHA-256
  over the full response with only `request_id`/`computed_at` excluded (so it
  changes automatically on any real content change — quote, restated
  financials, model bump — giving invalidation "for free," directly
  satisfying the plan's invalidation bullet with no separate purge
  mechanism); `Cache-Control: public, max-age=30, s-maxage=60,
  stale-while-revalidate=30` as a starting point; `Vary: Accept-Encoding`
  only (explicitly not by API key, which would defeat sharing); and a new
  requirement to **remove `X-RateLimit-*` headers from valuation responses
  entirely**, since baking one customer's quota state into a response a
  shared cache might later serve to a different caller is a real (if minor)
  data leak.
- IMPLEMENTATION_PLAN.md's Phase 7 rewritten in full with these decisions,
  concrete tasks, and testable exit criteria. No code changed this session —
  planning only, per user's explicit request to ask before implementing.
- **Next up:** implementation, starting with the peek/consume quota RPC
  split (the piece most likely to need a new migration) and the ETag/
  canonical-resolver logic, since those are the load-bearing pieces
  everything else in this phase depends on.

## 2026-07-12 (evening) — Added self-service key rotation

User asked whether anything was blocking Phase 6 completion; answer was: two
code-only items (key rotation, key rename) I could build unprompted, plus
several items needing the user's own credentials/decisions (CAPTCHA provider,
Vercel env vars, custom SMTP, cross-provider linking, email-verification
scope call). User chose key rotation.

- Added `POST /v1/account/keys/{id}/rotate`: regenerates a key's secret in
  place (same id/prefix/label/`created_at`), immediately invalidating the old
  secret. Rejects already-revoked keys and cross-customer attempts with the
  same generic 404 `revoke`/`create` already use (no leak of *why* it was
  rejected). New `SupabaseClient.rotate_customer_key()` (PATCH filtered by
  `id` + `customer_id` + `revoked=eq.false`).
- `docs/index.html` gets a "Rotate" button next to "Revoke" on each active
  key; shows the new secret once via a shared `showNewKey()` helper (factored
  out of `createKey()`).
- New audit event `account.key_rotated`.
- **Writing the rotation test surfaced two latent gaps in the shared test
  fake** (`tests/fake_supabase.py`), not app code: its `GET /rest/v1/api_keys`
  handler only ever matched on `customer_id`, so it silently broke the
  machine-auth prefix-based lookup the moment a test actually tried to use a
  self-service key for a real `/v1/valuations/*` call (nothing had exercised
  that combination before). It also had no handler at all for the
  `consume_daily_quota`/`record_usage_event` RPCs, for the same reason. Both
  fixed — this fake will now correctly support any future test that drives a
  self-service key through an actual valuation request.
- Verified the real PATCH query (id/prefix preserved, secret_hash changed)
  directly against the live Supabase schema, then cleaned up the test
  customer/key from the project afterward. Full end-to-end rotation
  (through a real browser session) wasn't tested live — that needs a real
  login, which requires the user in the loop.
- 8 new tests (221 → 229), coverage 94.49%. Ruff/mypy clean.

## 2026-07-12 (later) — Fixed two account-UI issues found in live testing

User confirmed both GitHub and email magic-link sign-in work end to end live,
and reported two issues while using the "Your account" page:

- **Self-service key quota looked frozen.** Traced the code first: quota
  *enforcement* was confirmed unaffected (self-service keys share the exact
  `api_keys.daily_quota` column and middleware path as admin-issued keys, no
  special-casing) -- this was purely a missing display feature. The key list
  only ever showed the static configured limit, never how much had actually
  been used that day. Fixed: `SupabaseClient.get_daily_quota_usage()` (new
  read-only lookup against `daily_quota_counters`), `list_keys()` enriches
  each active key with `requests_used_today` (revoked keys skip the lookup),
  `create_key()` returns `0` for a brand-new key without an extra query.
  Verified the new query against the real Supabase project's schema directly
  (not just mocks) before trusting it.
- **`last_used_at` was a raw ISO timestamp.** Added a `timeAgo()` helper in
  `docs/index.html` ("3 hours ago", falling back to a localized date after 30
  days).
- 7 new tests (168 → 221 total), coverage 94.75%. Ruff/mypy clean.

## 2026-07-12 — Fixed live GitHub sign-in bug (bad_oauth_state); added email magic-link login

- **Bug found via live testing:** user hit `bad_oauth_state` on the real
  Supabase project when trying to sign in with GitHub. Root cause: the
  `state` parameter we sent to Supabase's `/authorize` endpoint for our own
  CSRF protection is actually reserved and managed internally by Supabase
  Auth to correlate its own round trip with the provider -- our override
  broke its callback validation. Confirmed via web research (Supabase's own
  error-code docs plus a matching `supabase/auth` GitHub issue reproducing
  the identical symptom with a different provider) and by directly curling
  the live authorize/redirect chain before and after the fix (before: our
  arbitrary `state=test123` was echoed to GitHub verbatim; after: Supabase
  generates its own UUID-shaped state). Fixed by removing `state` entirely
  from `SupabaseAuthClient.authorize_url()` and dropping the now-broken
  `pt_oauth_state` cookie/comparison in `app/accounts.py`. Not a security
  regression -- PKCE's code_verifier/code_challenge binding already provides
  the CSRF protection `state` was redundantly trying to add.
- **Added email magic-link login** (`POST /v1/auth/email/login`) as a second
  sign-in method alongside GitHub, per user's request. Chose passwordless
  magic link over email+password specifically to avoid building password
  storage/reset/verification (Supabase's `/auth/v1/otp` + PKCE handles it).
  Both providers complete through the same `/v1/auth/callback` code-exchange
  step (`app/accounts.py::complete_login`, renamed from
  `complete_github_login` since it's now provider-agnostic) -- GitHub's
  authorize redirect and Supabase's magic-link verify both land on
  `?code=...` the same way, so no new redirect-URL configuration was needed.
  Audit events now record which provider was used
  (`user.app_metadata.provider`, read back from Supabase after the exchange).
- Extended `tests/fake_supabase.py` with an `/auth/v1/otp` handler and a
  `provider` field on registered login codes/users.
- Known new gap: a customer who signs in with both GitHub and email under
  the same real identity gets two separate accounts (no cross-provider
  identity linking in Supabase). Documented in `IMPLEMENTATION_PLAN.md`
  Phase 6, not fixed this session.
- 14 new tests; suite grew from 200 to 214 passing, coverage 94.67% (93%
  floor). Ruff/mypy clean.
- **Still required from project owner:** configure custom SMTP in Supabase
  before relying on email login for real customer volume -- the default
  built-in email sending is rate-limited and meant for dev/testing.

## 2026-07-11 (later night) — Phase 6 implemented: GitHub sign-in + self-service API keys

- Implemented customer login via Supabase Auth's GitHub OAuth provider,
  server-mediated PKCE (no client-side Supabase JS, keeping the runtime
  dependency footprint and the docs page's strict CSP unchanged), with
  `HttpOnly`/`SameSite=Lax` session cookies distinct from `X-API-Key` machine
  auth. New: `app/accounts.py` (PKCE, cookies, session/refresh logic,
  self-service key business logic), `SupabaseAuthClient`/`AuthSession` plus
  customer/key/audit-event methods in `app/supabase.py`, new routes in
  `app/api.py` (`/v1/auth/github/login`, `/v1/auth/callback`, `/v1/auth/me`,
  `/v1/auth/logout`, `/v1/account/keys` GET/POST,
  `/v1/account/keys/{id}/revoke`), migration
  `supabase/migrations/002_phase6_customer_login.sql` (`auth_user_id` on
  `api_customers`, `label` on `api_keys`), and a "Your account" section in
  `docs/index.html` (sign-in link, key list/create/revoke, no external JS).
- Self-service keys are fixed to `valuation:read` scope and a 100/day quota,
  capped at 5 active keys per customer; the admin script/CLI path
  (`scripts/create_api_key.py`) stays as the operator/support fallback for
  custom quota or scope.
- Security decisions: FastAPI-mediated access chosen over direct-Supabase +
  RLS (matches the existing `api_keys`/`usage_events` no-policy/service-role
  posture); ownership enforced in application code and verified by
  `test_self_service_keys_are_isolated_between_customers`.
  `SameSite=Lax` cookies cover CSRF on the self-service POST endpoints without
  a separate token (Lax cookies aren't sent on cross-site POST).
- Known gaps versus the original Phase 6 plan (see `IMPLEMENTATION_PLAN.md`
  for the itemized status): no email-verification gate before first key
  creation, no CAPTCHA on signup, no in-place key rotation/rename (revoke +
  create covers it today), no password-reset flow (not applicable — GitHub
  OAuth is the only credential, we never store one).
- 32 new tests (`tests/test_accounts.py` new; additions to
  `tests/test_supabase.py` and `tests/test_api.py`), including a shared
  in-memory fake Supabase Auth+REST backend (`tests/fake_supabase.py`).
  Suite grew from 168 to 200 passing tests, coverage 94.56% (93% floor).
  `tests/conftest.py`'s env-isolation fixture extended to also clear
  `PUBLIC_BASE_URL`.
- Test-harness note for future sessions: simulating two independent logged-in
  browser sessions against one app requires **two separate `TestClient`
  instances** sharing the same `app` object (each gets its own cookie jar).
  Manually clearing/restoring `TestClient.cookies` via `.update()`/`.set()`
  does not reliably interoperate with cookie deletion (`Set-Cookie` with
  `Max-Age=0`) in this httpx/Starlette version — a real Set-Cookie response is
  needed for later deletion to match. Cost real debugging time; avoid
  re-deriving this the hard way in a future session.
- Still required before this phase is genuinely live: apply migration 002 to
  the production Supabase project; create a GitHub OAuth App and configure
  the Supabase GitHub provider + redirect-URL allowlist (README has exact
  steps); set `PUBLIC_BASE_URL` per environment.

## 2026-07-11 (night) — Planning only: new Phase 14, decouple UI/UX from the microservice

- Added `IMPLEMENTATION_PLAN.md` **Phase 14 — Separate UI/UX from the
  microservice**, at the end of the plan (after Phase 13). Goal: the DCF API
  becomes a pure headless JSON service with no bundled UI; `docs/index.html`
  (currently served directly from `GET /` in `app/api.py`) moves to a
  separately deployable frontend that talks to the API, and every future
  pubTools product, purely over HTTP. Covers: CORS allowlist for the new
  frontend origin, what replaces `GET /` once the UI moves out, where Phase 6
  login/account portal lives (frontend, not this repo), and compatibility for
  existing bookmarked docs links. Added a 5th delivery milestone,
  "Platform decoupling (pubTools multi-product readiness): Phase 14." No
  application code changed — planning only, per user's request.
- **Next up:** same open decisions as Phase 6 (identity provider,
  RLS-vs-FastAPI), plus Phase 14's own: target frontend hosting/repo
  structure and domain shape, before either phase moves into implementation.

## 2026-07-11 (evening) — Planning only: new Phase 6, customer login + self-service keys

- User's product vision: **pubTools** is a multi-tool financial-calculation
  platform; this DCF valuation API is only its first product. Account/API-key
  infrastructure must be designed pubTools-wide, not DCF-specific — recorded
  in memory (`project_pubtools_vision`) so future sessions default to that
  framing.
- Added `IMPLEMENTATION_PLAN.md` **Phase 6 — Customer accounts, login, and
  self-service API keys**, between Phase 5 (auth/quotas/metering, done) and
  the old Phase 6 (HTTP caching, renumbered to Phase 7). All later phases
  shifted by one (old 6–12 → 7–13); delivery-milestones ranges and the one
  code cross-reference (`app/rate_limit.py`'s "Phase 5/7" comment) updated to
  match. No application code changed — planning only, per user's request.
- Phase 6 covers: identity-provider choice (Supabase Auth recommended), a
  browser session mechanism kept separate from machine API-key auth,
  email-verified signup, login/password-reset with rate limiting and no
  account-existence leakage, self-service key list/create/rotate/revoke
  endpoints, linking `api_customers` to an auth user, an RLS-vs-FastAPI
  decision (pick one, don't mix), and keeping the existing admin CLI scripts
  as an operator/support fallback rather than removing them.
- **Next up:** user to decide identity-provider and RLS-vs-FastAPI questions
  raised in Phase 6 before any implementation starts.

## 2026-07-11 (later) — Supabase live-verified; critical quota-RPC bug fixed

- User created a real Supabase project, ran `supabase/migrations/001_phase5_auth_usage.sql`,
  and set `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` in `.env`. Live-verified end
  to end against real Supabase + real FMP: 401 for missing/bad key, a real 200
  valuation for AAPL with a valid key, and quota headers/enforcement.
- **Critical bug found and fixed via live testing (would have failed 100% of
  authenticated production requests):** `SupabaseClient.consume_daily_quota()`
  in `app/supabase.py` expected the `consume_daily_quota` RPC to return a JSON
  object, but PostgREST always returns `returns table (...)` RPC results as a
  JSON array of rows (even for one row). Every real quota check raised
  `SupabaseError` → 503 `auth_storage_unavailable`. The mocked test in
  `tests/test_api.py` masked this because it stubbed the RPC response as a
  bare dict, which doesn't match real PostgREST behavior. Fixed the client to
  unwrap `rows[0]`; fixed the test's mock shape; added
  `tests/test_supabase.py::test_supabase_quota_parses_real_postgrest_table_rpc_shape`
  and an empty-array regression test so this can't silently regress again.
- **Security hardening (from a security review of this branch):** the
  migration's `consume_daily_quota`/`record_usage_event` SQL functions are
  `security definer` but Postgres grants `EXECUTE` to `PUBLIC` by default, and
  PostgREST auto-exposes every public-schema function as an RPC endpoint. Added
  explicit `revoke ... from public` / `grant ... to service_role` to the
  migration so the project's `anon`/`authenticated` keys can't call these
  directly and bypass app-level API-key auth. Re-run the updated migration
  block if it was applied before this fix landed.
- `scripts/create_api_key.py` and `scripts/report_usage.py` didn't load `.env`
  (only `app/api.py` did), so they failed with `SUPABASE_URL is required` even
  with credentials present locally. Both now load `.env` the same way `app/api.py`
  does.
- `tests/conftest.py` didn't isolate tests from ambient `SUPABASE_URL`/
  `SUPABASE_SERVICE_ROLE_KEY` env vars — once real credentials existed in local
  `.env`, ~40 tests that assume "Supabase not configured" broke. Added an
  autouse fixture that deletes both vars for every test.
- Test customers/keys/usage-event rows created during live verification were
  deleted from Supabase afterward; DB is clean. 168 tests passing, 94.5% coverage.
- **Next up:** flip `docs/index.html`'s "X-API-Key: preview, not yet enforced"
  wording now that auth is genuinely live; add the Supabase env vars to Vercel
  production/preview and redeploy; re-verify against the deployed URL before
  telling customers auth is enforced.

## 2026-07-11 - Phase 5 Supabase auth, quotas, and metering

- Added Supabase-backed API-key auth, daily quota RPC integration, and usage
  event recording without adding a new runtime dependency; the app uses `httpx`
  against Supabase REST/RPC.
- Production behavior: if `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set,
  `/v1/valuations/*` requires `X-API-Key`, uses hashed key records, consumes an
  atomic per-key daily quota, and records usage. Public `/`, `/health`, docs,
  and OpenAPI remain public.
- Added `supabase/migrations/001_phase5_auth_usage.sql`, plus admin helpers:
  `scripts/create_api_key.py` and `scripts/report_usage.py`.
- Website UI now accepts an optional API key for the current valuation request
  only; it does not persist the key.
- Still required from project owner before production activation: run the
  Supabase migration and add `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` to
  Vercel server-side environment variables.

---

Session-to-session state for Claude. **Read this at the start of a session;
update it at the end of any session that changes the project.** Newest entry
first. Keep entries short — what changed, key decisions, what's next. Detailed
specs live in CLAUDE.md; this file is only the running state.

## Current state (TL;DR)

- **Done:** pure DCF engine and sensitivity grid, FMP client/normalization,
  FastAPI route/error mapping, customer docs, real-key/live endpoint
  verification, and **325 passing tests (93.58% coverage, 93% floor)**;
  ruff/format/mypy clean (`app` + `scripts`). Supabase auth, quotas, and usage
  metering are live-verified end to end. CSRF enforcement
  (`pt_csrf`/`X-CSRF-Token`) and peppered API-key hashing are implemented.
  **Phase 6** (GitHub OAuth + email magic-link sign-in, self-service key
  list/create/rotate/revoke/rename) is functionally complete and confirmed
  live, except explicitly deferred email-verification/CAPTCHA items.
  **Phase 9 public site is committed and live on `ashaat.dev`** (portfolio at
  `/`, `/apis` directory, DCF at `/dcf`). **ADR-008 is implemented
  (uncommitted, model 0.2.0):** the market price is fetched **live from
  Finnhub on every request and cached nowhere**; `/v1/valuations/*` is
  `Cache-Control: no-store`; `BaseFinancials`/`Valuation` are price-free by
  construction; upside is computed at the API layer from the live quote; any
  Finnhub failure degrades to null price + warning. The Phase 7 ETag/304/
  public-cache handling and the Phase 8 Slice B response cache are **retired**
  for this endpoint, and the quota flow is back to one atomic
  `check_and_increment` with `X-RateLimit-*` on all valuation responses.
  **What remains of Phase 8 Slices A+B:** the statement cache — L1 +
  Redis `fund:`/`profile:`/`neg:` + distributed single-flight (N
  differently-assumed valuations of one ticker cost one FMP statement fetch)
  — and the cross-instance Redis login limiter. Migration 002 applied; all
  needed env vars (incl. `FINNHUB_API_KEY`) are in Vercel Production.
  `project-docs/REQUEST_FLOW.md` predates ADR-008 on the price/response-cache
  path — trust `issues.MD`/ADRs where they disagree.
- **In progress:** **Phase 8 Slice C — parts 1 and 2 are implemented
  (uncommitted):** migration 003 is complete (immutable
  `normalized_snapshots`, mutable `ticker_snapshot_heads`, run/claim ledger,
  `store_ticker_snapshot` + `begin/finish_financial_refresh_run` RPCs; NOT
  yet applied — must be applied before this code deploys), the DB
  read-through is live in code (L1 → Redis → DB → FMP cold-only; store errors
  fail closed via `SnapshotStoreError`/503; bootstrap awaits the DB write
  before Redis/return; existing DB tickers never trigger request-time FMP),
  and the daily 6 PM Eastern refresh exists end to end
  (`app/refresh.py` runner, `GET /internal/cron/refresh-financials` with
  `CRON_SECRET`, `vercel.json` crons at 22+23 UTC with the
  `America/New_York` guard, durable claims + reconciliation). **Part 3
  remains:** the 6 PM L1 hard-expiry boundary, structured freshness response
  fields (today freshness travels only in `data_quality_warnings`), and live
  verification. Per ADR-008 Slice C must NOT reintroduce a `quote:` cache,
  response cache, or response-cache generation rotation; the daily cycle
  refreshes statements/profile only. Live wiring blocked on TODO §4 (env
  pull, `CRON_SECRET` in Vercel, migration apply, FMP capacity). Preview env
  still has no Supabase vars (auth off in previews); custom SMTP still
  recommended before real email-login volume.
- **Not started:** external object storage, advanced DCF drivers, and
  Phase 15 (on hold — separate frontend split, superseded in practice by
  Phase 9).
- **Run tests:** `./.venv313/Scripts/python -m pytest -q` (current Windows/OneDrive
  recovery environment; standard clean setups may use `.venv`).
- **Run server:** `./.venv313/Scripts/uvicorn app.api:app --reload` (needs
  FMP_API_KEY); landing/API UI at /, docs at /docs, health at /health.
- **Improvement roadmap:** `IMPLEMENTATION_PLAN.md` is the source of truth for
  phased hardening and model work. Mark tasks done only with implementation
  and verification evidence.

---

## 2026-07-11 — Phase 3 complete: latest compatible filings and fresh quotes

- Provider now fetches up to eight annual candidates per statement endpoint.
  Normalization joins income/balance/cash-flow by FY and exact statement date,
  validates fiscal year/currency, selects the newest complete set, and prefers
  the latest accepted/filing record for restatements.
- Newer incomplete periods are never mixed into an older complete set. Missing
  intersections, fiscal conflicts, currency conflicts, and invalid dates return
  controlled normalization failures.
- Added fiscal/filing/selection provenance and warnings for derived fiscal year,
  profile-currency fallback, restatement choice, incomplete newer periods, and
  bounded stale-quote use.
- Statements (4h), profile (24h), negative outcomes (4h), and quotes (60s) have
  independent configurable caches. Quote refresh failure can use cached price for
  at most 15 minutes, then fails safely.
- Verification: Ruff, format, mypy, 136 tests, and 95.80% coverage pass.

---

## 2026-07-11 — Numeric safety and API contract hardening

- Completed Phase 1: finite-number validation, explicit v1 assumption bounds,
  provider/base validation, final-result integrity checks, and unclipped negative
  valuation warnings. OpenAPI, README, and customer builder rules were updated.
- Phase 2 in progress: additive version-1 error envelope with request IDs and
  stable codes is implemented while preserving legacy `detail`; native FastAPI
  validation and current 404/422/500/502/503 responses are typed and tested.
- Projection responses now expose the complete FCF bridge (growth, margin, taxes,
  NOPAT, D&A, capex, NWC, discount period/factor, FCF, and PV).
- Verification: Ruff, format, mypy, and 108 tests pass; coverage is 95.27%.

## 2026-07-11 — Phase 2 complete: stable contracts and auditability

- Completed the additive version-1 error envelope while preserving legacy
  `detail`; every emitted failure category has stable code/request-ID tests and
  all documented statuses have typed OpenAPI schemas/examples.
- Added success provenance: request/computation IDs, currency/raw units,
  provider/model/data versions, fundamentals/quote dates, warnings, disclaimer,
  and deterministic normalized-snapshot SHA-256 fingerprint.
- Annual projections expose the complete FCF bridge. End-to-end tests reconstruct
  FCF, PV, enterprise value, equity value, and per-share value from response data.
- Added a canonical OpenAPI hash snapshot and documented the float/no-intermediate-
  rounding policy plus new response/error contracts.
- Verification: Ruff, format, mypy, 115 tests, and 96.07% coverage pass.

---

## 2026-07-11 — Vercel compatibility and Markdown ignore policy

- Confirmed the supported custom Vercel FastAPI entrypoint remains
  `app.api:app` through `[tool.vercel]`.
- Disabled local raw-response filesystem persistence when `VERCEL` is present;
  local development retains the audit sink. Added focused tests.
- Added Vercel-wide release gates to `IMPLEMENTATION_PLAN.md`, covering
  ephemeral instances, external durable state, timeouts, bundles, regions,
  background work, and preview smoke tests.
- `.gitignore` now ignores all `.md`/`.MD` files except `README.md`/`README.MD`.
  Previously tracked Markdown remains tracked unless explicitly removed from the
  Git index.

---

## 2026-07-11 — Comprehensive implementation roadmap

- Added `IMPLEMENTATION_PLAN.md` covering tooling, numeric safety, API contracts,
  data alignment/freshness, concurrency, authentication, HTTP caching,
  Postgres/Redis, audit storage, observability, richer assumptions, terminal
  value, and release readiness.
- Tasks have explicit phase exit criteria and must be marked done in the same
  change that implements and verifies them.
- No API behavior or calculation code changed during this planning session.

---

## 2026-07-11 (late) — Docs rewrite: teach DCF + assumptions + design pass

- Reworked docs/index.html per user's SKILL.md (frontend-design skill).
  Now OPENS by teaching: new sections #what-is-dcf (plain-language: FCF,
  time value, discounting, terminal value, how investors use it),
  #how-built (5 numbered steps — a real sequence), #assumptions (per-input
  guidance cards with typical ranges + common-mistake warnings for every
  parameter). Kept all reference sections + endpoint builder + real numbers.
- Design: serif display face (Palatino/Georgia stack) paired with sans body
  + mono code; kept green identity, added a single amber accent used ONLY
  in the signature element — a hero "discounting diagram" (CSS bar chart:
  faint bar = future cash flow, solid = present value, shrinking with time;
  staggered grow-in, reduced-motion respected). Both themes styled.
- Republished to same artifact URL. SKILL.md is just the design skill the
  user dropped in; not part of the app.

## 2026-07-11 (late night) — Plan-coverage error + negative caching

- New `TickerNotCoveredError` (exceptions.py) for FMP HTTP 402 (symbol
  outside the account's data plan). Distinct from `TickerNotFoundError`;
  both map to HTTP 404 but with different messages. 402 message to the
  customer deliberately does NOT mention "subscription/upgrade" (they can't
  act on our upstream plan). fmp.py: 402 -> NotCovered, 404 -> NotFound;
  neither retried.
- **Negative caching** in FundamentalsService: definitive per-ticker
  rejections (NotFound, NotCovered, UnsupportedSector) are cached for the
  same TTL, so repeat requests for a bad/uncovered/bank ticker cost 0 FMP
  calls instead of 1–5. Transient errors (503/network) are NOT cached
  (stay retryable). Directly addresses the ~250 calls/day FMP free-tier
  budget. `invalidate()` clears both caches.
- Verified: 1 live FMP call (nonexistent ticker) confirmed 402 ->
  TickerNotCoveredError + negative cache serves the repeat with no extra
  call. Everything else covered by mock-transport tests (61 total, 6 new).
- Docs 404 row reworded (existence OR coverage); artifact republished.

**Budget note for future sessions:** FMP free tier ~250 calls/day, and 402
is returned for BOTH nonexistent and out-of-plan symbols (indistinguishable
on free tier). Don't burn calls re-fetching what fixtures/data/raw already
hold. Key is session-only (never stored); ask the user for it when a live
check is truly needed.

## 2026-07-11 (night) — Live verification against real FMP + 402 fix

- User provided FMP API key in-session (free tier; NOT stored in any file
  — must be supplied via FMP_API_KEY env var each time).
- Live smoke tests passed: AAPL (Sep FY), MSFT (Jun FY), WMT (Jan FY2026
  offset fiscal year), NVDA (Jan FY2026). Real numbers normalize cleanly;
  negative delta_nwc (WMT) flows through; JPM correctly 422s on sector.
- Live HTTP server tested end-to-end: 200 with sensitivity grid (center
  cell == point estimate live), 422s for sector + bad assumptions,
  in-process cache confirmed, raw sink writes data/raw/{ticker}/*.json.
- **Bug found & fixed via live testing:** FMP free tier returns HTTP 402
  (Payment Required) for ANY symbol outside plan coverage — including
  nonexistent tickers. Client didn't classify 402 → unhandled
  HTTPStatusError → customer-facing 500. Now 402 maps to
  TickerNotFoundError (404) in `app/providers/fmp.py`; regression test
  added. 55 tests passing.
- README updated: architecture table current, run-the-server section,
  sensitivity grid documented. Docs artifact already current.

## 2026-07-11 (evening) — Sensitivity grid (milestone 5)

- Engine: `compute_sensitivity_grid(base, assumptions)` in
  `app/dcf_engine.py` — pure 3x3 grid, WACC ±1% x terminal growth ±0.5%
  (offsets in `WACC_OFFSETS`/`TERMINAL_GROWTH_OFFSETS`). Gordon-breaking
  cells (g >= wacc, or wacc <= 0) are `None` instead of erroring; center
  cell always equals the point estimate. `SensitivityGrid` dataclass in
  models.py; exported from `app/__init__.py`.
- API: grid is **default-on** (`?sensitivity=false` to opt out) —
  promoted from CLAUDE.md's "optional" per earlier recommendation, since
  WACC/terminal-growth sensitivity is the top documented risk.
- Docs updated with real computed grid numbers + republished to same
  artifact URL. 54 tests passing (7 new: axes, center-cell identity,
  independent recomputation, monotonicity both axes, None cells,
  API default-on/opt-out).
- FMP key still not provided — user offered; needed only for live smoke
  test (`scripts/smoke_fetch.py`). Ask when running live verification.

**Next up:** live verification with real FMP key; then price split from
fundamentals cache, canonical query-param ordering + Cache-Control for
HTTP cacheability, per-key metering. Nothing committed since the data
layer — API layer, README, docs, sensitivity grid all await a push.

## 2026-07-11 (later still) — Customer documentation

- `docs/index.html` — self-contained Alpaca-style API docs (sidebar nav,
  param tables, real example response generated by running the actual
  engine over the AAPL fixture). Includes an interactive **endpoint
  builder**: customer enters assumptions in % form, page validates with
  the same rules as the server and emits a canonical-order URL + curl.
  This implements the "custom UI → custom endpoint" request without
  backend changes (the API is GET-based, so an endpoint IS a URL).
- Published as Claude artifact:
  https://claude.ai/code/artifact/a6e04de4-53a7-40bb-b5a5-568724176bcd
  (redeploy by republishing the same file path).
- Docs document `X-API-Key` as "preview, not yet enforced" — honest about
  current state; metering is still on the roadmap.
- Base URL in docs is the placeholder `https://api.example.com` — swap
  when a real host exists.

## 2026-07-11 (later) — FastAPI layer

- `app/schemas.py` — pydantic wire models + `build_valuation_response()`;
  internal layers keep using the frozen dataclasses, conversion happens at
  the boundary only.
- `app/api.py` — `create_app(fmp_client=None)` factory; lifespan creates
  the real FMP client at startup (so importing the module needs no key)
  and shares one FundamentalsService across requests. Exception handlers
  map domain errors per app/exceptions.py (404/422/500/502/503).
  Validation strategy: pydantic does types/required only; ALL domain rules
  stay in the engine's validator (single source of truth), surfaced as
  `{"detail": [{"field", "message"}]}` 422s. `revenue_growth` arrives as a
  string ("0.05" or "0.08,0.07,...") and is parsed in the route.
- Engine: added missing `wacc > 0` validation rule.
- `tests/test_api.py` — 14 TestClient tests over the fixture transport
  (happy path auditability, resolved-assumption echo, all error codes,
  cache behavior across HTTP requests, /health).
- README.md populated this session too (architecture table, setup, live
  smoke instructions). At that time PROGRESS.md was gitignored by user choice;
  the 2026-07-15 publishing decision at the top of this file supersedes that
  policy.

**Next up:** commit+push API layer & README (user hasn't asked yet);
then sensitivity grid (recommend default-on), price split from
fundamentals cache, canonical query-param ordering for HTTP cacheability.

## 2026-07-11 — Engine, data layer, tests, GitHub setup

**Built (in dependency order):**

- `app/models.py` — frozen dataclasses: `BaseFinancials`, `Assumptions`
  (normalizes scalar-or-list `revenue_growth` in `__post_init__`),
  `YearProjection`, `Valuation`.
- `app/dcf_engine.py` — `compute_dcf(base, assumptions)`, pure/no I/O.
  Raises `DCFValidationError(field, message)` for the API layer to map
  to 422s. Simplifications flagged in its docstring for a later
  correctness pass: D&A/capex/ΔNWC projected as constant % of revenue
  from base-year ratios; end-of-year discounting (no mid-year convention);
  single flat `ebit_margin` across years.
- `app/exceptions.py` — domain errors with intended HTTP mappings in the
  docstring (TickerNotFound→404, UnsupportedSector→422,
  NormalizationError→502, ProviderAuthError→500, ProviderError→503).
- `app/providers/fmp.py` — async httpx client for FMP's **stable** API
  (`/stable`, `symbol=` query param, not legacy v3). Retries 429/5xx with
  exponential backoff, honors Retry-After, never retries auth errors.
  Optional `raw_sink` hook (`FileRawSink` → `data/raw/`, gitignored).
  API key from `FMP_API_KEY` env var; **no key has been tested live yet**.
- `app/normalization.py` — FMP → `BaseFinancials`. Two sign conventions
  handled (do not "fix" these, they are correct): FMP `capitalExpenditure`
  is negative → stored positive via abs(); FMP `changeInWorkingCapital` is
  cash impact → sign-flipped into `delta_nwc`. Sector gate rejects
  financial-sector tickers (`UnsupportedSectorError`).
- `app/fundamentals.py` — `FundamentalsService`: fetch + normalize +
  in-process TTL cache (4h default, injectable `now` for tests).
- `tests/` — 33 tests, all passing: hand-computed spreadsheet cases with
  arithmetic in comments, validation-rule cases, 3 hypothesis
  property tests (equity value monotonic ↓ in WACC, ↑ in terminal growth;
  EV reconciles to ΣPV(FCF)+PV(TV)), and mock-transport data-layer tests
  over fixtures in `tests/fixtures/fmp/` (AAPL = happy path, JPM =
  rejected financial).
- `scripts/smoke_fetch.py` — live end-to-end check once `FMP_API_KEY` is set.

**Decisions made this session (beyond CLAUDE.md):**

- `MODEL_VERSION` lives in `app/__init__.py` (currently "0.1.0").
- Hypothesis property tests can't use function-scoped fixtures — use
  `make_base_financials()` from `tests/conftest.py` directly instead.
- Git identity configured repo-locally (abdshaat / abdshaat@outlook.com).

**Known issues / deferred:**

- Correctness discussion deferred by user: mid-year discounting, SBC
  treatment, reproducibility policy for model_version.
- `current_price` is cached inside `BaseFinancials` with the 4h
  fundamentals TTL — should become a separate short-TTL fetch when the
  API layer is built.
- Sensitivity grid: recommended default-on for v1 (CLAUDE.md lists it
  optional), not yet built.

**Next up:** FastAPI layer — `GET /v1/valuations/{ticker}`, pydantic
models for query params/response, map domain exceptions to HTTP codes,
echo resolved assumptions + `model_version` in response.
