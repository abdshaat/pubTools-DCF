# Issues

## ⚠️ Live production defects found 2026-07-26 (doc/state audit)

Both were found by reading the real production runtime logs (Vercel MCP,
`pub-tools-dcf` production, deployment `dpl_9gYqAHSc…` = commit `912c9de`).
Neither is visible from tests, which is why they survived a green suite.

- [ ] **(owner) The production `FINNHUB_API_KEY` value is not a valid key —
  every production valuation is returning a null price.** A real keyed
  `GET /v1/valuations/AAPL` on 2026-07-26 19:25 UTC logged
  `GET https://finnhub.io/api/v1/quote?symbol=AAPL&token=X-Finnhub-Token
  "HTTP/1.1 401 Unauthorized"` and closed with `"price": "unavailable"`. So
  ADR-008's headline feature — the live market price — is silently off in
  production, and the null-price degrade (working exactly as designed) is what
  hides it. Evidence it is the *value*, not the key or the code: the key in the
  local `.env` (40 characters) returns **HTTP 200** for the same symbol from
  this machine, and the variable *is* present in Vercel for Preview +
  Production (`vercel env ls`, added 9 days ago). The logged token reads
  `X-Finnhub-Token`, which is the name of Finnhub's auth *header* — consistent
  with the header name having been pasted where the key value belongs. **Fix:**
  Vercel → `pub-tools-dcf` → Settings → Environment Variables → replace
  `FINNHUB_API_KEY` (Production, and check Preview) with the 40-character key
  from the Finnhub dashboard, then redeploy. Confirm with one valuation whose
  response carries a non-null `current_price`. Also in `TODO.md` §6.
- [x] **The `httpx` INFO log prints full outbound request URLs, and the FMP key
  rides in the URL query string — so `FMP_API_KEY` is written to production
  logs in the clear.** **Fixed 2026-07-26** (same session, details below the
  original description). `configure_logging()` installs the scrubbing handler on
  the **`app`** logger tree only (`app/observability.py`, `logging.getLogger("app")`
  + `propagate = False`), which is correct for our own lines but leaves the
  `httpx` logger untouched; on Vercel those records still reach stdout. Proven
  by the Finnhub line above (a credential printed verbatim). FMP builds
  `{"symbol": ticker, "apikey": self._api_key, …}` as query params
  (`app/providers/fmp.py:167`), so **every** FMP call — 4 per nightly refresh,
  plus any cold ticker — emits the key. Supabase and Upstash are unaffected
  (their credentials are headers). This contradicts the Phase 11 Slice 2 claim
  that "secrets and PII never reach a log", which holds only inside the `app`
  tree. **Fix (code, not owner):** either silence/raise the level of the
  `httpx` logger at startup, or attach a scrubbing filter to it — the existing
  `scrub()`/`redact_url()` already do the work; the gap is only which logger
  they are wired to. Rotate `FMP_API_KEY` afterwards, since the current value
  has been in log storage.

  **Fix, as shipped (2026-07-26).** `ScrubbingFilter` in `app/observability.py`,
  attached by `configure_logging()` to the `httpx` and `httpcore` loggers.
  Filtering, not silencing: a logger-level filter runs *before* propagation to
  whatever handler the runtime installed, which is the only hook available for a
  logger this app does not own. **Silencing `httpx` was rejected** — those lines
  are what exposed both defects in this section, so the fix redacts them rather
  than throwing away the visibility. Three details that made it non-trivial:
  (1) the URL arrives as an `httpx.URL` in `record.args`, not in `record.msg`,
  so scrubbing the message alone would have caught nothing; (2) `record.args`
  must stay a **tuple** or `%`-formatting breaks; (3) arguments are only
  replaced when scrubbing actually changed the text, so the `int` behind the
  `%d` status code stays an `int` — otherwise the record fails to format and the
  whole line is lost, which is how a redaction fix turns into an outage in the
  logs. The filter is idempotent across repeated `configure_logging()` calls
  (marked and removed like the handler), and it touches nothing else about those
  loggers — no level, no handler, no `propagate`, so an embedder still decides
  whether httpx logs at all.
  **Tests: 4 new** in `tests/test_observability.py` (suite **457 → 461**, 94.88%
  coverage; ruff/format/mypy clean) — a real `FMPClient.fetch_fundamentals` call
  whose key must not appear in any emitted line while `symbol=AAPL` and the
  status still do, the Finnhub `token=` shape with its `401` preserved, the
  non-string-argument guard, and filter-stacking.
  **Live-verified outside pytest** against real outbound calls, reproducing
  Vercel's stdout handler at INFO — before: `…&token=fake-key-DO-NOT-USE-1234567890
  "HTTP/1.1 401 Unauthorized"`; after: `…&token=REDACTED "HTTP/1.1 401
  Unauthorized"`; and the FMP shape as
  `…income-statement?symbol=AAPL&apikey=REDACTED&limit=8`. *(The verification
  script deliberately avoided importing `app.api`, because that path calls
  `load_dotenv()` and would restore real credentials — the trap recorded in
  `PROGRESS.md` on 2026-07-26.)*
  ⚠️ **Still owner-side: rotate `FMP_API_KEY` after this deploys** — the current
  value has already been written to Vercel's log storage. `TODO.md` §7.

# Production sign-in investigation (2026-07-13)

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

- [x] **Supabase Auth URL configuration must match the real domain.** Closed
  2026-07-26 as **superseded**: this item was written for the
  `pub-tools-dcf-nu.vercel.app` host, which the Phase 9 migration to
  `ashaat.dev` replaced. The live equivalents were completed by the owner —
  `PUBLIC_BASE_URL = https://ashaat.dev` and the matching Supabase redirect
  allowlist entry (`TODO.md` §1.3/§1.4) — and sign-in has since been observed
  completing in production. Original wording below for history. In
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
  *(Re-confirmed 2026-07-26 via `vercel env ls production`: all three are still
  Production-only. Tracked for the owner as `TODO.md` §5.2 — this is the same
  item, not a second one.)*
- [ ] **Custom SMTP in Supabase** before relying on email magic-link login
  for real customer volume (carried over from Phase 6 — default sender is
  rate-limited/dev-oriented). *(Same item as `TODO.md` §5.1.)*

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
- [x] **Slice C — DB read-through plus immutable normalized snapshots and
  migration 003.** *(Closed 2026-07-26: the scheduler has been observed running
  successfully in production for five consecutive days — details below.)* Added 2026-07-13: cache miss order is L1 → Redis → latest
  verified Supabase snapshot → provider; provider success writes the immutable
  snapshot/current ticker head before publishing cache and returning. See
  ADR-006 and Phase 8 for freshness and database-failure behavior.
  **Part 1 implemented 2026-07-18** (migration 003 drafted + read-through +
  snapshot writes + fail-closed 503 + 20 tests; details in PROGRESS.md);
  the daily-refresh scheduler below is what keeps this box open. ⚠️ Apply
  migration 003 before deploying this code — with Supabase configured, a
  missing table is a storage error (503 on cold tickers), not a miss.
- [x] **Daily all-database-ticker refresh scheduler.** *(Closed 2026-07-26 by
  reading the production ledger: runs for 2026-07-21…2026-07-25 all `succeeded`,
  one claim per (ticker, date), `total=attempted=succeeded=1, failed=0`, and
  `normalized_snapshots` still holds exactly one row — an unchanged filing
  re-confirmed daily updates the head and creates no new immutable row. The two
  UTC schedules produced exactly one claimed run per Eastern date.)* ADR-007 requires one
  complete statements/profile FMP cycle (no quote — ADR-008) for every ticker
  in `ticker_snapshot_heads` during each day's 6 PM Eastern window.
  **Code implemented 2026-07-18** (`app/refresh.py`, the CRON_SECRET-protected
  endpoint, `vercel.json` at `0 22 * * *` + `0 23 * * *`, the
  `America/New_York` hour guard, durable Eastern-date run claim, full
  per-ticker claim manifest with no filter/skip/deferral — details in
  PROGRESS.md). Box stays open until it runs live: needs migration 003
  applied, `CRON_SECRET` in Vercel Production (TODO §4.2), the capacity gate
  below, and a deployed observation of one real run.
- [x] **Daily refresh capacity gate.** *(Verified 2026-07-20 by calculation and
  confirmed 2026-07-26 by five real runs: the one-ticker manifest completes in
  about one second and spends 4 FMP calls, far inside the ~250/day allowance and
  the function duration. Re-run this gate before growing the manifest.)*
  Original wording: before deployment, prove the FMP plan and
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

> **Status sync 2026-07-26.** These boxes had gone stale against `TODO.md` §1,
> which is the owner's live queue and records the work as done. Corrected here
> so the two files stop contradicting each other. What is genuinely still open
> is only §1.4c (apex is not the primary host) and §1.5 (a sign-in round trip
> tested with the local server stopped).

- [x] **Remove `ashaat.dev` from the standalone portfolio Vercel project**
  (a domain can only be attached to one project at a time). Done by the owner
  (`TODO.md` §1.1).
- [x] **Add `ashaat.dev` to the `pub-tools-dcf` project** and verify DNS
  resolves. Done (`TODO.md` §1.2). ⚠️ The www/apex direction was *not* settled
  the way the owner intended — see §1.4c and the note below.
- [x] **Update Production `PUBLIC_BASE_URL`** to `https://ashaat.dev` and
  redeploy. Done 2026-07-16 (`TODO.md` §1.3); the login redirect was
  live-verified emitting `redirect_to=https://ashaat.dev/v1/auth/callback`.
- [x] **Add `https://ashaat.dev/v1/auth/callback`** to Supabase →
  Authentication → URL Configuration → Redirect URLs, and review the Site URL.
  Done by the owner (`TODO.md` §1.4). Supabase only honors allow-listed
  redirect targets. (The previously allow-listed `pub-tools-dcf-nu.vercel.app`
  callback can stay — the vercel.app host keeps working — but
  `PUBLIC_BASE_URL` decides which host sign-in actually returns to, so only one
  completes a round trip.)
- [ ] **⚠️ Flip the primary domain to the apex** (`TODO.md` §1.4c).
  Re-verified live 2026-07-26: `https://ashaat.dev/` still returns **308 →
  `https://www.ashaat.dev/`**, i.e. www is still primary, the reverse of the
  stated intent. It works only because the 308 preserves `?code=`, and the
  PKCE `pt_oauth_verifier` cookie is host-only.
- [x] **Update the hardcoded base URL in `docs/index.html`** →
  `https://ashaat.dev`. Done 2026-07-16: both curl examples updated. The
  endpoint builder needed no change — `defaultBaseUrl()` derives from
  `window.location.origin`, so it follows whatever host serves the page.
- [ ] **Test on `https://ashaat.dev` with the local server stopped:** `/`
  portfolio, `/apis` directory, `/dcf` tool, a full GitHub **and** email sign-in
  round trip landing on `/dcf`, and a valuation request. *(Still open as
  `TODO.md` §1.5, though production logs on 2026-07-26 show a real browser
  completing `/v1/auth/me`, key create/revoke, and a 200 valuation — so the
  round trip does work; what is missing is the owner's deliberate
  local-server-stopped confirmation.)*
- [x] **Retire the old portfolio Vercel project** once `ashaat.dev` resolves
  here. Done (`TODO.md` §1.6); `vercel list-projects` shows `pub-tools-dcf` as
  the only project on the account (verified 2026-07-26).

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
  **Updated 2026-07-17 (`TODO.md` §2.3):** the AWS logo is now in use — a
  Certified Cloud Practitioner card was added to the certifications section, and
  production logs confirm `/Pics/AWS-Certified-Cloud-Practitioner-logo.png`
  serving 200. The other three stay skipped (two are non-transparent duplicates,
  one has no section). **Still owner-side:** supply the certification's earned
  date (or "in progress") so the card can show it — no date was invented.
- The source portfolio repo also has a second page (`portfolio.html`, "Course
  Portfolio") that was **not** migrated; its nav link was dropped. **Decided
  2026-07-17 (`TODO.md` §2.1): drop it.** Nothing further to do.

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

- [x] ADR-007 + Phase 8 Slice C: the daily FMP refresh no longer fetches/stores
  a quote; remove the `dcf:v1:quote:` key from the Phase 8 namespace; Slice C
  must not re-introduce a cached quote. Done 2026-07-18: the Phase 8
  supersession note landed 2026-07-16; the remaining stale details inside
  Phase 8 (`quote:`/`resp:` rows in the key table, `last_quote`/
  `quote_verified_at` on the migration-003 head spec, quote references in the
  single-flight/promotion/exit-criteria text) were scrubbed so Slice C is
  implemented from a spec that cannot reintroduce a stored price.
- [x] Phase 7 notes / `docs/index.html` "Caching" section: valuation responses
  are `no-store`; only the internal compute cache is shared; the price is live.
  Done 2026-07-18: supersession note added at the top of Phase 7 in
  `IMPLEMENTATION_PLAN.md`; `docs/index.html`'s section was rewritten as
  "Live price & caching" with the Slice 2–4 work.
- [x] README + `docs/index.html`: document Finnhub as the price source, the
  `FINNHUB_API_KEY` setup, real-time price / never-cached behavior, and the
  Finnhub-outage null-price degrade. Done 2026-07-18 with the Slice 2–4 work.

### Implementation plan (sliced)

**Slice 1 — Finnhub client + normalization (no route wiring).**
- [x] `app/providers/finnhub.py`: `FinnhubConfig.from_env()` (`FINNHUB_API_KEY`);
  `FinnhubClient` over the existing `httpx` dependency (no new runtime dep, same
  pattern as `app/providers/fmp.py`/`app/supabase.py`). `fetch_quote(ticker) ->
  (raw: dict, fetched_at: datetime)` hitting `GET /api/v1/quote?symbol=&token=`.
  Timeout well under the Vercel function budget; classify transport/timeout/
  malformed-JSON/non-200 into `ProviderError`; empty/`c==0` payload (Finnhub's
  "unknown symbol" shape) into `TickerNotFoundError`. Completed 2026-07-17 —
  3s default timeout, no retries (a quote is latency-sensitive; the caller
  degrades to a null price instead), 401/403 → `ProviderAuthError`, and
  transport exceptions are chained rather than interpolated so the token can
  never leak into an error message (deliberate deviation from the FMP client).
- [x] `normalize_finnhub_quote(ticker, raw, fetched_at) -> NormalizedQuote`:
  map `c`→price, `t` (unix seconds)→`price_as_of` (UTC); reject non-finite/≤0
  price via `NormalizationError`, mirroring `normalize_fmp_quote`. Completed
  2026-07-17 — `t == 0` (Finnhub's "no timestamp") maps to `price_as_of=None`.
- [x] Unit tests with a fake `httpx` transport: happy path, unknown symbol,
  timeout/5xx→ProviderError, malformed JSON, non-finite/zero price, `t` mapping,
  and `from_env` on/off (absent key → client not constructed / feature off).
  Completed 2026-07-17 — 25 tests in `tests/test_finnhub.py` (house style:
  sync tests + `asyncio.run`), including a token-never-in-error-text assertion.
  Suite 298 → 323, 94.25% coverage, ruff/format/mypy clean.
- [x] **Live verification against the real Finnhub API** (2026-07-17, key in
  `.env` + Vercel Preview/Production): real AAPL ($333.74) and MSFT ($393.82)
  quotes fetched and normalized with `price_as_of` = that Friday's 4 PM ET
  close (correct after-hours behavior); an unknown symbol's all-zero body was
  classified `TickerNotFoundError`. `tests/conftest.py`'s isolation fixture now
  clears `FINNHUB_API_KEY` so the ambient real key can never flip Slice 2's
  auto-enable on inside tests (the 2026-07-11 Supabase-creds incident class).

**Slices 2+3+4 — implemented together 2026-07-18** (one structural change; the
route rewrite, cache retirement, and docs land as a unit). Resolutions the
owner made before implementation: (a) **quota flow simplified** — one atomic
`check_and_increment` pre-flight replaces the peek/deferred-consume split
(which existed only so 304s could be free) and `X-RateLimit-*` headers return
on all valuation responses (safe again under `no-store`); (b) a **Finnhub
unknown-symbol for an FMP-served ticker degrades like an outage** (null price +
warning, never a 404/502); (c) **`model_version` bumped 0.1.0 → 0.2.0** for the
nullable-price schema change (OpenAPI snapshot regenerated).

- [x] Engine/model split: `BaseFinancials` and `Valuation` carry **no price
  fields at all** (stronger than Optional — a cached snapshot cannot represent
  a price by construction); `compute_dcf` drops the `current_price > 0`
  precondition and its internal upside math; upside is computed in
  `build_valuation_response(quote=...)` from the live quote.
- [x] Route: statements via the fundamentals cache → live Finnhub fetch →
  fresh `compute_dcf` → `Cache-Control: no-store`. Any `ProviderError`/
  `NormalizationError` from Finnhub (outage, rate limit, unknown symbol,
  auth misconfig) degrades to null price + warning.
- [x] `app.state.finnhub` wired in the lifespan (auto-enable on
  `FINNHUB_API_KEY`, injectable `finnhub_client=` for tests, closed on
  shutdown); absent → "not configured" warning.
- [x] Removed: `app/response_cache.py`, `app/http_cache.py` (only the
  valuation route used it), the ETag/304 branch, `Vary`, the peek phase,
  `is_not_modified`, `FundamentalsService._get_quote`/`_quote_cache`/Redis
  `quote:`/stale-quote logic/`quote_ttl_seconds`, the FMP `quote` endpoint
  from `fetch_fundamentals` (cold load is now 4 FMP calls, not 5),
  `FMPClient.fetch_quote`, and `normalize_fmp_quote`.
- [x] Tests: login-limiter tests split into `tests/test_login_rate_limit.py`;
  new ADR-008 route tests (1 FMP statement load vs 3 Finnhub calls across 3
  same-ticker requests, live-price/upside assertions, no-store + no-ETag,
  outage/unknown-symbol/feature-off degrades, conditional headers ignored and
  metered, atomic-consume ordering, quota headers counting down). Suite:
  **291 passing, 94.08% coverage**; ruff/format/mypy clean.
- [x] Docs: README rate-limit/live-price/statement-freshness sections and
  `docs/index.html` "Live price & caching" section rewritten; example response
  and version strings updated to 0.2.0; `base_financials` example is
  price-free.
- [x] Live-verified against real uvicorn with real FMP+Finnhub keys:
  AAPL 200 with live price $333.74, `upside_pct` −76.07, `model 0.2.0`,
  `no-store`, no ETag/Vary, `X-RateLimit-Remaining: 99`, price-free
  `base_financials`; second call refreshed `price_fetched_at`; with
  `FINNHUB_API_KEY` blanked the same request returned identical math with
  null price/upside + the config warning.
- [x] `FINNHUB_API_KEY` present in Vercel Preview+Production (owner, 2026-07-17).

### Open sub-decisions (defaults chosen; change here if desired)

- **Outage behavior:** default = null price + warning (return the math). Flip to
  502-only if the price is considered mandatory.
- **Concurrent-request coalescing of the live fetch:** default = not built for
  v1; add later only if Finnhub rate limits bite.
