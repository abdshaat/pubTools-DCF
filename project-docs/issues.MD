# Issues — production sign-in investigation (2026-07-13)

Symptom reported: signing in from the production URL only works when the app
is also running locally.

Diagnosis evidence (all verified live on 2026-07-13):

- The real production deployment is healthy at
  `https://pub-tools-dcf-nu.vercel.app` (`/health` returns 200; Vercel
  auto-assigned the `-nu` suffix because `pub-tools-dcf.vercel.app` was
  already taken by another account).
- `https://pubtools-dcf.vercel.app` — the domain recorded in
  IMPLEMENTATION_PLAN.md and stored in Vercel's `PUBLIC_BASE_URL` — serves
  **no deployment at all** (`DEPLOYMENT_NOT_FOUND`).
- Production `GET /v1/auth/github/login` 302s to Supabase with
  `redirect_to=https://pubtools-dcf.vercel.app/v1/auth/callback` — i.e. after
  GitHub/magic-link auth, Supabase sends the browser to the dead domain (or,
  when that URL isn't in Supabase's redirect allowlist, falls back to the
  Site URL — presumably `http://127.0.0.1:8000`, which is exactly why sign-in
  only completes when the local server happens to be running).

## To fix now (this session)

- [x] **Vercel `PUBLIC_BASE_URL` points at the dead domain.** Root cause of
  the reported symptom. Fixed 2026-07-13 (user-approved): Production value
  replaced with `https://pub-tools-dcf-nu.vercel.app` via `vercel env
  rm`/`add`, docs fix committed (`ba3b93b`) and pushed so the git
  integration redeployed production, and the live login redirect re-verified
  — `redirect_to` now points at the real domain. NOTE: sign-in is not fully
  fixed until the Supabase URL-configuration item below is done.
- [x] **`docs/index.html` still uses the placeholder base URL
  `https://api.example.com`** (2 occurrences) although a real production host
  exists. Swapped to `https://pub-tools-dcf-nu.vercel.app`; also removed the
  leftover half-deleted HTML comment fragment from the old "preview, not yet
  enforced" note. Fixed 2026-07-13, commit `ba3b93b`, verified live.
- [x] **IMPLEMENTATION_PLAN.md records the wrong production domain**
  (`pubtools-dcf.vercel.app`). Corrected with an explanatory note, and the
  stale "Supabase env vars in Vercel unconfirmed" bullet updated (confirmed
  present in Production via `vercel env ls`). PROGRESS.md never contained
  the wrong domain. Fixed 2026-07-13.

## Account-page actions unresponsive (reported 2026-07-13, after sign-in fix)

Symptom: signed in with GitHub on production, but "Generate key", "Revoke",
and "Sign out" buttons do nothing at all — no error, no network activity.

- [x] **`csrfHeaders()` is defined in the wrong `<script>` block in
  `docs/index.html`.** Fixed 2026-07-13, commit `162f002` — helpers moved
  into the account block (both blocks syntax-checked, all 6 call sites now
  in scope), deployed and verified live on production HTML. The page has two separate IIFEs in two separate
  `<script>` tags: the endpoint-builder block (lines ~1226–1497) and the
  account block (lines ~1499–1728). `cookieValue()`/`csrfHeaders()` were
  added to the *builder* block (where nothing uses them), while every
  consumer — `createKey`, `revokeKey`, `rotateKey`, `signOut`,
  `sendEmailLogin` — lives in the *account* block, a different function
  scope. Each button click throws `ReferenceError: csrfHeaders is not
  defined` inside an async handler → unhandled promise rejection → silent
  no-op. Introduced with the CSRF work in the "Phase 7 push" bundle; never
  caught because pytest sends `X-CSRF-Token` directly without executing the
  page's JS, and the last real-browser test of these buttons predates the
  CSRF change. Server-side CSRF verified working on production via curl
  (matching token → 200, wrong token → 403), so the fix is purely moving the
  two helpers into the account block.
- [x] **Silent-failure hardening (same fix):** `revokeKey` ignores a non-OK
  response and `signOut` reloads unconditionally, so any future server-side
  failure of these actions is also invisible. Surface an error message on
  failure like `createKey`/`rotateKey` already do. Fixed 2026-07-13, commit
  `162f002` — both now show an error banner on a non-OK response.

## Needs the project owner (Supabase dashboard / decisions)

- [ ] **Supabase Auth URL configuration must match the real domain.** In
  Supabase → Authentication → URL Configuration:
  - Add `https://pub-tools-dcf-nu.vercel.app/v1/auth/callback` to the
    Redirect URLs allowlist (the allowlisted URL from Phase 6 used the dead
    `pubtools-dcf.vercel.app` domain).
  - Review the Site URL: if it is `http://127.0.0.1:8000`, failed-allowlist
    fallbacks land on localhost — set it to the production origin once local
    development no longer depends on it (local logins use the explicit
    allowlisted localhost redirect, not Site URL).
  - Then test a full GitHub + email sign-in from the production site with the
    local server STOPPED, including returning after a browser restart.
- [ ] **Auth/quota env vars exist only in the Production environment.**
  `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `PUBLIC_BASE_URL` are not
  set for Preview, so preview deployments run with auth OFF (valuations
  unauthenticated behind the in-process limiter) and sign-in unconfigured.
  Decide whether previews should mirror production auth; if so, add the vars
  for Preview with a preview-appropriate `PUBLIC_BASE_URL`.
- [ ] **Custom SMTP in Supabase** before relying on email magic-link login
  for real customer volume (carried over from Phase 6 — default sender is
  rate-limited/dev-oriented).

## Pre-existing findings from the 2026-07-13 plan-vs-code audit

- [x] **Phase B quota consume result is ignored** in `app/api.py` — an
  over-limit race can still receive a 200. Fixed 2026-07-13, commit
  `045d202` — a rejected consume now replaces the computed response with the
  same 429 the pre-flight gate returns; regression test
  `test_stale_peek_does_not_let_an_over_limit_consume_serve_a_200`.
- [x] **Middleware pre-flight errors (401/403 auth, 503 quota-storage) lack
  `Cache-Control: no-store`**. Fixed 2026-07-13, same commit `045d202` —
  both shared response builders now set `no-store` unconditionally (live
  curl had shown Vercel injecting its own `public, max-age=0` default on
  those responses).
- [x] **Login rate limiter is instance-local on Vercel** (in-process
  `DailyRequestLimiter`), so the per-IP login cap is a warm-instance guard
  only in production. **Update 2026-07-13:** fix designed into Phase 8
  (Redis `INCR` per ip+day, Slice B, fail-open to the in-process limiter).
  **Fixed 2026-07-14** — Slice B shipped `RedisLoginRateLimiter`; the
  counter is shared across instances whenever Redis is configured
  (cross-instance test in `tests/test_response_cache.py`).
- [x] **PROGRESS.md missing a session entry** for the CSRF token system,
  peppered API-key hashing, and doc clarifications from the "Phase 7 push"
  commit. Backfilled 2026-07-13 into the audit-session entry; stale
  test/coverage counts corrected; the leftover HTML comment fragment was
  removed in commit `ba3b93b`.

## Phase 8 (designed 2026-07-13; Slice A implemented)

- [x] **User provisioning needed before live verification:** Vercel
  dashboard → `pub-tools-dcf` → Storage → Marketplace → Upstash (Redis),
  free tier, region iad1, connect for Production + Preview. User confirmed
  this is complete 2026-07-13. Local `vercel env pull` and deployment/live
  verification remain.
- [x] **Slice A — distributed fundamentals cache and single-flight.** Upstash
  REST client, four L2 ticker caches, versioned envelopes, token-safe lock,
  fail-open behavior, and shared-backend tests completed 2026-07-13. The
  planned lock TTL was corrected from 10s to 45s to cover the actual bounded
  FMP retry path; losers still fall through after 3s.
- [x] **Slice B — response cache and distributed login limiter.** This closes
  the instance-local login-limiter issue above. Completed 2026-07-14:
  `app/response_cache.py` + route integration (hits skip FMP and
  `compute_dcf`, identical ETag, still metered; generation-aware key ready
  for Slice C's rotation) and `RedisLoginRateLimiter`. 292 tests passing,
  94.04% coverage.
- [ ] **Slice C — DB read-through plus immutable normalized snapshots and
  migration 003.** Added 2026-07-13: cache miss order is L1 → Redis → latest
  verified Supabase snapshot → provider; provider success writes the immutable
  snapshot/current ticker head before publishing cache and returning. See
  ADR-006 and Phase 8 for freshness and database-failure behavior.
- [ ] **Daily all-database-ticker refresh scheduler.** ADR-007 requires one
  complete statements/profile/quote FMP cycle for every ticker in
  `ticker_snapshot_heads` during each day's 6 PM Eastern window. Configure the
  CRON_SECRET-protected endpoint at both `0 22 * * *` and `0 23 * * *`, then
  use an `America/New_York` hour guard plus a durable Eastern-date run claim.
  The run snapshots all database tickers into per-ticker claims; no activity
  filter, budget skip, or silent deferral is allowed. Implement with Slice C.
- [ ] **Daily refresh capacity gate.** Before deployment, prove the FMP plan and
  runtime can process the entire ticker manifest (including multiple endpoints
  and bounded retries). Insufficient capacity must fail visibly and trigger an
  upgrade/durable-worker decision; it must not silently omit tickers.

## Phase 9 — Public site: domain migration (owner actions, 2026-07-16)

Code for Phase 9 Slices 1–2 is **done** (portfolio at `/`, API directory at
`/apis`, DCF at `/dcf`, images at `/Pics/*`, auth callback retargeted to
`/dcf`). What remains needs the owner's Vercel/Supabase dashboards. **Do these
in order — skipping any of the middle three breaks sign-in in production**,
exactly as the 2026-07-13 `PUBLIC_BASE_URL` incident above did.

**The domain is `ashaat.dev`** (confirmed by the owner 2026-07-16).

- [ ] **Remove `ashaat.dev` from the standalone portfolio Vercel project**
  (a domain can only be attached to one project at a time).
- [ ] **Add `ashaat.dev` to the `pub-tools-dcf` project** and verify DNS
  resolves. Decide whether `www.ashaat.dev` should redirect to the apex.
- [ ] **Update Production `PUBLIC_BASE_URL`** to `https://ashaat.dev` and
  redeploy. The OAuth/magic-link `redirect_to` is built from this value; a stale
  value sends users to the old host after sign-in.
- [ ] **Add `https://ashaat.dev/v1/auth/callback`** to Supabase →
  Authentication → URL Configuration → Redirect URLs, and review the Site URL.
  Supabase only honors allow-listed redirect targets. (The previously
  allow-listed `pub-tools-dcf-nu.vercel.app` callback can stay for now — the
  vercel.app host keeps working — but `PUBLIC_BASE_URL` decides which host
  sign-in actually returns to, so only one completes a round trip.)
- [x] **Update the hardcoded base URL in `docs/index.html`** →
  `https://ashaat.dev`. Done 2026-07-16: both curl examples updated. The
  endpoint builder needed no change — `defaultBaseUrl()` derives from
  `window.location.origin`, so it follows whatever host serves the page.
- [ ] **Test on `https://ashaat.dev` with the local server stopped:** `/`
  portfolio, `/apis` directory, `/dcf` tool, a full GitHub **and** email sign-in
  round trip landing on `/dcf`, and a valuation request.
- [ ] **Retire the old portfolio Vercel project** once `ashaat.dev` resolves here.

Notes for whoever does this:

- The `-nu` vercel.app domain keeps working alongside the custom domain; only
  `PUBLIC_BASE_URL` decides where sign-in returns to, so sign-in will complete on
  exactly one host until the custom domain is configured everywhere.
- `docs/Pics/` (~540 KB, 7 images) now ships in the function bundle. If Vercel
  bundle size ever becomes a gate, move these to Vercel static hosting/CDN
  rather than serving them through the Python function.
- 4 images in the source portfolio repo were **not** copied because nothing
  referenced them (`AWS-Certified-Cloud-Practitioner-logo.png`, `GithubLogo.png`,
  `Java_programming_language_logo.svg.png`, `OutlookLogo.jpg`).
- The source portfolio repo also has a second page (`portfolio.html`, "Course
  Portfolio") that was **not** migrated; its nav link was dropped. Decide whether
  it should come across.

## Feature: real-time current price from Finnhub, never cached (planned 2026-07-16)

Recorded as **ADR-008** in `ARCHITECTURE_DECISIONS.md`. This section is the
working definition and the sliced implementation plan.

### What and why

Today the market price reaches the response through two cache layers — the
per-ticker quote cache (`FundamentalsService._get_quote`, 60s L1 / Redis
`quote:`) and the whole-response valuation cache (`dcf:v1:resp:`, 60s) — and the
planned ADR-007 daily refresh would make it up to a full day old. The product
requirement is that the response always shows a **live** price.

**User decision (2026-07-16):** the current price must be requested from Finnhub
on every call, and **no current price may be cached anywhere**.

**Clarified (2026-07-16):** the *only* caching wanted is the **financial
statements** for widely-used tickers. If a customer runs three different discounts
on AAPL, the statements must be fetched from FMP **once**, not three times — but
the request/response and the DCF math are **not** cached. The math is recomputed
per request (it is pure and cheap) from the cached statements + the live price.
This retires the Phase 8 Slice B valuation response cache (`dcf:v1:resp:`) and the
Phase 7 ETag/304 handling for this endpoint; the Slice A fundamentals cache
(`fund:`/`profile:` + single-flight) is exactly the "reuse the statements"
mechanism and is kept.

### Customer-request read path (confirmed 2026-07-16)

Every valuation request resolves the **statements** and the **price** on two
independent paths, then computes fresh:

1. **Statements** — cache → database → FMP, returning as soon as one layer has
   them (ADR-006 read-through; the detailed order lives in `IMPLEMENTATION_PLAN.md`
   Phase 8 "Authoritative cache-aside read/write flow"):
   - in-process L1 cache hit → use it;
   - else Redis L2 (`fund:`/`profile:`) hit → hydrate L1, use it;
   - else Supabase snapshot (DB) hit → hydrate cache, use it;
   - else (genuinely cold ticker, absent everywhere) → fetch from FMP, persist to
     DB + cache, use it.
   - A ticker already in the DB is **never** refreshed from FMP by a customer
     request, even when stale; the ADR-007 daily 6 PM job is the only refresher
     (user decision 2026-07-16). Stale DB data is served with a freshness note.
2. **Price** — always live from Finnhub, cached nowhere (ADR-008).
3. **Compute** — run the DCF fresh from the resolved statements + the live price;
   the response is not cached.

**Dependency/sequencing:** the database step (cache → **DB** → FMP) is Phase 8
**Slice C**, which is not yet implemented — today's code does cache → FMP with no
DB layer. This Finnhub feature (live price + retiring the response cache) is
independent of Slice C and can ship first; the DB layer then slots into the
statements path without changing the price path.

### Scope

- In scope: a Finnhub quote client; live per-request price fetch; injecting
  `current_price`/`price_as_of`/`price_fetched_at`/`upside_pct` fresh on every
  request; stripping those four from the response cache; removing the FMP quote
  path; making `/v1/valuations/*` `no-store`; docs/config/tests.
- Out of scope: changing the FMP statements/profile path (ADR-006/007 unchanged
  except that the daily cycle no longer fetches a quote); Finnhub for anything
  other than price; websocket/streaming (REST `/quote` is enough).

### Key architectural consequences (accept explicitly)

1. **`/v1/valuations/*` becomes `Cache-Control: no-store`.** The Phase 7 public/
   edge cache no longer applies to this endpoint because the body carries an
   uncacheable live price and the response is no longer cached at all. Provider
   savings come from the **financial-statement cache**, not a response cache.
2. **The response cache is retired, not modified.** `app/response_cache.py`
   (`dcf:v1:resp:`, Slice B) and its route integration are removed, along with
   the ETag/conditional-304 handling for this endpoint. Nothing strips price
   fields from a response cache because there is no response cache.
3. **One Finnhub call per valuation request.** Request volume is re-coupled to
   Finnhub's rate limit (free tier: 60 req/min). Capacity/plan is a deployment
   gate, same shape as the FMP capacity gate above. Coalescing genuinely
   concurrent identical in-flight requests into one live fetch is permitted (it
   returns one live result to simultaneous callers; it is not a time-cache) but
   optional — not required for v1.
4. **The financial-statement cache is the point.** Slice A's `fund:`/`profile:`
   L2 + in-process L1 + single-flight are kept unchanged in purpose: N
   differently-assumed valuations of one ticker cost one FMP statement fetch. The
   cached snapshot must be **price-free** (price is live), so `current_price` is
   dropped from the cached `BaseFinancials` snapshot.
5. **The DCF engine stays pure.** It must produce the price-independent math
   without requiring a positive `current_price` (today `compute_dcf` validates
   `current_price > 0` and computes `upside_pct` internally — that moves to an
   API-layer step applied after the live fetch).
6. **Finnhub outage never yields a stale price.** Degrade to `null`
   price/`upside_pct` + a data-quality warning; still return the intrinsic value.

### Cross-doc updates required

- [ ] ADR-007 + Phase 8 Slice C: the daily FMP refresh no longer fetches/stores
  a quote; remove the `dcf:v1:quote:` key from the Phase 8 namespace; Slice C
  must not re-introduce a cached quote. (Note added in `IMPLEMENTATION_PLAN.md`
  Phase 8 as part of this feature.)
- [ ] Phase 7 notes / `docs/index.html` "Caching" section: valuation responses
  are `no-store`; only the internal compute cache is shared; the price is live.
- [ ] README + `docs/index.html`: document Finnhub as the price source, the
  `FINNHUB_API_KEY` setup, real-time price / never-cached behavior, and the
  Finnhub-outage null-price degrade.

### Implementation plan (sliced)

**Slice 1 — Finnhub client + normalization (no route wiring).**
- [ ] `app/providers/finnhub.py`: `FinnhubConfig.from_env()` (`FINNHUB_API_KEY`);
  `FinnhubClient` over the existing `httpx` dependency (no new runtime dep, same
  pattern as `app/providers/fmp.py`/`app/supabase.py`). `fetch_quote(ticker) ->
  (raw: dict, fetched_at: datetime)` hitting `GET /api/v1/quote?symbol=&token=`.
  Timeout well under the Vercel function budget; classify transport/timeout/
  malformed-JSON/non-200 into `ProviderError`; empty/`c==0` payload (Finnhub's
  "unknown symbol" shape) into `TickerNotFoundError`.
- [ ] `normalize_finnhub_quote(ticker, raw, fetched_at) -> NormalizedQuote`:
  map `c`→price, `t` (unix seconds)→`price_as_of` (UTC); reject non-finite/≤0
  price via `NormalizationError`, mirroring `normalize_fmp_quote`.
- [ ] Unit tests with a fake `httpx` transport: happy path, unknown symbol,
  timeout/5xx→ProviderError, malformed JSON, non-finite/zero price, `t` mapping,
  and `from_env` on/off (absent key → client not constructed / feature off).

**Slice 2 — Decouple price from the math; recompute per request with a live price.**
- [ ] Engine/model split: `compute_dcf` returns the price-independent `Valuation`
  (no `current_price`/`upside_pct` requirement — drop the `current_price > 0`
  precondition from the math path). Add a small pure helper
  `apply_market_price(valuation, price, price_as_of, price_fetched_at) ->
  Valuation` (or compute upside in the API layer) that fills
  `current_price`/`upside_pct`. Keep the engine I/O-free.
- [ ] `ValuationResponse`/`Valuation`: make `current_price` and `upside_pct`
  `Optional` (null only on Finnhub outage); `price_as_of` is already optional.
- [ ] Route (`get_valuation`): every request fetches statements via the
  fundamentals cache (unchanged), fetches the live Finnhub price via the
  lifespan-scoped client, computes the DCF fresh, applies the live price +
  `upside_pct`, and returns. Set `Cache-Control: no-store` for
  `/v1/valuations/*`. On Finnhub failure, inject nulls + a data-quality warning.
- [ ] Wire `app.state.finnhub` in `create_app` lifespan (auto-enable on
  `FINNHUB_API_KEY`; absent → price feature off, warning surfaced).
- [ ] Tests: two rapid same-ticker requests with **different assumptions** make
  exactly one FMP statement fetch but two Finnhub price fetches (assert call
  counts); each response carries a live `price_fetched_at` and `no-store`;
  Finnhub-down → null price + warning with the math still present;
  metering/quota behavior unchanged.

**Slice 3 — Retire the response cache and the dead FMP quote path.**
- [ ] Remove `app/response_cache.py`, its route integration in
  `app/api.py::get_valuation` (`get_cached_response`/`store_response`), and
  `tests/test_response_cache.py`'s response-cache tests (keep the login-limiter
  tests, which live in the same file — split them out).
- [ ] Remove the Phase 7 ETag/conditional-304 branch from `get_valuation` and the
  `VALUATION_CACHE_CONTROL`/`VALUATION_VARY` public-cache headers for this
  endpoint (replaced by `no-store`). Keep `app/http_cache.py` only if used
  elsewhere — verify with a grep; otherwise remove it and its tests.
- [ ] Remove `FundamentalsService._get_quote`, `_quote_cache`, the Redis `quote:`
  read/write, and `quote_override`/`quote_fetched_at` plumbing through
  `FMPClient.fetch_fundamentals`/`normalize_fmp_fundamentals`. Drop
  `current_price`/`price_as_of`/`price_fetched_at` from the cached
  `BaseFinancials` snapshot so no price is ever cached.
- [ ] Remove `FMPClient.fetch_quote` and its tests (verify with a grep it is
  unused elsewhere first).
- [ ] Update `IMPLEMENTATION_PLAN.md` Phase 8 namespace + Slice C to drop the
  `quote:` and `resp:` keys and the daily quote fetch.
- [ ] Full suite green; ruff/mypy clean; coverage floor held.

**Slice 4 — Docs, config, live verification.**
- [ ] README + `docs/index.html`: Finnhub price source, `FINNHUB_API_KEY` setup,
  real-time/never-cached price, `no-store` valuations, outage degrade.
- [ ] Add `FINNHUB_API_KEY` to Vercel Production + Preview.
- [ ] Live-verify against a real server (not just TestClient): consecutive calls
  return live prices with advancing `price_fetched_at`; header is `no-store`;
  killing the Finnhub key reproduces the null-price + warning degrade; the DCF
  math is unchanged across the price refresh.

### Open sub-decisions (defaults chosen; change here if desired)

- **Outage behavior:** default = null price + warning (return the math). Flip to
  502-only if the price is considered mandatory.
- **Concurrent-request coalescing of the live fetch:** default = not built for
  v1; add later only if Finnhub rate limits bite.
