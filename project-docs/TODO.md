# TODO — owner actions

Everything here is a step **only you can do**: dashboard access, credentials, or
a product decision. Nothing in this file is something Claude can complete alone.
Each item says what it unblocks, so you can skip sections that aren't relevant
yet.

Last updated 2026-07-16.

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
  Vercel → `pub-tools-dcf` → Settings → Environment Variables → Production →
  edit `PUBLIC_BASE_URL` → `https://ashaat.dev`. **Redeploy after saving** (env
  changes don't apply to existing deployments).
- [x] **1.4 — Allow-list the callback in Supabase.**
  Supabase → Authentication → URL Configuration → Redirect URLs → add
  `https://ashaat.dev/v1/auth/callback`. Review the Site URL while you're there.
- [ ] **1.5 — Test on `https://ashaat.dev` with your local server STOPPED.**
  Check: `/` portfolio loads with images · `/apis` lists the DCF API · `/dcf`
  loads · **GitHub sign-in** completes and lands on `/dcf` · **email magic-link
  sign-in** completes and lands on `/dcf` · a valuation request returns 200.
- [x] **1.6 — Retire the old portfolio Vercel project** once `ashaat.dev`
  resolves here and 1.5 passes.

> ⚠️ **1.3 and 1.4 must both be done, together.** Doing one without the other
> reproduces your 2026-07-13 outage exactly: sign-in only completes while a local
> server is running. `PUBLIC_BASE_URL` decides where Supabase sends the browser
> back to; the Supabase allowlist decides whether it's allowed to. The
> `pub-tools-dcf-nu.vercel.app` host keeps serving either way, but only the host
> named by `PUBLIC_BASE_URL` completes a sign-in round trip.

**Unblocks:** your site being publicly live on your domain; Phase 9 exit criteria.

---

## 2. Decisions I need (no dashboard — just answer)

- [ ] **2.1 — The second "Course Portfolio" page.** Your source repo
  (`WebstormProjects/index.html/portfolio.html`, ~8.4 KB) has a second page I did
  **not** migrate, and I dropped its nav link. Want it brought over and restyled
  to match? *(Yes / no.)*
- [ ] **2.2 — `www.ashaat.dev`.** Redirect to the apex, or ignore? *(Part of 1.2.)*
- [ ] **2.3 — Four unused images.** These exist in your source repo but nothing
  references them, so I left them out to keep the Vercel function bundle lean:
  `AWS-Certified-Cloud-Practitioner-logo.png`, `GithubLogo.png`,
  `Java_programming_language_logo.svg.png`, `OutlookLogo.jpg`. Want any added
  (e.g. an AWS cert card)?
- [ ] **2.4 — Phase 15's fate.** Phase 15 ("Separate UI/UX from the
  microservice") plans to strip **all** bundled UI out of this deployment and make
  it headless — the exact opposite of what you just built in Phase 9. It's marked
  **on hold**. Re-scope it (e.g. keep the site, split only later products) or drop
  it entirely? *(Not urgent, but the plan currently holds two opposite goals.)*

**Unblocks:** finishing Phase 9 cleanly; stopping the plan from contradicting itself.

---

## 3. Unblocks the next feature — Finnhub live price (ADR-008)

The plan is written and approved; implementation needs a key.

- [ ] **3.1 — Create a free Finnhub account** at <https://finnhub.io/register>
  and copy the API key. The free tier gives real-time US quotes at 60 calls/min,
  which is what ADR-008 assumes.
- [ ] **3.2 — Add `FINNHUB_API_KEY` to Vercel** → `pub-tools-dcf` → Settings →
  Environment Variables → **Production** (and Preview if you want previews to
  have live prices).
- [ ] **3.3 — Add `FINNHUB_API_KEY` to your local `.env`** so I can verify against
  the real API rather than only mocks.
- [ ] **3.4 — Confirm the outage default.** If Finnhub is down, the planned
  behavior is: return the valuation math with `current_price`/`upside_pct` as
  `null` + a warning (**never** a cached/stale price). Alternative is failing the
  whole request with a 502. *(Default stands unless you say otherwise.)*

**Unblocks:** the whole Finnhub feature (live price, never cached). Without a key
I can write the client and tests against a fake, but can't live-verify it.

---

## 4. Unblocks Phase 8 Slice C (database read-through + daily refresh)

This is the "cache → database → FMP" read path you specified, plus the 6 PM
Eastern refresh job.

- [ ] **4.1 — Pull the Upstash env vars locally:** run `vercel env pull` in the
  repo (or copy the two Upstash REST vars into `.env`). Redis is provisioned but
  I've never run against the real instance — only the in-memory fake.
- [ ] **4.2 — Generate a `CRON_SECRET`** (16+ random characters), add it to Vercel
  **Production**, and redeploy. This protects the internal refresh endpoint. Do
  **not** commit it or expose it to the browser.
- [ ] **4.3 — Apply migration 003 to Supabase** when I hand it to you — same flow
  as migrations 001/002 (paste the SQL into the Supabase SQL editor). It creates
  the immutable snapshot table + ticker heads. *(I'll tell you when it's ready.)*
- [ ] **4.4 — Confirm FMP plan capacity.** The daily job refreshes **every**
  ticker in the database, with several FMP endpoint calls each plus retries. Your
  current FMP tier is ~250 calls/day. Confirm the plan (or upgrade) before the
  cron is enabled — the design says insufficient capacity must fail visibly rather
  than silently refresh fewer tickers.

**Unblocks:** Phase 8 Slice C; the daily refresh; live Redis verification.

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
| §3 (Finnhub key) | Build **and live-verify** the real-time price feature |
| §4.1 (env pull) | Verify Redis against the real Upstash instance |
| §4.2–4.4 | Build and ship Phase 8 Slice C + the daily refresh cron |
| Nothing | Write Slice C code/tests against fakes (can start anytime) |

## Also worth knowing

- **Nothing is committed yet.** The Phase 9 site work, the restyled portfolio,
  `docs/apis.html`, `docs/Pics/`, the plan/ADR updates — all uncommitted. Say the
  word and I'll commit and push (which triggers a Vercel production deploy via the
  git integration).
- **Current state:** 298 tests passing, 94.07% coverage, ruff/format/mypy clean.
- Detailed context: Phase 9 in `IMPLEMENTATION_PLAN.md`, the domain checklist and
  feature definitions in `issues.MD`, decisions in `ARCHITECTURE_DECISIONS.md`
  (ADR-008 = Finnhub), session history in `PROGRESS.md`.
