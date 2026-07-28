# TODO — owner actions

Everything here is a step **only you can do**: dashboard access, credentials, or
a product decision. Nothing in this file is something Claude can complete alone.
Each item says what it unblocks, so you can skip sections that aren't relevant
yet.

Last updated 2026-07-28.

**Open right now:** §9 — apply migration 005 **before** the next deploy (this
one blocks a deploy), §7.3 — rotate `FMP_API_KEY`, and §8.1 — one click to turn
on private vulnerability reporting. Everything else here is smaller or deferred.
*(§6 closed 2026-07-27: the live price is working in production.)*

---

## 9. ⚠️ Apply migration 005 before the next deploy

`supabase/migrations/005_p3_metered_quota.sql` is in the working tree. It is the
database half of performance item P3 (quota consumption and usage metering in
one round trip instead of two). **The code that calls it is committed alongside
it, so deploying without applying the migration first would 503 every keyed
valuation** — exactly the way the Slice C push did on 2026-07-18, which is the
one failure mode this project has actually hit.

- [x] **9.1 — Apply it. Done by you 2026-07-28, and live-verified the same
  session.** Every branch of the SQL was exercised against the real Supabase:
  `status_code` is no longer a required column (PostgREST's own schema says so);
  both new functions exist with the right signatures and all three input guards
  raise *before* any write; an admitted call returned `allowed=true` and wrote a
  row with `status_code: null, quota_consumed: true`; `finalize_usage_event` set
  it to 502 and then **refused to overwrite** on a second call; the over-limit
  call returned `allowed=false` and wrote its own `429, quota_consumed: false,
  rate_limited: true`. 001's `consume_daily_quota` is still present, so the
  currently-deployed code keeps working until the deploy. The check used a
  namespaced synthetic subject (`verify-005-<uuid>`, `customer_id` NULL) that
  cannot collide with a real API key, and **its three rows were deleted
  afterwards** — the ledger is exactly as it was.
- [x] **9.2 — Deployed 2026-07-28**, in that order (database first). Commit
  `68bc9cb` built and went live as `dpl_G3wMZx6y…` in `iad1`, aliased to both
  `www.ashaat.dev` and `ashaat.dev`; production `/health` reports instance
  `38610b89`. Smoke-checked: `/health`, `/ready`, `/dcf`, `/apis`,
  `/openapi.json` and `/docs` all 200; `/ready` reports **supabase ok
  (required), redis ok, finnhub ok**; an unauthenticated valuation still
  returns the correct 401; no 5xx and no error line in the runtime logs.
- [ ] **9.3 — One keyed valuation, when convenient (yours — needs a customer
  API key).** Everything above proves the app boots, reaches Supabase, and
  serves; it cannot prove the *metered* path, because valuations require a key
  and auth runs before quota — an unauthenticated request 401s without ever
  reaching the new RPC. **What to do:** sign in at `www.ashaat.dev/dcf` and run
  one valuation, exactly like §6.2. Then tell me and I will confirm two things
  that are only observable afterwards: the request's log line should read
  **`supabase_calls: 2`** (it was 3), and its `usage_events` row should carry
  **`status_code: null`** with `quota_consumed: true`. *(Reminder from §6.2: run
  it on the deployed site with your local server stopped, or the page sends the
  request to your laptop and production never sees it.)*
  **If it 503s instead**, that is the signal to roll back — `dpl_7EUe9c7W…`
  (the merge commit before this one) is the rollback candidate, and the
  migration is additive so it can stay.

**One thing to know about the data, because it changes what a column means.**
`usage_events.status_code` is now nullable, and going forward:

| Value | Meaning |
|---|---|
| `429` | the quota gate rejected the request |
| `NULL` | the request was admitted and served without failing — a 200 (or, rarely, a request that died before it could record its status) |
| anything else | a non-200 response, recorded after the fact |

So "how many of my requests succeeded" becomes `status_code is null` rather than
`status_code = 200`. If you have any saved SQL or dashboard that counts 200s,
that is the one query to update. Rows written before this migration are
unaffected and still carry their literal 200.

---

## 8. From the safety & security audit (2026-07-26)

The audit is in `issues.MD` → "Safety & security audit". Two HIGH and four
MEDIUM findings were **already fixed in code**; these four are the ones that
need you. None is urgent — the urgent ones are already done.

### 8.1 Turn on private vulnerability reporting (one click)

GitHub → this repo → **Settings** → **Security** → **Private vulnerability
reporting** → *Enable*.

`.github/SECURITY.md` now ships in the repo and tells finders to use that flow, which
is deliberately the one channel that needs no personal email address published
on a public repository. **Until the toggle is on, the button it points at does
not exist**, and someone with a real finding is left with a public issue as
their only option — which discloses the bug to everyone at the moment it is
reported.

### 8.2 Decide: keep `/docs` and `/redoc` on the CDN, or cut the dependency

Those two pages run Swagger UI / ReDoc from `cdn.jsdelivr.net`. They now carry
a strict Content-Security-Policy (they previously had **none**), and the
important line is `connect-src 'self'`: a hostile script could no longer send
anything it stole to an outside host. What a CSP cannot fix is that the script
is still *third-party code executing on the origin that holds customer
sessions* — it could still act as a signed-in user within that origin.

Your options, in increasing order of effort:

1. **Leave it.** Reasonable — the CSP does most of the work, and jsdelivr is
   widely trusted. This is the current state; no action needed.
2. **Vendor the assets.** Download the Swagger UI JS/CSS into `docs/` and serve
   them from `/`, then tighten `script-src` to `'self'`. Removes the third
   party entirely. Costs a few hundred KB in the bundle and a manual bump
   whenever you want a newer Swagger UI.
3. **Turn the pages off in production.** `/openapi.json` and the hand-written
   reference at `/dcf` already cover what customers need.

Tell me which and I'll implement it.

### 8.3 Decide: harden the CSRF cookie (needs a coordinated change)

`pt_csrf` is an unsigned double-submit token on a cookie with no `__Host-`
prefix. Anything able to set a cookie for `ashaat.dev` — a hostile or
compromised **sibling subdomain**, now or in future — could inject a value and
then send the matching header, satisfying the check. `SameSite=Lax` is what
actually stops cross-site POSTs today, which means the CSRF token is currently
the weaker of your two defenses rather than an independent second one.

Not fixed mid-audit because either fix **invalidates the token in every browser
currently holding one**, and it touches `docs/index.html` and the server
together:

- **`__Host-pt_csrf`** — browsers refuse to accept a `__Host-` cookie from a
  subdomain. Smallest change; a rename plus the JS that reads it.
- **Bind the token to the session** with an HMAC. Strictly stronger, slightly
  more code.

Low urgency: it needs an attacker who already controls a subdomain of
`ashaat.dev`. Worth doing before you add any subdomain (a staging host, a docs
host, a marketing page) — that is the day the assumption changes.

### 8.4 Nothing to do — recorded so it isn't re-litigated

- **HSTS**: Vercel injects `Strict-Transport-Security: max-age=63072000` on
  production responses, so you are covered. The application itself does not emit
  it, which only matters if this ever runs somewhere other than Vercel. Vercel's
  header includes neither `includeSubDomains` nor `preload`.
- **`connect-src` allows `http://localhost:*`** on the production page. That is
  deliberate — it lets the `/dcf` endpoint builder call a developer's local
  server — and it is also what stops that page from being able to send a pasted
  API key to any third-party host.

---

## 6. ✅ CLOSED 2026-07-27 — the production `FINNHUB_API_KEY` was not a valid key

*(Corrected in Vercel by Claude with the owner's go-ahead; one confirmation step
below is still yours. Original diagnosis kept, because the reasoning is the
reusable part.)*

Found 2026-07-26 by reading production runtime logs. A real keyed
`GET /v1/valuations/AAPL` at 19:25 UTC produced:

```
GET https://finnhub.io/api/v1/quote?symbol=AAPL&token=X-Finnhub-Token -> 401 Unauthorized
... "price": "unavailable"
```

So **every production valuation is serving `current_price: null` and
`upside_pct: null`** right now. The math is correct and the response is a 200 —
ADR-008's outage degrade is working exactly as designed, which is precisely why
this was invisible until someone read the logs.

It is the *value* that is wrong, not the key itself and not the code: the key in
your local `.env` returns **HTTP 200** for the same symbol, and the variable is
present in Vercel for Preview + Production. The token in the log line reads
`X-Finnhub-Token` — the name of Finnhub's auth *header* — which is what you'd
see if the header name got pasted where the key value belongs.

- [x] **6.1 — Replace `FINNHUB_API_KEY` in Vercel.** **Done by Claude
  2026-07-26 with your go-ahead**, using the working 40-character key from your
  local `.env` (`vercel env rm` + `add`). Both environments carry it:
  **Production** and **Preview**. *(Note: removing the Production entry also
  removed the shared Preview one — they were a single record — so Preview was
  re-added with the same correct value. Before this it held the broken value, so
  preview deploys were serving null prices too.)* Production was then redeployed
  from the same source (`vercel redeploy dpl_9gYqAHSc…`, so no unreviewed code
  shipped) and re-aliased to `www.ashaat.dev`; `/health` confirms a new instance
  `7fe7c1c3`.
- [x] **6.2 — Confirmed 2026-07-27.** You ran one valuation on
  `www.ashaat.dev/dcf` and the price rendered. Production log at 01:18:55 UTC:
  `finnhub.io/api/v1/quote?symbol=AAPL&token=REDACTED "HTTP/1.1 200 OK"` with
  `"price": "live"`, `finnhub_calls: 1`, `t_price_ms: 63.98` — a 200 where the
  identical call returned 401 six hours earlier. **§6 is closed.**
  *(Note for next time: your first attempt never reached production — the logs
  showed only portfolio page loads, no `/dcf`, no valuation. The `/dcf` page
  builds its endpoint from `window.location.origin`, so a tab on localhost sends
  the request to your laptop and Vercel never sees it. Same shape as the
  2026-07-13 sign-in incident.)*

**Unblocks:** the live-price feature actually being live. Nothing else is
affected — statements, quotas, auth, and the nightly refresh are all healthy.

---

## 7. Rotate `FMP_API_KEY` — the log-scrubbing fix is deployed

Your FMP key has been written to Vercel's log storage in plaintext. FMP
authenticates with `?apikey=` **in the URL**, and the `httpx` library logs every
outbound request URL — those records bypassed our scrubber, which was installed
only on the app's own logger tree. Every FMP call emitted the key: 4 per nightly
refresh, plus any cold ticker.

**The code fix is done** (2026-07-26, `app/observability.py` — the `httpx` and
`httpcore` loggers are now scrubbed; live-verified showing `apikey=REDACTED`).
That stops new leakage. It cannot un-log what is already stored, so the key
itself should be replaced.

- [x] **7.1 — Deploy the fix.** Done 2026-07-26: you committed it as `b068a96`
  and pushed, which deployed it. *(It then needed one correction — see the
  rollback note below — and production now serves `b068a96` with the corrected
  Finnhub key, instance `d1721502`.)*
- [x] **7.2 — Confirmed 2026-07-27.** The §6.2 valuation logged
  `token=REDACTED` where the 19:25 line the day before had printed the
  credential verbatim — same request, both fixes proven at once. Diagnostics
  survived redaction (`symbol=AAPL`, the `200 OK`, every telemetry field).
  The **FMP** shape has not been observed live yet — no cold ticker or nightly
  refresh has run since the deploy — but it is the same filter on the same
  logger, and the `apikey=` shape was live-verified locally. The next 6 PM
  Eastern refresh will show it.
- [ ] **7.3 — Then rotate.** FMP dashboard → issue a new key → update
  `FMP_API_KEY` in Vercel (Production **and** Preview) → redeploy → revoke the
  old key. **Order matters — do not rotate before 7.2 confirms**, or the new key
  gets logged too. Worth also rotating the Finnhub key: it was installed while
  production was still logging in the clear (low value, free tier — your call).

> ⚠️ **Rollback note, 2026-07-26 — a mistake Claude made and corrected.**
> After fixing the §6 env var, Claude ran `vercel redeploy` against the
> deployment id it had been reading logs from (`912c9de`) — but by then you had
> already pushed `b068a96`, so that redeploy **re-aliased production to the
> older source**, silently reverting the log-scrub fix in production. Two
> things to remember, because neither is obvious:
> 1. **`vercel redeploy <old-id>` is a rollback**, not a "pick up new env vars"
>    operation. To refresh env vars, redeploy whatever is *currently* production
>    — look it up first rather than reusing an id from earlier in the session.
> 2. **Env vars are snapshotted per deployment at build time.** So `vercel
>    promote` on an existing deployment would *not* have picked up the corrected
>    key — the fix had to be a fresh **rebuild** of `b068a96`, which is what was
>    done.

**Severity, honestly:** the exposure is to Vercel's log storage, which only your
account can read, and Hobby-tier runtime logs are retained for roughly an hour.
This is prudent hygiene, not an active breach — but a provider key in plaintext
logs is worth closing out properly.

**Unblocks:** nothing. It is cleanup you should do once, at your convenience.

---

## 1. Blocking now — point `ashaat.dev` at this project

The code is done, tested (298 passing), and live-verified locally: `/` serves your
portfolio, `/apis` lists your APIs, `/dcf` serves the DCF tool. It just isn't on
your domain yet. Do these **in order**.

- [x] **1.1 — Remove `ashaat.dev` from the standalone portfolio Vercel project.**
  Vercel → old portfolio project → Settings → Domains → remove `ashaat.dev`.
  *(A domain can only be attached to one Vercel project at a time, so this must
  come first.)*
- [x] **1.2 — Add `ashaat.dev` to the `pub-tools-dcf` project.**
  Vercel → `pub-tools-dcf` → Settings → Domains → Add → `ashaat.dev`. Verify DNS
  goes green. Decide whether `www.ashaat.dev` should redirect to the apex.
- [x] **1.3 — Set Production `PUBLIC_BASE_URL` to `https://ashaat.dev`.**
  **Done by Claude 2026-07-16 via `vercel env rm`/`add`.** It had never actually
  changed (still the 3-day-old vercel.app value), and — separately — the last
  deploy was 2 days old, so *no* env change could have taken effect anyway.
  Verified live: the login redirect now emits
  `redirect_to=https://ashaat.dev/v1/auth/callback`.
- [x] **1.4 — Allow-list the callback in Supabase.** You did this.
- [x] **1.4b — Deploy the Phase 9 code.** Commit `2a3b66e` pushed to `main`
  2026-07-16; production redeployed and verified: `/` portfolio, `/apis`, `/dcf`,
  `/Pics/*`, `/health`, `/docs` all 200. *(This was the real reason `/dcf` 404'd
  — the code had never been committed.)*
- [ ] **1.4c — ⚠️ Flip the primary domain to the apex.** You chose `ashaat.dev`
  as canonical, but Vercel still has **www as primary**: `ashaat.dev` 308-
  redirects to `www.ashaat.dev`. Fix: Vercel → `pub-tools-dcf` → Settings →
  Domains → set **`www.ashaat.dev` to redirect to `ashaat.dev`** (currently it's
  the reverse). *Why it matters:* the PKCE `pt_oauth_verifier` cookie is
  **host-only**. Today you browse `www`, so the cookie lands on `www`, while the
  callback goes to the apex and bounces back to `www` — it works only because the
  308 happens to preserve `?code=`. Same host everywhere removes that fragility.
- [ ] **1.5 — Test sign-in with your local server STOPPED.**
  `/` portfolio · `/apis` · `/dcf` · **GitHub sign-in** lands on `/dcf` ·
  **email magic-link** lands on `/dcf` · a valuation returns 200.
  *(Everything except sign-in is already verified live by Claude.)*
- [x] **1.6 — Retire the old portfolio Vercel project.**

> ⚠️ **1.3 and 1.4 must both be done, together.** Doing one without the other
> reproduces your 2026-07-13 outage exactly: sign-in only completes while a local
> server is running. `PUBLIC_BASE_URL` decides where Supabase sends the browser
> back to; the Supabase allowlist decides whether it's allowed to. The
> `pub-tools-dcf-nu.vercel.app` host keeps serving either way, but only the host
> named by `PUBLIC_BASE_URL` completes a sign-in round trip.

**Unblocks:** your site being publicly live on your domain; Phase 9 exit criteria.

---

## 2. Decisions I need (no dashboard — just answer)

- [x] **2.1 — The second "Course Portfolio" page.** Answered 2026-07-17: drop it,
  the page is unnecessary. The nav link was already removed; nothing else to do.
- [x] **2.2 — `www.ashaat.dev`.** Answered 2026-07-17 (owner note: "ashaat.dev now
  redirects to DCF Valuation API"). Live-verified same day: the apex still 308s to
  **www** — i.e. www remains the primary host, which is what the owner observed.
  Sign-in works through the query-preserving 308, so this stays merely a
  fragility; see 1.4c if it should ever be flipped to apex-primary.
- [x] **2.3 — Unused images.** Answered 2026-07-17: yes. Added an **AWS Certified
  Cloud Practitioner** card to the portfolio's certifications section with its
  logo. **Owner: supply the earned date (or "in progress") so the card can show
  it — no date was invented.** The other three images stay skipped:
  `GithubLogo.png`/`OutlookLogo.jpg` are non-transparent duplicates of images
  already in use, and `Java_programming_language_logo.svg.png` has no section to
  live in.
- [x] **2.4 — Phase 15's fate.** Answered 2026-07-17: keep it on hold. Recorded;
  the plan already marks it on hold with the Phase 9 supersession note, so no
  further change.

**Unblocks:** finishing Phase 9 cleanly; stopping the plan from contradicting itself.

---

## 3. Unblocks the next feature — Finnhub live price (ADR-008)

The plan is written and approved; implementation needs a key.

- [x] **3.1 — Create a free Finnhub account.** Done 2026-07-17.
- [x] **3.2 — Add `FINNHUB_API_KEY` to Vercel.** Done 2026-07-17 — confirmed via
  `vercel env ls`: present for **Preview and Production**. ⚠️ **2026-07-26: the
  variable is present but its *value* is not a working key** — Finnhub returns
  401 in production. `vercel env ls` can only prove presence, never validity, so
  this box stays closed and the value problem is tracked separately as **§6**.
- [x] **3.3 — Add `FINNHUB_API_KEY` to your local `.env`.** Done 2026-07-17
  (after a save-the-file false start). Live-verified same day: real AAPL/MSFT
  quotes fetched and normalized, unknown symbol correctly classified, and the
  full suite is immune to the ambient key (`tests/conftest.py` isolation
  fixture now clears `FINNHUB_API_KEY` too).
- [x] **3.4 — Confirm the outage default.** Resolved 2026-07-18 (with the other
  pre-implementation resolutions in `issues.MD`): the null-price degrade is
  implemented — Finnhub outage/rate-limit/unknown-symbol/misconfig all return
  the valuation math with `current_price`/`upside_pct` as `null` + a warning,
  never a cached/stale price and never a 502. Flip the "Open sub-decisions"
  entry in `issues.MD` if you ever want the 502 behavior instead.

**Unblocks:** the whole Finnhub feature (live price, never cached). Without a key
I can write the client and tests against a fake, but can't live-verify it.

---

## 4. Unblocks Phase 8 Slice C (database read-through + daily refresh)

This is the "cache → database → FMP" read path you specified, plus the 6 PM
Eastern refresh job.

> ✅ **The refresh job is confirmed working in production (checked 2026-07-26).**
> Five consecutive nightly runs — 21–25 July — all succeeded: one claim per
> ticker per Eastern date, counts reconciled, about a second each, 4 FMP calls.
> The immutable snapshot table still holds exactly one row while the ticker head
> advances daily, which is the dedup design working. Only §4.1 remains.

- [ ] **4.1 — Observe the real Redis.** ⚠️ **`vercel env pull` cannot do this**
  — tried 2026-07-26 for both Development and Production: the CLI writes the
  variable *names* with **empty values**. That is true of `SUPABASE_URL` and
  `FMP_API_KEY` too, so it is a property of the project, not of Upstash.
  **Update 2026-07-26 (later): this may now need nothing from you.** Route (b)
  is unblocked — commit `912c9de` is deployed (production `/health` reports
  `instance`), and this session read the production logs directly through the
  Vercel MCP: multiple distinct `instance` ids serving the fleet, and a real
  keyed valuation logging `redis_calls: 6` with `cache: database`. So Redis is
  demonstrably reachable and in use in production; what is left is composing the
  specific two-instance/outage observations from those logs. Note Vercel's
  runtime-log retention on Hobby is short (about an hour), so the observation
  has to be made close to the traffic that produces it.
  - **(a) Paste the two REST values into `.env` yourself** — Vercel → Storage →
    your Upstash database → REST API. Then the two-instance and outage checks
    can run locally in minutes. *(Naming note: this project's Vercel variables
    are actually `KV_REST_API_URL` / `KV_REST_API_TOKEN`, provisioned by the
    Vercel–Upstash integration; `RedisConfig.from_env()` accepts either those or
    the `UPSTASH_REDIS_REST_*` pair, so paste whichever the dashboard shows.)*
  - **(b) Let production answer it** — *recommended, and now available*. Each
    log line carries `instance` and `cache` (`l1`/`l2`/`database`/`provider`),
    so two different instances serving one ticker with a single provider load is
    directly visible on the real fleet, with no credential ever leaving Vercel.
    Strictly better evidence than a laptop pretending to be two instances.
- [x] **4.2 — Generate a `CRON_SECRET` and add it to Vercel Production.** Done
  2026-07-20 using a cryptographically random 32-byte value stored as a
  Vercel Sensitive variable. The value is not stored locally or committed.
  Commit `086572b` was pushed and its production deployment reached Ready in
  `iad1`; the first authenticated scheduled run will be the next 6 PM Eastern
  window.
- [x] **4.3 — Apply migration 003 to Supabase.** Done 2026-07-18 (after the
  Slice C push briefly left production 503ing keyed valuations — the code
  deployed before the migration; applying it restored service). Verified
  live same session: all four tables respond, both RPC guards fire, and a
  real AAPL bootstrap wrote a durable head that a second instance then
  served **with an invalid FMP key** (proof the statements came from the
  database alone).
- [x] **4.3b — Apply migration 004 to Supabase.** Done 2026-07-18. Live-verified
  with a non-mutating invalid-status call to `complete_financial_refresh_claim`:
  the deployed RPC returned its expected `invalid refresh claim status` guard.
  The current working tree may now be deployed without an RPC ordering gap.
- [x] **4.4 — Confirm current FMP/runtime capacity.** Verified 2026-07-20 for
  the current one-ticker manifest (`AAPL`): 4 normal endpoint calls and at most
  12 bounded attempts, safely below the recorded ~250-call daily allowance.
  The bounded one-ticker path also fits the current Vercel Python-function
  duration. Re-run this gate before materially increasing the ticker count.

**Unblocks:** Phase 8 Slice C; the daily refresh; live Redis verification.

---

## 4b. One decision for Phase 10 — where production keeps raw provider evidence

The capture machinery is built and tested (2026-07-25): every FMP payload is
stored gzipped, credential-redacted, collision-proof, atomic, and retention-
bounded, with `scripts/raw_captures.py` to inspect/replay/prune it. **It only
runs locally.** On Vercel the sink is deliberately off, because a function's
filesystem is not durable — so production keeps no evidence today, and Phase
10's "every snapshot traces to provider evidence" criterion stays open.

- [ ] **4b.1 — Choose the durable backend.** Options, cheapest first:
  (a) **Supabase Storage** in the project you already pay nothing for — one
  bucket, service-role-only, no new vendor; (b) **Supabase Postgres** as
  compressed rows next to `normalized_snapshots` — easiest to join, but raw
  payloads compete with your 500 MB database quota; (c) **S3/R2** — cheapest
  per GB at volume, one more account and credential to manage.
- [ ] **4b.2 — Pick a retention window.** Local default is 30 days / 25
  captures per ticker-endpoint. Evidence for a filing you still serve is worth
  keeping longer than that; say what you want and it becomes the policy.

Rough size: ~4 payloads per ticker per daily refresh, ~1 KB gzipped each with
today's fixtures (real filings are larger — order 10–50 KB each). One ticker
for a year is single-digit MB.

**Unblocks:** Phase 10 Slice 2 and its exit criterion. Nothing else waits on it.

---

## 5. Deferred — not blocking, decide when relevant

- [ ] **5.1 — Custom SMTP in Supabase.** Supabase's built-in email sender is
  rate-limited and dev-oriented. Configure real SMTP before relying on email
  magic-link login for actual customer volume.
- [ ] **5.2 — Preview environment auth.** `SUPABASE_URL`,
  `SUPABASE_SERVICE_ROLE_KEY`, and `PUBLIC_BASE_URL` exist only in **Production**,
  so preview deploys run with auth OFF and sign-in unconfigured. Decide whether
  previews should mirror production.
- [ ] **5.3 — CAPTCHA on signup.** Email magic-link login means anyone with a
  disposable inbox can create an account; the per-IP rate limit is currently the
  only mitigation. Revisit if you see abuse.
- [ ] **5.4 — Email verification before first key.** Not implemented; GitHub's own
  signup friction is the current (partial) substitute.
- [ ] **5.5 — Cross-provider account linking.** Signing in with GitHub **and**
  email using the same real identity currently creates **two separate accounts**.
  Known gap; needs a "link another sign-in method" flow.

---

## Reference — what's blocked on what

| You do | I can then do |
|---|---|
| **§6 (Finnhub key value)** | **Nothing — but production stops serving null prices** |
| §7 (deploy, then rotate `FMP_API_KEY`) | Nothing — it closes out the plaintext-key exposure |
| §1.4c + §1.5 (domain) | Close Phase 9's last exit criteria |
| ~~§2.1 (course page)~~ | ~~Migrate + restyle that page~~ Answered: dropped |
| ~~§3 (Finnhub key)~~ | ~~Build and live-verify the real-time price feature~~ Done 2026-07-18 |
| §4.1 (Redis observation) | Close the last three Phase 8 boxes — *may need nothing from you now; see the 2026-07-26 update* |
| ~~§4.2–4.4~~ | ~~Ship Phase 8 Slice C live + enable the daily refresh cron~~ Done; five clean runs observed |
| §4b (evidence backend) | Give production a durable raw-capture trail (Phase 10 Slice 2) |
| §6 of `RUNBOOKS.md` (SLOs) | Close the last Phase 11 item |
| **§9 (apply migration 005)** | **Deploy P3 — until then it must not ship** |
| Nothing | Phase 12 |

## Also worth knowing

- **Phase 9 is committed and live** (commit `2a3b66e`, deployed 2026-07-16): the
  portfolio, `/apis`, `/dcf`, and images are all serving on the domain.
- **ADR-008 + Slice C parts 1–2 are committed and deployed** (commit
  `8e30cf4`, pushed by the owner 2026-07-18; migration 003 applied the same
  day). Production runs model 0.2.0 with the live Finnhub price and the
  database read-through, live-verified against the real Supabase/FMP.
- **`CRON_SECRET` is configured in Production** as of 2026-07-20, and the
  nightly refresh has since been observed succeeding five days running
  (2026-07-21 → 2026-07-25). Nothing further is needed from you for the cron.
- **Nothing new is required from you for Phase 11.** Settings are validated at
  boot; your production variables were checked on 2026-07-25 and all pass, and
  every new setting has a default. `LOG_LEVEL`/`LOG_FORMAT` and
  `READINESS_CACHE_SECONDS` are optional.
- **Optional if you want production metrics:** add a `METRICS_TOKEN` (16+ chars)
  in Vercel and scrape `GET /internal/metrics` with
  `Authorization: Bearer <token>`. Without it the endpoint stays closed (401),
  which is the safe default — nothing else changes. Note the counters are
  per-instance, so a scrape describes whichever instance answered.
- **One decision waiting on you: SLOs.** `project-docs/RUNBOOKS.md` §6 proposes
  availability 99.5%, warm p95 < 900 ms, cold p95 < 4 s, sign-in 99%, freshness
  ≥95%/day, live price ≥98%, plus page/notice alert thresholds — all derived
  from measured numbers. Accept or edit them; they are proposals until you do,
  because an SLO is a promise about your product.
- **If you set `TRUSTED_PROXY_HOPS`,** it must match reality: Vercel is 1 (now
  the default there). Too low merges every caller into one sign-in bucket; too
  high lets a caller forge their address. Nothing to do unless your topology
  changes.
- **The log-scrubbing fix is done** (2026-07-26): the `httpx`/`httpcore` loggers
  are scrubbed, so provider keys no longer reach the logs. It needs a deploy,
  and then the key rotation in §7. See `issues.MD` → "Live production defects
  found 2026-07-26".
- **Current state 2026-07-28:** 497 tests passing, 94.88% coverage;
  ruff/format/mypy clean. The security-audit commit `1582c69` is **deployed and
  confirmed live** (production `/docs` now carries the CSP it added, and `/dcf`
  answers `Cache-Control: private, no-store` where Vercel used to say `public` —
  both are audit-only behaviors). Migrations 001–004 are applied; **005 exists
  in the tree and is not applied — see §9, it gates the next deploy.**
  Remaining after that: the Redis observation (§4.1), the §4b decision, and the
  SLO sign-off.
- Detailed context: Phase 9 in `IMPLEMENTATION_PLAN.md`, the domain checklist and
  feature definitions in `issues.MD`, decisions in `ARCHITECTURE_DECISIONS.md`
  (ADR-008 = Finnhub), session history in `PROGRESS.md`.
