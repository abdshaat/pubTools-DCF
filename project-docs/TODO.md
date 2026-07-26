# TODO — owner actions

Everything here is a step **only you can do**: dashboard access, credentials, or
a product decision. Nothing in this file is something Claude can complete alone.
Each item says what it unblocks, so you can skip sections that aren't relevant
yet.

Last updated 2026-07-25.

**Fastest path to unblocking real work:** Section 1 (deploy the site you already
have) and Section 2 (four one-line answers). Section 3 unblocks the next feature.

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
  `vercel env ls`: present for **Preview and Production**.
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

- [ ] **4.1 — Give me a way to observe the real Redis.** ⚠️ **`vercel env pull`
  cannot do this** — tried 2026-07-26 for both Development and Production: the
  CLI writes the variable *names* with **empty values**, because every variable
  in this project is stored as Sensitive (write-only). That is true of
  `SUPABASE_URL` and `FMP_API_KEY` too, so it is a property of the project, not
  of Upstash. Two ways forward, pick either:
  - **(a) Paste the two REST values into `.env` yourself** — Vercel → Storage →
    your Upstash database → REST API → `UPSTASH_REDIS_REST_URL` and
    `UPSTASH_REDIS_REST_TOKEN`. Then I can run the two-instance and outage
    checks locally in minutes.
  - **(b) Deploy the current work and let production answer it** —
    *recommended*. Each log line now carries `instance` and `cache`
    (`l1`/`l2`/`database`/`provider`), so two different instances serving one
    ticker with a single provider load is directly visible in the logs, on the
    real fleet, with no credential ever leaving Vercel. This is strictly better
    evidence than a laptop pretending to be two instances.
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
| §1 (domain) | Finish Phase 9; verify the live site end to end |
| §2.1 (course page) | Migrate + restyle that page |
| ~~§3 (Finnhub key)~~ | ~~Build and live-verify the real-time price feature~~ Done 2026-07-18 (uncommitted) |
| §4.1 (env pull) | Verify Redis against the real Upstash instance |
| §4.2–4.4 | Ship Phase 8 Slice C live + enable the daily refresh cron |
| §4b (evidence backend) | Give production a durable raw-capture trail (Phase 10 Slice 2) |
| Nothing | Phase 11 (typed settings, structured logs, metrics) can start anytime |

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
- **Current state:** 451 tests passing, 94.84% coverage; ruff/format/mypy clean.
  Slice C parts 3a–3b are committed in `a1131e2`; migration 004 is applied.
  Phase 10 Slice 1 (raw-evidence capture) landed 2026-07-25 and needs no
  deployment step — it changes only local behavior. Remaining: local/live Redis
  observation (§4.1), observing the next real cron run, and the §4b decision.
- Detailed context: Phase 9 in `IMPLEMENTATION_PLAN.md`, the domain checklist and
  feature definitions in `issues.MD`, decisions in `ARCHITECTURE_DECISIONS.md`
  (ADR-008 = Finnhub), session history in `PROGRESS.md`.
