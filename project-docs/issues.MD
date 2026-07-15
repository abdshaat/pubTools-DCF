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
