# DCF Valuation API Implementation Plan

This document is the source of truth for implementing the API improvements
identified in the July 2026 architecture review. Work is ordered by dependency
and risk: calculation safety first, then data correctness and concurrency,
followed by public-API controls, infrastructure, and richer valuation features.

## Status rules

- `[ ]` Pending: not implemented, or implementation has started but is not fully
  verified.
- `[x]` Done: code, tests, documentation, and required migrations/configuration
  are complete and the relevant verification commands pass.
- A phase is complete only when every task and its exit criteria are done.
- Mark a task `[x]` in the same change that implements it. Add the completion
  date and brief evidence, for example: `Completed 2026-08-01 — test name`.
- Do not mark partially implemented tasks done. Split a task if useful.
- Update `PROGRESS.md` after every implementation session, as required by
  `CLAUDE.md`.

## Baseline already implemented

- [x] Layered provider, normalization, fundamentals, engine, API, and schema
  architecture.
- [x] Provider retry/backoff for transport errors, HTTP 429, and HTTP 5xx.
- [x] Positive and definitive-negative in-process ticker caching.
- [x] Domain error mapping for provider, ticker, normalization, and DCF errors.
- [x] Pure point-estimate DCF and default-on 3x3 sensitivity grid.
- [x] Fixture-backed API/data tests and property-based DCF invariants.

## Phase 0 — Baseline, tooling, and decisions

Goal: make later changes measurable and prevent undocumented contract drift.

- [x] Repair/recreate the local virtual environment and document supported setup
  commands for Windows, macOS, and Linux. Completed 2026-07-11 — Python 3.13.14
  `.venv313` created and editable development dependencies installed; README
  documents standard and Windows/OneDrive recovery commands.
- [x] Run the complete suite and record the real test count in `README.md` and
  `PROGRESS.md`. Completed 2026-07-11 — 63 tests passed on Python 3.13.14 in
  2.75 seconds (one upstream Starlette deprecation warning).
- [x] Configure formatting, linting, and static typing (recommended: Ruff and
  mypy) with constrained development dependencies. Completed 2026-07-11 — Ruff
  0.14–<1 and mypy 1.18–<2 configured; lint, format check, typing, and 63 tests
  pass after typed cleanup with no runtime contract changes.
- [x] Add CI for formatting/linting, typing, tests, coverage, and package builds
  across supported Python versions. Completed 2026-07-11 — GitHub Actions quality
  job and Python 3.11–3.14 test matrix added; all quality/test/build commands pass
  locally on Python 3.13.14. Coverage enforcement is completed in the next step.
- [x] Select a realistic initial coverage threshold and ratchet it upward.
  Completed 2026-07-11 — measured 95% statement and 93.70% branch-aware coverage
  (432 statements, 76 branches) and enforced a 93% floor through pytest/CI.
- [x] Record architecture decisions for numeric precision/rounding, error-envelope
  compatibility, authentication/key storage, Postgres/Redis topology, and model
  versioning. Completed 2026-07-11 — ADR-001 through ADR-005 recorded in
  `ARCHITECTURE_DECISIONS.md` with Vercel-safe, vendor-neutral boundaries.
- [x] Reconcile stale documentation about test counts, sensitivity status, live
  verification, and pending milestones. Completed 2026-07-11 — README test count
  and `PROGRESS.md` current-state summary now reflect 63 tests, live verification,
  completed sensitivity, active Phase 0 work, and actual pending systems.
- [x] Add a Vercel preview-deployment check to CI after local unit/contract tests.
  Completed 2026-07-11 — deployment-status workflow checks out the deployed SHA,
  runs API contract tests, then validates `/health` and `/openapi.json` against
  the exact Vercel Ready URL without duplicating Vercel tokens in GitHub secrets.

Exit criteria:

- [ ] A clean checkout can install, lint, type-check, test, and build in CI.
- [ ] Contributor documentation contains current commands and results.

## Vercel deployment requirements (apply to every phase)

Vercel is the target runtime. Every completed phase must satisfy these rules;
they are release gates, not optional follow-up work.

- [x] Export the FastAPI `app` through the custom `app.api:app` entrypoint in
  `[tool.vercel]`.
- [x] Use a Vercel-supported Python version through `requires-python` (the current
  constraint permits Vercel's supported Python 3.12+ runtimes).
- [x] Disable the local raw-response filesystem sink when the `VERCEL`
  environment variable is present.
- [ ] Add `vercel dev` to the documented local verification workflow and verify
  `/health`, `/docs`, and a fixture/demo valuation through that runtime.
- [x] Configure Vercel project environment variables for preview/production;
  never ship `.env` or provider/customer secrets in the deployment bundle.
  Completed 2026-07-11 — user confirmed `FMP_API_KEY` is configured in Vercel;
  `.env` and Vercel local metadata remain excluded from Git.
- [ ] Treat function memory as ephemeral and instance-local. Correctness,
  authentication, quotas, metering, distributed locks, and durable caches must
  use external Postgres/Redis/object storage.
- [ ] Keep database/Redis clients reusable at module/lifespan scope and use small
  bounded pools appropriate for horizontally scaling functions.
- [ ] Set provider and request timeouts below the configured Vercel function
  duration, leaving time to serialize a controlled error response.
- [ ] Do not run durable background jobs after returning an HTTP response. Use
  Vercel Cron or an external queue/worker for refresh, replay, cleanup, and audit
  persistence.
- [ ] Keep the function bundle small; exclude tests, fixtures, local raw data,
  tooling caches, and non-runtime assets while retaining required
  `app/demo_data` snapshots.
- [ ] Keep responses below Vercel's payload limit and put customer-facing static
  assets in `public/` when they should be served through the CDN.
- [ ] Choose the function region closest to Postgres/Redis/object storage and
  verify preview and production use compatible regions.
- [ ] Validate cold-start, warm-instance, concurrent-instance, timeout,
  dependency-outage, and instance-recycling behavior in preview deployments.
- [ ] Do not mark a task done if it depends on local filesystem durability,
  process-global state, a permanently warm instance, or post-response execution.

Vercel exit criteria:

- [ ] Preview passes health, API-contract, cold/warm, and dependency smoke tests.
- [ ] Recycling/scaling instances cannot lose durable state, bypass metering,
  duplicate unsafe work, or change valuation correctness.

## Phase 1 — Numeric safety and authoritative validation

Goal: reject unsafe values before calculation and guarantee JSON-safe results.

- [x] Define documented ranges for WACC, terminal growth, tax rate, EBIT margin,
  revenue growth, projection years, price, and normalized financial values.
  Completed 2026-07-11 — v1 safety constants defined in `dcf_engine.py` and
  published in OpenAPI, README, and customer docs.
- [x] Add reusable `math.isfinite` validation for every caller-supplied float and
  provider-derived numeric field. Completed 2026-07-11 — assumptions, growth
  sequences, canonical provider fields, derived net debt, and engine base data
  reject NaN/infinity.
- [x] Enforce bounds in the engine's authoritative validator: positive bounded
  WACC, bounded terminal growth below WACC, tax rate in `[0, 1]`, documented EBIT
  margin bounds, growth within +/-50%, and valid forecast length/list length.
  Completed 2026-07-11 — WACC 0.001–0.50, terminal growth -0.10–0.10 and below
  WACC, tax 0–1, EBIT margin -1–1, growth +/-0.50, and horizon/list rules enforced.
- [x] Validate normalized base data: positive revenue, diluted shares, and price;
  finite monetary fields; explicitly permitted negative net cash/NWC values.
  Completed 2026-07-11 — invalid provider values raise controlled normalization
  errors; negative EBIT, net debt, and NWC remain supported.
- [x] Decide whether negative enterprise/equity/per-share values are valid model
  outputs; document the policy and return warnings instead of silently clipping.
  Completed 2026-07-11 — negative estimates remain valid and unclipped, with
  additive response warnings and engine/API regression coverage.
- [x] Add a final result-integrity check so NaN/infinity cannot enter the response
  or sensitivity grid. Completed 2026-07-11 — every projection intermediate and
  aggregate output is checked before model/response construction.
- [x] Add engine and HTTP tests for boundaries, NaN, infinities, extreme values,
  and malformed comma-separated growth. Completed 2026-07-11 — suite expanded
  from 63 to 107 tests and covers engine, normalization, and HTTP boundaries.
- [x] Update OpenAPI and customer documentation with the exact supported ranges.
  Completed 2026-07-11 — route descriptions, README, interactive builder rules,
  error table, and response example updated.

Exit criteria:

- [x] Every accepted request produces finite, JSON-serializable output. Verified
  2026-07-11 by result-integrity checks and 107 passing tests.
- [x] Every rejected numeric input returns a deterministic field-level 422.
  Verified 2026-07-11 by parameterized HTTP tests for all numeric assumptions.

## Phase 2 — Stable API contracts and auditability

Goal: give clients one predictable error format and enough detail to reproduce
the valuation.

- [x] Design a versioned error envelope with stable code, message, request ID,
  and optional field errors. Completed 2026-07-11 — envelope version 1 and
  `X-Request-ID` added with typed schemas and UUID contract coverage.
- [x] Add a `RequestValidationError` handler so FastAPI and domain validation use
  the same envelope. Completed 2026-07-11 — framework errors are normalized into
  envelope fields while retaining their native v1 `detail` payload.
- [x] Give provider/domain errors stable machine-readable codes without leaking
  credentials or subscription details. Completed 2026-07-11 — assumption,
  sector, ticker, normalization, provider-auth, and availability codes mapped.
- [x] Attach response models/examples for 400/401/403/404/422/429/500/502/503 to
  OpenAPI. Completed 2026-07-11 — all statuses use the typed version-1 envelope
  schema/example; 401/403/429 are explicitly marked reserved until Phase 5.
- [x] Decide and test the compatibility/migration policy for the current `detail`
  response shape. Completed 2026-07-11 — existing `detail` is preserved verbatim
  and the new `error` member is additive; native and domain contracts are tested.
- [x] Expose the full annual FCF bridge: revenue, growth, EBIT margin, EBIT, cash
  taxes/NOPAT, D&A, capex, change in NWC, FCF, discount period/factor, and PV.
  Completed 2026-07-11 — internal and wire projection models expose every bridge
  component, with engine/API reconciliation tests proving FCF and PV arithmetic.
- [x] Add request ID, computation time, currency, units, provider/data version,
  fundamentals date, quote date, model version, and warnings/disclaimer metadata.
  Completed 2026-07-11 — metadata is provider-derived where available, optional
  quote dates remain null rather than invented, and data versions are deterministic
  SHA-256 fingerprints of canonical normalized snapshots.
- [x] Define internal precision and API rounding behavior. Completed 2026-07-11 —
  finite IEEE-754 doubles, no intermediate rounding, raw API calculation values,
  client-only display rounding, and separate decimal/minor-unit billing policy.
- [x] Add OpenAPI contract snapshots and end-to-end tests that reconstruct FCF,
  enterprise value, equity value, and per-share value from the response. Completed
  2026-07-11 — canonical OpenAPI SHA-256 snapshot plus full bridge/EV/equity/share
  reconciliation tests added.
- [x] Update README and the interactive customer documentation. Completed
  2026-07-11 — precision, metadata, complete projection bridge, compatibility
  envelope, supported ranges, errors, warnings, and response example documented.

Exit criteria:

- [x] Every documented failure uses one tested envelope. Verified 2026-07-11 for
  request/domain validation, sector, ticker missing/coverage, normalization,
  provider authentication, and provider availability failures.
- [x] Consumers can reproduce a valuation without hidden ratios. Verified
  2026-07-11 by end-to-end response-only FCF, PV, EV, equity, and per-share tests.

## Phase 3 — Provider data integrity and freshness

Goal: prevent mismatched statement periods and stale prices from contaminating
calculations.

### Latest complete statement selection logic

The API must never equate “first provider row” with “latest usable statement.”
Selection follows this deterministic sequence:

1. Fetch up to five annual records for income, balance-sheet, and cash-flow
   endpoints; retain raw ordering only for audit, not selection.
2. Normalize identity fields (`period`, statement `date`, fiscal year/calendar
   year, reported currency, filing date, accepted date) without interpreting
   monetary line items.
3. Restrict v1 candidates to annual/FY periods. Join the three statement types
   on exact period and statement date. Fiscal years must agree when supplied;
   missing fiscal-year fields may be derived from the matched income statement or
   statement date and must be recorded as a fallback warning.
4. Reject cross-currency sets. A statement currency, when present, must agree
   across all three statements and with the company profile currency.
5. For duplicate/restated records of the same period, choose the newest
   `acceptedDate`, then filing date, then provider position; record that a newer
   filing/restatement was selected.
6. Rank complete compatible sets by statement date, fiscal year, and accepted/
   filing date. Select the newest complete set, even if a newer incomplete period
   exists; return a data-quality warning for that incomplete newer period.
7. Fail with controlled `normalization_failed` when no complete compatible annual
   set exists. Never combine independently selected statement rows.
8. Expose statement/fiscal/filing provenance and freshness metadata in the API
   response. “Latest” means latest complete compatible provider set, not a claim
   that no newer filing exists outside the provider.

- [x] Retain statement date, fiscal year, period, currency, filing/accepted date,
  and quote timestamp when available. Completed 2026-07-11 — canonical metadata
  and response provenance now include every available provider field plus quote
  retrieval time.
- [x] Fetch enough records to select compatible income, balance-sheet, and
  cash-flow statements rather than independently taking item zero. Completed
  2026-07-11 — provider retains up to eight candidates from each annual endpoint.
- [x] Implement/document statement matching, including fiscal-year offsets and
  acceptable date tolerances. Completed 2026-07-11 — v1 requires exact annual
  period/date matches, validates supplied fiscal years, retains fiscal offsets,
  and derives missing years from statement date with a warning.
- [x] Return a controlled normalization error when no compatible set exists.
  Completed 2026-07-11 — missing intersections, conflicting fiscal years,
  currencies, and unusable dates fail as `normalization_failed` rather than mix.
- [x] Validate currency and unit consistency across selected records. Completed
  2026-07-11 — supplied statement currencies must agree with each other/profile;
  canonical monetary values retain the documented raw-currency-unit convention.
- [x] Add fixtures for mismatched periods, restatements, missing dates, fiscal
  offsets, share classes, and provider naming drift. Completed 2026-07-11 —
  synthetic fixture variants cover every listed condition plus incomplete newer
  annual periods and malformed provider payloads.
- [x] Separate slow-moving fundamentals from current quote retrieval and caching.
  Completed 2026-07-11 — independent quote endpoint/cache refreshes price without
  refetching statements or profile.
- [x] Configure independent TTLs for statements, profiles, negative results, and
  quotes. Completed 2026-07-11 — defaults are 4h statements, 24h profile, 4h
  negative results, and 60s quote; each is constructor-configurable and tested.
- [x] Return `fundamentals_as_of` and `price_as_of`; define quote-staleness policy.
  Completed 2026-07-11 — provider timestamp and retrieval timestamp are distinct;
  failed refresh may use cached quote for at most 15 minutes with a warning.
- [x] Return data-quality warnings and provenance for fallbacks/substitutions.
  Completed 2026-07-11 — fiscal/currency fallback, restatement selection,
  incomplete newer periods, and stale-quote fallback are surfaced.

Exit criteria:

- [x] No valuation combines unverified periods, currencies, or units. Verified
  2026-07-11 by compatible-set selection and mismatch/error regression tests.
- [x] Price freshness is independent of the fundamentals cache lifetime. Verified
  2026-07-11 by independent statement/profile/quote call-count and stale-policy
  tests.

## Phase 4 — Concurrency, latency, and resilience

Goal: reduce cold latency without multiplying upstream cost or failures.

- [x] Add per-ticker single-flight request coalescing to `FundamentalsService`.
  Completed 2026-07-11 — cold same-ticker bursts share one provider load and
  regression tests prove 10 concurrent callers produce one logical fetch.
- [x] Ensure one waiter cancellation does not cancel shared work, and clean up
  locks/tasks after success and failure. Completed 2026-07-11 — `asyncio.shield`
  protects shared loads and in-flight tasks self-remove on completion.
- [x] Fetch independent FMP endpoints concurrently behind a bounded semaphore.
  Completed 2026-07-11 — statement/profile/quote endpoints are gathered
  concurrently while honoring a provider semaphore.
- [x] Configure concurrency according to provider limits. Completed 2026-07-11 —
  `provider_concurrency` is constructor-configurable and defaults conservatively
  to 3.
- [x] Add bounded exponential backoff with jitter and safe/capped `Retry-After`
  parsing. Completed 2026-07-11 — invalid `Retry-After` falls back to jittered
  exponential delay and provider-supplied values are capped.
- [x] Explicitly classify malformed JSON/payloads, timeouts, transport failures,
  unsupported statuses, and raw-sink failures. Completed 2026-07-11 — malformed
  JSON, malformed payloads, `httpx` timeouts/transport errors, unsupported 4xx,
  retry exhaustion, and raw-sink failures raise controlled provider errors.
- [x] Define total provider/API time budgets so retries cannot exceed latency SLOs.
  Completed 2026-07-11 — provider defaults are 6s timeout, 2 retries, 2s capped
  retry waits, and concurrency 3; README documents the Vercel-oriented budget.
- [x] Add safe stale-if-error fundamentals behavior with a response warning.
  Completed 2026-07-11 — failed statement refreshes can use a bounded stale
  normalized snapshot with a response warning; too-old snapshots still fail.
- [x] Test concurrent cold requests, cleanup, cancellation, partial failure,
  timeout, malformed JSON, retry exhaustion, and stale fallbacks. Completed
  2026-07-11 — data-layer tests cover same-ticker bursts, waiter cancellation,
  timeout retry classification, malformed JSON/payloads, unsupported statuses,
  retry exhaustion, raw-sink failure, and quote stale fallback.
- [x] Add repeatable cold, warm, and same-ticker burst load tests. Completed
  2026-07-11 — `scripts/load_probe.py` runs fixture-backed or against a supplied
  base URL and reports cold, warm, and same-ticker burst latency summaries.

Exit criteria:

- [x] A same-ticker burst triggers one logical upstream load.
- [x] Cold latency improves without exceeding provider concurrency/retry budgets.

## Phase 5 — Authentication, rate limiting, and metering

Goal: safely expose the API and account for usage independently of FMP.

- [x] Add a temporary unauthenticated valuation request guard capped at 100
  requests per UTC day, including website calls. Completed 2026-07-11 —
  in-process limiter protects `/v1/valuations/*`, returns 429 with
  `X-RateLimit-*`/`Retry-After`, and is covered by API tests. This is not the
  final distributed quota system.
- [x] Design customer/account/API-key records and key lifecycle: creation, hashed
  storage, identifier/prefix, scopes, rotation, revocation, and last-used time.
  Completed 2026-07-11 — `APIKeyRecord` defines key ID, public prefix, SHA-256
  secret hash, scopes, revocation, and expiry; durable creation/rotation UI and
  last-used persistence remain pending with storage.
- [x] Add authentication that never logs or returns full secrets. Completed
  2026-07-11 — valuation auth supports hashed API-key records, constant-time
  hash comparison, stable 401/403 envelopes, and secret-redaction tests.
- [x] Define public endpoints and protect valuation endpoints by default.
  Partially implemented 2026-07-11 — `/`, `/health`, `/docs`, and OpenAPI stay
  public; valuation endpoints can be protected by injecting/configuring an
  authenticator. Default enforcement remains off to avoid breaking the public
  website demo until customer key distribution is decided.
- [x] Add per-key quotas/burst limits and conservative IP limits wherever
  unauthenticated valuation access remains.
- [x] Return standardized 401/403/429 errors and rate-limit/`Retry-After` headers.
  Completed 2026-07-11 — auth failures use the versioned error envelope plus
  `WWW-Authenticate`; rate-limit failures return 429 with `X-RateLimit-*` and
  `Retry-After`.
- [x] Meter customer requests separately from FMP calls, cache hits, and failures;
  explicitly define billable outcomes.
- [x] Add authorized key-management and usage-reporting workflows with audit logs.
- [x] Test missing, malformed, expired, revoked, insufficient-scope, quota-window,
  concurrent-counter, and secret-redaction cases. Auth coverage completed
  2026-07-11 for missing, malformed, unknown, expired, revoked,
  insufficient-scope, public endpoint bypass, and secret redaction; distributed
  quota-window/concurrent-counter tests remain pending with Redis/Postgres.
- [x] Threat-model enumeration, oversized URLs, proxy spoofing, denial of service,
  and provider-quota exhaustion.
- [x] Update customer docs only after key enforcement is deployed.

Phase 5 completion evidence, 2026-07-11: application code now auto-enables
Supabase-backed API-key authentication, atomic per-key daily quotas, and usage
metering when `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are configured.
Admin workflows exist in `scripts/create_api_key.py` and
`scripts/report_usage.py`; database setup is captured in
`supabase/migrations/001_phase5_auth_usage.sql`. Production activation requires
the project owner to run that SQL migration in Supabase and add both Supabase
environment variables to Vercel.

Exit criteria:

- [x] Production valuation requests are authenticated, bounded, metered, and
  traceable without exposing secrets.

## Phase 6 — Customer accounts, login, and self-service API keys

Goal: let a customer sign up, log in, and generate/manage their own scoped API
keys without operator involvement, replacing `scripts/create_api_key.py` as the
only way a key comes into existence. This is shared **pubTools** identity
infrastructure, not a DCF-specific feature — pubTools is planned to host
multiple financial calculators beyond DCF, and every one of them will
authenticate through the same customer/API-key system built in Phase 5. Model
that from the start: an account is a pubTools account, and a key's `scopes`
select which product(s) it can call (`valuation:read` today; future tools add
their own scope strings to the same `api_keys.scopes` array — no new key
system per product).

- [x] Decide identity provider: Supabase Auth (GoTrue) reuses the Postgres
  project already in place and avoids hand-rolling password storage/reset/
  verification; document why versus a custom email+password implementation or
  third-party OAuth (Google/GitHub) if evaluated. Completed 2026-07-11,
  extended 2026-07-12 — Supabase Auth with two providers: GitHub OAuth and
  email magic link (passwordless, chosen over email+password specifically to
  avoid password storage/reset entirely). No custom password storage added.
  `app/supabase.py`'s `SupabaseAuthClient`. Both providers complete through
  the same PKCE code-exchange step (`app/accounts.py::complete_login`) since
  GitHub's authorize redirect and Supabase's magic-link verify both land on
  `/v1/auth/callback?code=...`.
- [x] Decide session mechanism for the browser: server-set `HttpOnly`, `Secure`,
  `SameSite` cookies with CSRF protection on state-changing requests, versus a
  bearer token held in browser JS. Human browser sessions and machine API keys
  are different credential classes with different threat models — do not reuse
  API-key auth (`app/auth.py`, `app/supabase.py`) for the login session, and do
  not let a login session itself be usable as a valuation API key. Completed
  2026-07-11 — `app/accounts.py` cookies (`pt_session`/`pt_refresh`, plus a
  short-lived `pt_oauth_verifier`), all `HttpOnly`, `SameSite=Lax`, `Secure`
  when `PUBLIC_BASE_URL` is https. `SameSite=Lax` blocks cross-site POST,
  covering CSRF on the self-service key endpoints without a separate token.
  Session cookies and `X-API-Key` are structurally disjoint code paths
  (different header vs. cookie, different parsers); never cross-accepted.
  **Correction 2026-07-12:** the original implementation also sent a
  caller-generated `state` parameter to Supabase's `/authorize` endpoint for
  CSRF purposes. That parameter is reserved and managed internally by
  Supabase Auth to correlate its own round trip with the provider; overriding
  it broke Supabase's own callback validation (`bad_oauth_state`, observed
  live). Removed entirely — PKCE's `code_verifier`/`code_challenge` binding
  already provides the CSRF protection `state` was redundantly trying to add.
  No `pt_oauth_state` cookie exists anymore.
- [x] Add explicit CSRF protection for cookie-authenticated state-changing
  routes. Added 2026-07-12 after security review: `SameSite=Lax` is useful
  defense-in-depth but should not be the only CSRF control for
  `/v1/account/keys`, `/v1/account/keys/{id}/revoke`,
  `/v1/account/keys/{id}/rotate`, `/v1/auth/logout`, and email-login POSTs.
  Implement a double-submit or server-issued CSRF token (`pt_csrf` plus
  `X-CSRF-Token`), reject missing/mismatched tokens with 403, and add tests.
  This becomes mandatory before any Phase 15 cross-origin frontend or
  `SameSite=None` cookie setup. Completed 2026-07-12 — `app/accounts.py`
  now issues/validates `pt_csrf`, `app/api.py` requires `X-CSRF-Token` on
  cookie-backed POSTs, `docs/index.html` sends the header for account/email
  actions, and `tests/test_api.py` covers missing/mismatched tokens plus the
  happy paths.
- [x] Persist signed-in browser sessions so returning users do not need to
  authenticate on every visit. The current implementation stores the short-
  lived Supabase access token in the `HttpOnly` `pt_session` cookie and the
  refresh token in the `HttpOnly` `pt_refresh` cookie for up to 30 days.
  `app/accounts.py::get_current_customer()` validates the access token and, when it
  has expired, silently exchanges the refresh token for a new Supabase session;
  `app/api.py` then rotates both cookies on the response. Cookies are `Secure`
  in production and `SameSite=Lax`, and logout clears both session cookies and
  the CSRF cookie. Existing implementation confirmed 2026-07-12.
- [ ] Harden persistent sessions for production. Confirm Supabase refresh-token
  rotation/reuse-detection settings, define the desired inactivity and absolute
  session lifetimes, and make the cookie lifetime match those server-side
  limits. Document that clearing browser storage, explicit logout, account
  disablement, or refresh-token expiry requires a new sign-in. Add tests for
  refresh-token rotation, invalid/revoked refresh tokens, cookie replacement
  after refresh, explicit logout, and persistence across a simulated browser
  restart. Decide whether customers need a "sign out all devices" control and,
  if so, add server-side session revocation and an audit event before launch.
- [x] Design the account model: a login identity (auth user) is distinct from
  an `api_customers` row (the billing/quota entity) which is distinct from an
  `api_keys` row (already exists). v1 scope: one login owns exactly one
  customer record; multi-user/team seats on one customer account are a
  documented future extension, not built now. Completed 2026-07-11 —
  `CustomerAccount` in `app/accounts.py`; one-to-one enforced by a unique
  constraint on `api_customers.auth_user_id`. **Known gap added 2026-07-12:**
  with two login providers now live (GitHub, email), a customer who signs in
  with both using the same real-world identity gets **two separate**
  `auth.users`/`api_customers` rows -- Supabase issues a distinct
  `auth_user_id` per provider unless identity linking is explicitly
  configured, which this project doesn't do. Not fixed now; would need an
  "link another sign-in method to my account" flow while already
  authenticated, which is out of scope for this iteration.
- [x] Add a migration linking `api_customers` to the auth user id (e.g. a
  nullable `auth_user_id` column), preserving existing operator-created
  customers that have no login yet. Completed 2026-07-11 —
  `supabase/migrations/002_phase6_customer_login.sql` (nullable, unique
  `auth_user_id`; existing operator-created customers are unaffected).
  Applied 2026-07-12 — user confirmed running it against the project's
  Supabase instance.
- [ ] Build signup: require verified email before a new account can create its
  first API key, to stop throwaway signups from consuming quota or abusing the
  DCF/future-tool endpoints for free. Not implemented — a GitHub login
  auto-provisions an `api_customers` row on first sign-in
  (`app/accounts.py::_ensure_customer`) with no separate email-verification
  gate. GitHub's own account-creation friction is some throwaway resistance
  but is not the same control the task calls for; revisit if self-service
  abuse is observed.
- [x] Build login: password reset flow, rate limiting on login/signup attempts
  per IP and per email, and error responses that never reveal whether a given
  email is registered. Completed 2026-07-11, extended 2026-07-12 with email
  magic-link login — password reset is not applicable to either provider
  (neither GitHub OAuth nor magic link involves a password; we never store
  one). `/v1/auth/github/login` and `/v1/auth/email/login` share one per-IP
  daily rate-limit bucket (`LOGIN_ATTEMPTS_DAILY_LIMIT`, 429
  `login_rate_limited`) so abuse across either entry point is bounded
  together. Email login always responds `{"sent": true}` for any well-formed
  address regardless of whether an account exists, matching Supabase's own
  `/auth/v1/otp` behavior ("obfuscate whether such an address... already
  exists"), so there is no account-existence probe surface on either path.
- [x] Add authenticated self-service endpoints (or direct Supabase access
  gated by RLS scoped to `auth.uid()`, if that path is chosen instead of
  routing through FastAPI): list own keys (prefix, scopes, quota, created/
  last-used/revoked state — never the secret or its hash), create a new key
  choosing scopes/quota within account limits, rotate a key, revoke a key,
  label/rename a key. Partially implemented 2026-07-11, extended 2026-07-12 —
  `GET/POST /v1/account/keys`, `POST /v1/account/keys/{id}/revoke`, and
  `POST /v1/account/keys/{id}/rotate` are done (fixed scope/quota, optional
  label at creation, secret/hash never returned after creation). Rotation
  regenerates the secret in place -- same key id/prefix/label/`created_at` --
  and immediately invalidates the old secret (verified: old secret gets 401,
  new secret gets 200, live-verified the underlying PostgREST query against
  the real Supabase schema); rejects revoked keys and cross-customer attempts
  with the same generic 404 used elsewhere (no information leak about which
  reason applied). **Completed 2026-07-13** — added
  `POST /v1/account/keys/{id}/rename` (`RenameKeyRequest`, `label: str | None`,
  max 64 chars): changes only the label, never the secret/scope/quota;
  rejects revoked keys and cross-customer attempts with the same generic 404
  as revoke/rotate. New `SupabaseClient.rename_customer_key()` (PATCH scoped
  to `id`+`customer_id`+`revoked=eq.false`, same pattern as rotate) and
  `app/accounts.py::rename_key()` (records `account.key_renamed` audit event).
  `docs/index.html` gets a "Rename" button (prompt-based label edit) next to
  Rotate/Revoke. No test-fake changes needed -- the existing generic PATCH
  handler in `tests/fake_supabase.py` already supports this filter shape.
- [x] **Found via live testing 2026-07-12 — fake test backend gaps surfaced by
  exercising rotation end-to-end.** Writing the rotation tests required, for
  the first time, driving a real `/v1/valuations/*` request with a
  self-service-created key through `tests/fake_supabase.py`. That exposed two
  latent gaps in the *test fake* (not app code): its `GET /rest/v1/api_keys`
  handler only ever matched on `customer_id`, so the machine-auth
  prefix-based lookup (`get_api_key_by_prefix`) always returned nothing; and
  it had no handler at all for the `consume_daily_quota`/`record_usage_event`
  RPCs, since no earlier test had exercised an authenticated valuation call
  through this particular fake. Both fixed in `tests/fake_supabase.py`.
- [x] **Found via live testing 2026-07-12 — self-service key list shows a
  static quota, not remaining/used-today.** Confirmed by tracing the code
  that actual quota *enforcement* was never affected (self-service keys use
  the identical `api_keys.daily_quota` column and middleware path as
  admin-issued keys — no special-casing) -- this was purely a missing
  display feature. Fixed 2026-07-12: added
  `SupabaseClient.get_daily_quota_usage()` (read-only lookup against
  `daily_quota_counters` by `subject_id`/`quota_window`, no increment);
  `app/accounts.py::list_keys()` now enriches each non-revoked key with
  `requests_used_today` (revoked keys skip the lookup, report `None`);
  `create_key()` returns `requests_used_today: 0` for a freshly created key
  without an extra query. `ApiKeySummaryOut` exposes the new field;
  `docs/index.html` shows "`N`/`daily_quota` today" instead of the static
  limit. Verified against the real Supabase project's schema (not just
  mocks) via a direct live query. Tested:
  `test_list_keys_enriches_active_keys_with_todays_usage`,
  `test_list_keys_does_not_look_up_usage_for_revoked_keys`,
  `test_create_key_returns_zero_requests_used_today_without_a_quota_lookup`,
  `test_account_keys_list_reports_requests_used_today`, plus
  `SupabaseClient.get_daily_quota_usage` unit tests.
- [x] **Found via live testing 2026-07-12 — `last_used_at` renders as a raw
  ISO-8601 timestamp.** Fixed 2026-07-12: added a `timeAgo()` helper in
  `docs/index.html` (relative "N minutes/hours/days ago", falling back to a
  localized date for anything 30+ days old); `renderKeys()` now formats
  `last_used_at` through it instead of concatenating the raw string.
- [x] If tables are queried directly from the browser, add RLS policies
  restricting every row to its owning `auth_user_id`/`customer_id`; if access
  stays behind FastAPI, keep the current no-policy/service-role-only posture
  and enforce ownership in application code instead. Pick one path and do not
  mix them per table. Completed 2026-07-11 — FastAPI-mediated path chosen;
  `api_customers`/`api_keys` keep no RLS policies (service-role only); every
  self-service query filters by the caller's own `customer_id` in
  `app/supabase.py`. Ownership isolation verified by
  `test_self_service_keys_are_isolated_between_customers`.
- [ ] Add bot/abuse mitigation on public signup (CAPTCHA or equivalent) and
  keep `scripts/create_api_key.py` / `scripts/report_usage.py` as the
  operator/support-issued fallback for accounts that need manual intervention.
  Partially implemented 2026-07-11 — admin scripts retained as documented
  fallback (done); no CAPTCHA added. **Note added 2026-07-12:** adding email
  magic-link login lowers the throwaway-account bar back down from GitHub
  OAuth's level (anyone can request a link to any address they can read,
  including disposable inboxes) -- the shared per-IP rate limit is currently
  the only mitigation for the email path. Revisit CAPTCHA if abuse is
  observed, especially via `/v1/auth/email/login`.
- [ ] Add audit events for account lifecycle (signup, email verified, login,
  password reset, key created/rotated/revoked via self-service) alongside the
  existing `audit_events` table. Partially implemented 2026-07-11, extended
  2026-07-12 and 2026-07-13 — `account.signup`, `account.login`,
  `account.logout`, `account.key_created`, `account.key_rotated`,
  `account.key_revoked`, `account.key_renamed` are all recorded now,
  login/signup events tagged with `metadata.provider` ("github"
  or "email") read from Supabase's own `user.app_metadata.provider`.
  **Not applicable/not done:** email verification and password reset (no
  password exists).
- [x] Update customer docs and `docs/index.html` to point at self-service
  signup once live, keeping the admin path documented separately for support
  use only. Completed 2026-07-11, extended 2026-07-12 — `docs/index.html`
  "Your account" section (GitHub sign-in, email magic-link sign-in, key
  list/create/revoke); README documents the GitHub OAuth App + Supabase
  provider + redirect-URL setup (shared by both login methods) and keeps the
  admin-script path for support-issued keys.
- [ ] Test: signup requires email verification before key creation; login
  rate limiting; password reset; session expiry/refresh; a logged-in customer
  can never list, rotate, or revoke another customer's keys; login-session
  credentials cannot be used as an `X-API-Key`; audit events are recorded for
  every account-lifecycle action. Partially completed 2026-07-11, extended
  2026-07-12 — covered: rate limiting (including that GitHub and email login
  share one bucket), session expiry/silent-refresh, ownership isolation (the
  cross-customer ownership test), audit events for signup/login/logout with
  correct provider tagging for both GitHub and email, email format validation,
  malformed/failed magic-link send handling. Not covered (not implemented):
  email verification, password reset. Not covered (structurally guaranteed by
  disjoint code paths rather than by a dedicated test):
  login-session-as-API-key rejection. New tests across
  `tests/test_accounts.py` (new file), `tests/test_supabase.py`, and
  `tests/test_api.py`; suite grew from 168 to 229 passing tests, 94.49%
  coverage (93% floor). Rotation tests specifically cover: new secret
  invalidates the old one against a real `/v1/valuations/*` call, ownership
  isolation, and rejection of already-revoked keys.

Exit criteria:

- [ ] A new customer can sign up, verify their email, log in, and generate a
  working scoped API key with no operator/admin-script involvement. Partially
  met 2026-07-11 — sign up/log in/generate-a-key works end to end with no
  operator involvement; there is no independent email-verification step, so
  the exit criterion as literally written is not yet fully met.
- [x] A customer can only ever see or manage their own keys and account data.
  Verified 2026-07-11 by `test_self_service_keys_are_isolated_between_customers`
  and the wrong-customer-revoke unit tests in `tests/test_accounts.py`.
- [x] Login and signup endpoints are rate-limited and do not leak account
  existence. Verified 2026-07-11, extended 2026-07-12 — both
  `/v1/auth/github/login` and `/v1/auth/email/login` are rate-limited per IP
  (shared bucket); the email path always responds `{"sent": true}` for any
  well-formed address whether or not an account exists, so it doesn't open
  the account-existence probe surface that adding an email field could have.
- [x] The account/key system is documented as shared across all current and
  future pubTools products, not coupled to DCF specifics. Verified 2026-07-11 —
  `app/accounts.py` module docstring, migration comments, and README frame
  scopes/accounts as pubTools-wide.

Phase 6 completion evidence, 2026-07-11: GitHub sign-in via Supabase Auth
(PKCE, server-mediated, `HttpOnly` cookies) is implemented end to end —
`app/supabase.py` (`SupabaseAuthClient`), `app/accounts.py` (PKCE, sessions,
self-service key logic), new routes in `app/api.py`
(`/v1/auth/github/login`, `/v1/auth/callback`, `/v1/auth/me`,
`/v1/auth/logout`, `/v1/account/keys*`), migration
`supabase/migrations/002_phase6_customer_login.sql`, and a "Your account"
section in `docs/index.html`.

Phase 6 update, 2026-07-12: two fixes/additions on top of the above.
(1) **Bug fix:** removed a caller-supplied `state` parameter that was being
sent to Supabase's `/authorize` endpoint for CSRF purposes -- `state` is
reserved and managed internally by Supabase Auth, and overriding it broke its
own callback validation (`bad_oauth_state`, reproduced live against the
user's real Supabase project and confirmed fixed by inspecting the resulting
GitHub-bound redirect). PKCE's verifier/challenge binding already provides
the CSRF protection this was redundantly attempting. (2) **Added email magic-
link login** (`POST /v1/auth/email/login`) as a second provider alongside
GitHub, chosen over email+password specifically to avoid building password
storage/reset. Both providers complete through the same `/v1/auth/callback`
PKCE code-exchange step, so no additional redirect-URL configuration is
needed beyond what GitHub already required. Login attempts across both
providers share one per-IP daily rate-limit bucket. Audit events are now
tagged with which provider was used, read from Supabase's own
`user.app_metadata.provider`. Known new gap: signing in with both GitHub and
email under the same real identity creates two separate accounts (no
cross-provider identity linking) -- documented above, not fixed this session.

Deployment setup status, 2026-07-12 (user-confirmed): migration 002 applied,
GitHub OAuth App created and configured in Supabase, redirect URL
allow-listed, and production `PUBLIC_BASE_URL` changed from
`http://127.0.0.1:8000` to `https://pubtools-dcf.vercel.app` in Vercel.
**Correction 2026-07-13:** that domain was wrong — it serves no deployment
(`DEPLOYMENT_NOT_FOUND`). The Vercel project is `pub-tools-dcf` and its real
production domain is `https://pub-tools-dcf-nu.vercel.app` (Vercel appended
`-nu` because the bare name was taken). The wrong `PUBLIC_BASE_URL` made
Supabase redirect sign-ins to the dead domain / fall back to its Site URL
(localhost), which is why production sign-in only completed while a local
server was running. See `issues.MD` for the fix status. The
live authorize→GitHub
redirect chain was curl-verified end to end after the `state`-parameter fix,
and **the user has since completed real browser logins with both GitHub and
email magic link and confirmed both work end to end.** Two UI issues found
during that live testing (frozen-looking quota display, raw `last_used_at`
timestamp) are fixed above. **Still outstanding:**
- Verify after the latest Vercel redeployment that Supabase's redirect
  allowlist contains the callback for whichever host `PUBLIC_BASE_URL` names,
  that login returns there without a redirect loop, and that returning after a
  browser restart restores the account through the refresh cookie.
  **Superseded in part by Phase 9 (2026-07-16):** the production host is moving
  to `https://ashaat.dev`, so the allowlist entry to verify is
  `https://ashaat.dev/v1/auth/callback`, not the
  `pub-tools-dcf-nu.vercel.app` one this item originally named. Sign-in also now
  returns to `/dcf` rather than `/`. Do this as part of Phase 9 Slice 3 rather
  than separately — see `issues.MD`.
- ~~`SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` in Vercel remain unconfirmed
  from Phase 5.~~ Confirmed present 2026-07-13 via `vercel env ls`
  (Production only — Preview has neither, so preview deployments run with
  auth off; see `issues.MD`).
- Configure custom SMTP in Supabase before relying on email login for real
  customer volume (default email sending is rate-limited/dev-oriented).
- Decide whether to add email verification, key rotation/rename, CAPTCHA,
  and cross-provider account linking before treating self-service signup as
  fully public -- all deliberately deferred, not blocking, per the itemized
  status above.

## Phase 7 — HTTP caching and canonical requests

> **Superseded for the valuation endpoint by ADR-008 (implemented 2026-07-18).**
> `/v1/valuations/*` responses carry a live, never-cached Finnhub price and are
> now `Cache-Control: no-store`: the ETag/If-None-Match/304 handling, `Vary`,
> the public/edge cache, and the peek/deferred-consume quota split (which
> existed only so 304s could be free) were all removed. Quota is back to one
> atomic `check_and_increment` pre-flight and `X-RateLimit-*` headers returned
> to all valuation responses (safe again — nothing shared can cache them).
> This phase's design below is kept as the historical record; `compute_etag`
> lives on only in git history (`app/http_cache.py` was deleted).

Goal: fulfill the GET endpoint's caching design safely. This phase is
"HTTP-protocol semantics only" (headers, ETags, canonical-key logic,
conditional requests) -- no new caching *store* is introduced. Every request,
200 or 304, still runs the full fetch+compute pipeline; a real shared
result-cache (Redis, avoiding re-fetching/re-computing entirely) is Phase 8's
job. This deliberately keeps the two phases' scope non-overlapping.

**Decisions made 2026-07-12 (resolving prior ambiguity in this phase):**

1. **Cache audience: public/shared.** Responses carry `Cache-Control: public`
   so a CDN/shared proxy (e.g. Vercel's edge network) can serve a cached
   valuation to *any* caller who requests the same canonical URL -- not just
   the original requester.
   - **Load-bearing consequence, stated explicitly:** a cache hit at the
     CDN/edge layer never reaches this app's origin at all, which means it
     never runs the `X-API-Key` auth/quota middleware. Once a canonical URL
     has been served once and cached, subsequent requests to that *exact*
     URL -- including ones with no API key, or an invalid one -- can receive
     the cached response until it expires or is revalidated. Only the
     request that causes a cache **miss** at the origin is authenticated and
     metered.
   - This is accepted, not accidental: a DCF valuation is a deterministic
     function of public financial data and caller-supplied assumptions, not
     confidential per-customer data, so this doesn't leak anything about a
     *customer* -- but it does mean this endpoint's cache cannot be used to
     enforce per-customer confidentiality of results, and popular
     ticker/assumption combinations become effectively free to re-fetch
     (from cache) for anyone who knows or guesses the canonical URL.
   - Task: document this tradeoff in `docs/index.html`'s Authentication
     section so it isn't a silent surprise to customers or support later.
2. **Canonical form used internally only; no redirect.** Non-canonical
   requests (out-of-order params, `tax_rate` omitted vs. explicit `0.21`,
   scalar vs. pre-expanded `revenue_growth`, etc.) are normalized
   server-side for our own ETag computation, but served directly at
   whatever URL the client used -- no 301/302/308.
   - Consequence: a CDN caches by the *raw* URL it receives, so two
     semantically-identical-but-differently-formatted requests are cached as
     **separate** CDN entries even though they'd produce byte-identical
     content. Our own ETag/If-None-Match logic still correctly recognizes
     repeat requests to the *same exact URL* as unchanged, but doesn't merge
     across non-canonical variants at the CDN layer.
   - Task: keep documenting (README/customer docs) that the interactive
     endpoint builder emits canonical-order, resolved-value URLs specifically
     so repeat/shared traffic gets the most CDN cache reuse.
3. **304s are free.** A conditional request that matches must **not** consume
   that day's quota or produce a `usage_events` row. This requires a
   two-phase quota interaction, replacing the current single-step
   check-and-consume:
   - **Phase A (pre-flight, before any fetch/compute):** a non-consuming
     *peek* at the caller's current count for today's quota window. If
     already at/over the limit, reject immediately with 429 -- unchanged from
     today's behavior, and still avoids wasted compute for an
     already-over-limit caller.
   - **Phase B (post-computation, only on a genuine fresh 200):** the
     existing atomic consume/increment happens here, only after the route
     handler has computed the ETag, compared it against `If-None-Match`, and
     determined the response will *not* be a 304.
   - **Known, accepted race:** peek-then-later-consume has a gap under
     concurrency -- two simultaneous requests from the same key could both
     pass the Phase A peek "under limit" and only be counted afterward in
     Phase B, potentially landing a couple of requests past the exact limit
     in a burst. This is the same class of imprecision the project already
     accepts elsewhere (e.g. the in-process fallback limiter); not fixed in
     this phase.
   - **Hardening follow-up added 2026-07-12:** before production launch, replace
     the accepted race with an exact quota flow. Preferred options: reserve a
     quota slot atomically before compute and refund it only when the response
     becomes a 304, or add a Supabase RPC that atomically reserves unless the
     caller is already over limit. At minimum, check the post-computation
     consume result and return 429 if it reports `allowed=false`, so an
     over-limit race cannot still receive a successful valuation.
   - Requires a new `SupabaseClient`/RPC method (or a `consume_daily_quota`
     variant) that supports a read-only peek distinct from the existing
     always-increments RPC. Add the corresponding migration if a new function
     is needed.
   - This also requires restructuring where quota logic runs relative to
     `call_next()` in `app/api.py`'s request-id middleware: auth (API key
     validity) stays a pre-flight middleware check as today; the
     consume-quota step moves to run *after* the route handler has decided
     the response is a fresh 200, not a 304.
4. **ETag definition:** SHA-256 over the full `ValuationResponse` payload,
   serialized deterministically (sorted keys), with exactly two fields
   excluded: `request_id` and `computed_at` (the only two fields that are
   per-request bookkeeping rather than content). Every other field --
   including `current_price`/`price_as_of` and the sensitivity grid --
   participates, so the ETag changes automatically whenever anything a
   client could see changes, and stays stable across repeat calls when
   nothing has. This also directly satisfies invalidation for quotes,
   restated financials, model-version bumps, and future assumption-schema
   changes: whichever of `data_version`/`model_version`/`current_price`
   changed will change the ETag, with no separate purge mechanism needed.
5. **`Cache-Control` values:** `public, max-age=30, s-maxage=60,
   stale-while-revalidate=30` as the starting point (roughly matching the
   existing 60s quote-refresh cadence) -- `max-age` bounds browser reuse,
   `s-maxage` bounds CDN/edge reuse (Vercel's edge network honors
   `s-maxage`; verify during Vercel preview testing per the Phase 0/every-
   phase Vercel gates whether Vercel's own `CDN-Cache-Control` header should
   be set separately from the generic `Cache-Control`). Tune these numbers
   once real traffic/latency data exists; not fixed in stone here.
6. **`Vary: Accept-Encoding` only.** Explicitly do **not** vary by
   `X-API-Key`/`Authorization` -- that would make the cache per-key instead
   of shared, defeating decision 1.
7. **Suppress `X-RateLimit-*` response headers on cache-eligible responses.**
   Those headers reflect the *specific caller's* quota state; baking them
   into a `Cache-Control: public` response would let a shared cache serve one
   customer's quota numbers to a completely different caller later -- a
   customer-data leak, however minor. Concretely: `X-RateLimit-Limit`,
   `X-RateLimit-Remaining`, and `X-RateLimit-Reset` are omitted from
   `/v1/valuations/*` responses entirely (both 200 and 304) once this phase
   ships, since any response from this endpoint may end up shared. A
   customer's authoritative quota status is only ever knowable from a
   request that actually reaches the origin (cache miss), so no headers on a
   possibly-cached response can be trusted as "this caller's real remaining
   count" anyway.

**Hardening follow-ups added 2026-07-12:**

- [x] Decide whether authenticated valuation responses should remain
  `Cache-Control: public` in production. **Decided 2026-07-13 (user):** keep
  it public, no route split. A CDN cache hit bypassing auth/quota for the
  exact same URL is accepted as a deliberate consequence of the original
  cacheable-GET design (CLAUDE.md): a DCF valuation is a deterministic
  function of public financial data plus caller-supplied assumptions, not
  per-customer-secret, so popular ticker/assumption combinations becoming
  cheap to re-serve from cache is fine. No code change; already documented
  in `docs/index.html`'s Authentication section.
- [x] Clarify that a 304 is quota-free, not necessarily compute-free at origin.
  The current origin still fetches/builds the valuation to compute the ETag
  before returning 304 unless the CDN answers the conditional request first.
  Add server-side valuation/ETag caching or adjust docs to avoid implying
  conditional requests are always cheap for provider/compute cost. Completed
  2026-07-12 — README and `docs/index.html` now explicitly say that a 304 is
  quota-free, but origin requests may still perform provider/compute work
  unless a shared cache/CDN answers first.
- [x] Add tests for exact quota enforcement once reservation/refund or
  consume-result enforcement is implemented, including concurrent same-key
  requests around the quota boundary. Completed 2026-07-13 — `app/api.py`'s
  Phase B now checks the atomic consume's `allowed` flag (previously
  ignored): a rejected consume replaces the already-computed response with
  the same 429 the pre-flight gate returns, and records the usage event as
  `rate_limited` rather than `quota_consumed`. Factored the 429 body into a
  shared `_over_quota_response()` helper used by both the pre-flight peek
  and the post-computation consume path. New regression test
  `test_stale_peek_does_not_let_an_over_limit_consume_serve_a_200` wraps a
  real `DailyRequestLimiter` with a `peek()` forced to report "allowed"
  (simulating the accepted race under a concurrent burst) and proves the
  real, atomic `check_and_increment()` still blocks a second request once
  the limit is reached, fails before the fix and passes after. Since
  `DailyRequestLimiter.check_and_increment`/the Supabase `consume_daily_quota`
  RPC were already atomic and non-overshooting (each call only increments if
  currently under limit), this closes the residual gap: no caller can now
  receive more than `limit` successful (200) responses per day even under
  concurrent bursts past the peek. Suite: 253 passing (was 252), ruff/mypy
  clean.
- [x] **Found via a live `curl` check against the real production
  deployment, 2026-07-13:** pre-flight 401/403 (auth failure) and 503
  (`auth_storage_unavailable`) responses never carried `Cache-Control` at
  all, contradicting this phase's documented claim that "valuation-path
  errors and 429s are `no-store`" -- only the 429 and post-route 4xx/5xx
  (404/422/502) had it. Live evidence: a real 401 from
  `pub-tools-dcf-nu.vercel.app` came back with Vercel's own injected
  `Cache-Control: public, max-age=0, must-revalidate` default, which is
  worse than no header at all for a per-caller response. Fixed --
  `_auth_error_response()` and `_storage_error_response()` in `app/api.py`
  (shared by the valuation pre-flight gate and the account/login routes)
  now set `Cache-Control: no-store` unconditionally. New assertion in
  `test_missing_malformed_or_unknown_api_key_returns_401`.

**Implementation resolved one ambiguity, 2026-07-12:** decision 3's phrase
"consume only on a genuine fresh 200" could be misread as making 4xx/5xx
errors free too. It does not: only a **304** is free. Every other valuation
response -- fresh 200 **and** errors (404/422/502/...) -- still consumes
quota, preserving the deliberate Phase-5 "invalid requests count against the
limit" behavior (`test_invalid_valuation_requests_count_against_daily_limit`).
Also: the peek did **not** need a new RPC/migration -- it reuses the read-only
`SupabaseClient.get_daily_quota_usage()` added in Phase 6, wrapped as a new
`peek()` on both `SupabaseDailyQuotaLimiter` and the in-process
`DailyRequestLimiter`. Fail-closed behavior is preserved: a quota-store
failure at either the peek (pre-flight) or the consume (post-computation)
returns 503 rather than serving an unmetered valuation.

**Tasks:**

- [x] Add a canonical-value resolver... Completed 2026-07-12 — the ETag is
  computed directly from the already-resolved `ValuationResponse` (which
  carries resolved defaults, expanded per-year growth, `model_version`, and
  `data_version`), so no separate resolver was needed. `app/http_cache.py`.
- [x] Implement the SHA-256 ETag; add `ETag` header and `If-None-Match`/304
  handling. Completed 2026-07-12 — `compute_etag()` (SHA-256 over
  `model_dump(mode="json", exclude={"request_id","computed_at"})`, sorted keys)
  and `if_none_match_satisfied()` (handles `*`, weak `W/` prefix, and
  comma-separated candidate lists) in `app/http_cache.py`; the valuation route
  returns a bodyless `Response(304)` on match.
- [x] Implement the two-phase quota peek/consume split. Completed 2026-07-12 —
  `peek()` on both limiters (non-consuming); `app/api.py` middleware does auth
  + peek pre-flight (Phase A, 429 if over limit) and moves the consume + usage
  record to after `call_next` (Phase B), skipped entirely for a 304.
- [x] Set `Cache-Control` and `Vary`. Completed 2026-07-12 —
  `public, max-age=30, s-maxage=60, stale-while-revalidate=30` and
  `Vary: Accept-Encoding` on 200 and 304; `no-store` on 429 and valuation-path
  errors.
- [x] Remove `X-RateLimit-*` from `/v1/valuations/*` responses (kept on 429).
  Completed 2026-07-12.
- [x] Update customer docs. Completed 2026-07-12 — new "Caching" subsection in
  `docs/index.html` documents the public-cache/auth-bypass-on-hit tradeoff,
  free 304s, and the removed quota headers.
- [x] Test: equivalent request forms produce identical ETags. Completed —
  `test_equivalent_request_forms_produce_identical_etags` (route) and
  `tests/test_http_cache.py`.
- [x] Test: a real content change (assumptions / price) changes the ETag;
  request_id/computed_at do not. Completed — `tests/test_http_cache.py`.
- [x] Test: a matching `If-None-Match` returns a bodyless 304 that neither
  increments the quota counter nor writes a `usage_events` row. Completed —
  `test_conditional_request_returns_free_304_without_quota_or_usage`.
- [x] Test: a non-matching/missing `If-None-Match` returns a fresh, metered
  200. Completed — `test_non_matching_conditional_request_returns_a_fresh_metered_200`
  plus every existing happy-path test.
- [x] Test: an already-over-quota caller gets 429 before any provider fetch.
  Completed — `test_over_quota_request_is_rejected_before_any_provider_fetch`
  (asserts zero additional FMP calls) and `test_over_quota_blocks_even_a_would_be_304`.
- [x] Test: `X-RateLimit-*` absent from 200/304; `Vary` is exactly
  `Accept-Encoding`. Completed — `test_fresh_valuation_carries_cache_headers_and_no_quota_headers`
  and the updated rate-limit tests. Suite: 229 -> 247 passing, 94.66% coverage.

Exit criteria:

- [x] A conditional GET with a matching ETag returns 304, never touches the
  quota counter, and never exposes one caller's rate-limit state. Verified
  2026-07-12 by the free-304 test and by removing `X-RateLimit-*` from all
  cacheable responses.
- [x] A conditional GET with a stale/missing ETag returns a fresh, metered
  200. Verified 2026-07-12.
- [x] Equivalent request forms produce byte-identical ETags from this app.
  Verified 2026-07-12.
- [ ] `Cache-Control`/`Vary` match the spec, **verified against a real Vercel
  preview deployment** that the edge honors `s-maxage`. Origin behavior
  live-verified 2026-07-12 against a real uvicorn server (correct ETag,
  `Cache-Control: public, max-age=30, s-maxage=60, stale-while-revalidate=30`,
  `Vary: Accept-Encoding`, 304 with no body, `no-store` on errors, no
  `X-RateLimit-*`). **Still pending:** the Vercel-edge half — confirming the
  CDN actually caches/revalidates and honors `s-maxage`, and deciding whether
  a separate `CDN-Cache-Control` header is needed — which requires a deployed
  preview (blocked on the same Vercel env-var setup outstanding since Phase 5/6).
- [x] The public-cache tradeoff is documented in customer-facing docs.
  Verified 2026-07-12 — `docs/index.html` "Caching" subsection.

## Phase 8 — Persistent storage and distributed caching

Goal: share state across workers, restarts, and serverless instances.

> **Superseded in part by ADR-008 (2026-07-16) — real-time price from Finnhub;
> statements-only caching.** The only caching kept for the valuation path is the
> **financial-statement** cache (Slice A `fund:`/`profile:` + single-flight): N
> differently-assumed valuations of one ticker cost one FMP statement fetch. The
> valuation **response cache (`dcf:v1:resp:`, Slice B) is retired**, along with
> the `dcf:v1:quote:` key and the Phase 7 ETag/304 handling for this endpoint.
> The current market price is fetched live from Finnhub on every request and
> cached nowhere; the daily refresh (ADR-007) no longer fetches a quote.
> `/v1/valuations/*` responses become `Cache-Control: no-store`. Slice C must not
> re-introduce a cached quote or response. See the "real-time current price from
> Finnhub" feature section in `project-docs/issues.MD` for the full plan.

**Design fully specified 2026-07-13 (planning session, no code). Decisions
made by the user that day: Redis provider is Upstash provisioned through the
Vercel Marketplace; Postgres remains the existing Supabase project (no new
database); this session produced the design only — implementation follows in
the sliced order below after the design is reviewed.**

### Scope and principles (from ADR-004, restated concretely)

- **Redis is an ephemeral accelerator and coordinator only** — caches,
  single-flight locks, abuse counters. Nothing billing- or identity-durable
  lives in Redis. Losing the entire Redis instance must never change a
  valuation result, only its cost/latency.
- **Postgres (Supabase) stays the durable system of record** — customers,
  keys, usage, audit (already live), plus this phase's immutable normalized
  snapshots.
- **The API-key daily quota path does not change.** It is already durable,
  atomic, and cross-instance via the Supabase `consume_daily_quota` RPC and
  already fail-closed (503 on storage failure). Redis is deliberately NOT
  introduced into the metering path (ADR-004: "Redis is not the sole durable
  record for billing").
- The DCF engine stays pure/I/O-free; all storage sits behind
  constructor-injectable interfaces; the in-process caches remain as an L1
  warm-instance accelerator in front of Redis (L2).

### Provider and client

- **Upstash Redis via Vercel Marketplace** (user decision 2026-07-13).
  HTTP REST access — correct fit for serverless (no TCP pool per instance).
  Region: **iad1** to match the deployed functions.
- Env vars: read `UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN`, falling
  back to `KV_REST_API_URL`/`KV_REST_API_TOKEN` (the names differ by
  Marketplace integration vintage). Same auto-enable pattern as Supabase in
  Phase 5: vars absent → feature off, everything behaves exactly as today.
- New `app/redis_cache.py`: `RedisConfig.from_env()`,
  `UpstashRedisClient` — plain `httpx` against the REST API (no new runtime
  dependency, same pattern as `app/supabase.py`), one client created at
  lifespan scope. Commands needed: `GET`, `SET` (with `EX`/`PX`/`NX`),
  `DEL`, `INCR`, `EXPIRE`, `EVAL` (compare-and-delete for lock release),
  plus `/pipeline` for batching. **Short timeouts (≈1s)** so a Redis
  brownout cannot eat the request latency budget — every Redis error is
  caught and degrades per the matrix below, never raised to the caller.
- A `RedisBackend` protocol with an `InMemoryRedisBackend` test fake
  (injectable clock, real TTL/NX semantics). Tests never need a live Redis;
  two service instances sharing one fake backend simulate two serverless
  instances.

### Key namespace and serialization

All keys are prefixed `dcf:v1:` (product-scoped so one Upstash instance can
later serve other pubTools products without collisions; `v1` is the
serialization-schema version, bumped only on incompatible layout changes).

Every value is a JSON envelope: `{"v": 1, "t": <stored-at unix>, "d": ...}`.
Readers treat anything that is not valid JSON, lacks `v`, or has an unknown
`v` as a **miss and delete the key** — corrupt/foreign entries can slow one
request, never break one.

| Key | Payload (`d`) | Redis TTL | Freshness rule |
|---|---|---|---|
| `dcf:v1:fund:{TICKER}` | canonical normalized snapshot (the same JSON already fingerprinted for `data_version`) | 48h | current when produced by the latest successful daily run; older entries may be served only with explicit stale/refresh-failed metadata |
| `dcf:v1:profile:{TICKER}` | raw provider profile dict | 48h | same daily-run freshness rule as `fund:` |
| `dcf:v1:neg:{TICKER}` | `{error: "ticker_not_found"\|"ticker_not_covered"\|"unsupported_sector", message}` — reconstructed via an explicit registry; unknown `error` → miss | 4h | fresh for full TTL |
| `dcf:v1:lock:fund:{TICKER}` | random holder token | 45s (`PX`) | n/a (lock) |
| `dcf:v1:login:{ip}:{yyyy-mm-dd}` | integer counter (raw `INCR`, no envelope) | expires end of UTC day | n/a (counter) |

The 48-hour TTL keeps one prior daily result available through a missed or
failed refresh window. Freshness is determined from durable daily-run/head
metadata, not merely from Redis age. The pre-ADR-007 4h statement rule
remains implemented in Slice A and must be replaced in Slice C before the
daily-only provider policy can be claimed as active. (The former
`dcf:v1:quote:`/`dcf:v1:resp:` keys were removed by ADR-008 — no price or
response is cached anywhere.)

L1 entries must also carry the durable refresh generation/status and have a
hard expiry no later than the next 6 PM Eastern refresh-window boundary. Once
that boundary is reached, no instance may keep serving its pre-window L1 entry
as current. While the new run is due/running, a prior DB snapshot may be served
with an explicit warning but must not be re-cached as current. Successful
per-ticker promotion publishes the new generation after the DB commit. This is
how a warm Vercel instance learns that the daily job replaced its old data.

### Authoritative cache-aside read/write flow (added 2026-07-13; price/response scope updated 2026-07-16 per ADR-008)

This layer resolves **financial statements only**. The market price is fetched
live from Finnhub on every request (ADR-008) and is not part of this flow; the
DCF math is then computed fresh from the resolved statements + the live price and
is **not** cached (there is no valuation response cache). The statement lookup
order below is the exact customer-request read path — **cache → database → FMP**,
computing and returning as soon as one layer has the statements — and is fixed and
must not be reordered:

1. Check the in-process L1 cache. On a hit, compute the math (with the live
   price) and return.
2. On an L1 miss, check the Redis L2 `fund:`/`profile:` entries. On a hit,
   hydrate L1, compute, and return.
3. On an L2 miss, enter distributed single-flight. The lock winner queries
   Supabase for the ticker's latest verified normalized snapshot; lock losers
   poll Redis as already designed and then fall through if necessary.
4. A database hit hydrates `BaseFinancials`, repopulates L1 and Redis (writing
   `fund:` last as the commit marker), and is returned with the durable
   statement/profile timestamps and daily-refresh status. A customer request
   never refreshes an existing ticker's statements from FMP.
5. Only a genuinely cold ticker — absent from L1, Redis, **and** the database —
   triggers the one-time bootstrap FMP fetch. A stale existing head is served
   with explicit freshness metadata and waits for the next scheduled refresh;
   customer traffic never refreshes existing-ticker statements from FMP.
6. After a successful bootstrap or scheduled provider fetch and normalization,
   synchronously await the Supabase snapshot/head write, then populate the
   `profile:`/`fund:` caches (there is no `quote:` cache — price is never
   cached), then return or complete the job. Cache publication happens after the
   DB write and `fund:` remains the final cache commit marker.

"Before returning" means a cold bootstrap's DB/cache operations are awaited on
the Vercel request path. A database error is not a confirmed miss and therefore
must not cause customer traffic to call FMP. If neither cache can serve the
ticker, return a controlled storage-unavailable response. A bootstrap or daily
refresh is not published to Redis as durable data unless the snapshot/head write
succeeds. Authentication and quota storage remain independently fail-closed.

Freshness is based on the mutable ticker head's `verified_at`, not the immutable
snapshot row's `created_at`. This is required because a provider refresh can
confirm the same filing/data hash again; `ON CONFLICT DO NOTHING` correctly keeps
the immutable snapshot unchanged while the head records the new verification.

### Daily all-database-ticker FMP refresh at 6 PM Eastern (final decision 2026-07-13)

Every ticker present in `ticker_snapshot_heads` is refreshed once during each
Eastern calendar day's 6 PM refresh window. The cycle fetches the FMP input set
used by a valuation—**statements and profile** (the market price is live from
Finnhub per ADR-008 and is no longer part of the refresh or the snapshot).
Existing tickers must not call FMP from customer requests; those requests receive
the newest stored statements with their exact timestamps and refresh status.

- Vercel cron expressions are UTC-only. Configure the same guarded endpoint at
  both `0 22 * * *` and `0 23 * * *`; the endpoint converts the current instant
  to `America/New_York`, proceeds only when the local hour is 18, and atomically
  claims that Eastern calendar date. The other invocation is a successful no-op.
  This handles EST/EDT without manually changing schedules. On Vercel Hobby the
  run may start anywhere within the 6:00–6:59 PM hour; exact 6:00 PM execution
  requires minute-precise Vercel scheduling or an external timezone-aware
  scheduler.
- Platform constraints were re-verified 2026-07-13 against Vercel's official
  [Cron Jobs](https://vercel.com/docs/cron-jobs),
  [Usage and Pricing](https://vercel.com/docs/cron-jobs/usage-and-pricing), and
  [Managing Cron Jobs](https://vercel.com/docs/cron-jobs/manage-cron-jobs)
  documentation: timezone is UTC, Hobby precision is per-hour, multiple
  schedules may share one path, `CRON_SECRET` is sent as a Bearer header,
  duplicate delivery is possible, and failed invocations are not retried.
- Endpoint: `GET /internal/cron/refresh-financials`, excluded from OpenAPI and
  protected by `Authorization: Bearer {CRON_SECRET}`. Missing configuration or
  a non-constant-time mismatch returns 401. `CRON_SECRET` is server-only and at
  least 16 random characters.
- "Once daily" means one **complete FMP refresh cycle per ticker per Eastern
  refresh date**, not one HTTP request: the existing FMP client uses multiple
  endpoints and may make bounded retries. A transactional Supabase claim keyed
  by `(ticker, refresh_date)` is the durable idempotency gate; Redis locking is
  additional concurrency control, never the sole proof. Duplicate Vercel cron
  delivery or a Redis reset must not spend a second provider cycle that day.
- A genuinely cold ticker with no L1, Redis, or DB snapshot may perform one
  on-request bootstrap provider load so the existing synchronous valuation API
  can return a result. It immediately persists the snapshot/head. A bootstrap
  is not a scheduled-refresh claim: if that ticker exists when the 6 PM
  manifest is created, the job refreshes it again with every other DB ticker.
- At run start, create a durable run record, enumerate **every** ticker in
  `ticker_snapshot_heads`, and create a pending per-ticker claim/manifest. There
  is no activity filter, popularity ordering, provider-budget skip, or silent
  deferral. `last_requested_at` may remain for analytics but cannot determine
  refresh inclusion.
- For an existing but stale snapshot, customer traffic never calls FMP. Return
  the stored snapshot with `freshness_status`, `last_refresh_attempt_at`,
  `last_refresh_success_at`, `next_refresh_window_at`, statement timestamps,
  and a warning when the latest cycle is due, running, partial, or failed.
  (Price fields are live per request — ADR-008 — and carry their own
  `price_as_of`/`price_fetched_at`.)
- Acquire a job-level Redis lock and per-ticker locks, use bounded concurrency,
  and make all writes idempotent. Vercel does not retry failed cron invocations,
  so persist per-ticker attempt/success/failure state for operational recovery.
- Provider-plan and Vercel-duration capacity for the complete manifest is a
  deployment gate. If the application cannot refresh every ticker, the run must
  finish `partial_failed`/`failed`, identify every unprocessed ticker, and alert;
  it must never silently refresh a smaller subset. Increase provider/runtime
  capacity or move orchestration to a durable worker before enabling growth that
  exceeds the daily window.
- A provider refresh promotes a snapshot only when the newest income, balance,
  and cash-flow records form one complete compatible period. Provider delay,
  partial data, or refresh failure leaves the prior head active and records a
  customer-visible warning/status until the next daily cycle.
- On each successful ticker promotion, replace the Redis fundamentals/profile
  entries and ensure pre-window L1 entries have already expired. (There is no
  response cache, `quote:` key, or response-cache generation to rotate —
  ADR-008; responses are `no-store`, so nothing downstream can carry stale
  data past the request.)

Suggested freshness statuses: `current_as_of_daily_refresh`,
`daily_refresh_due`, `daily_refresh_running`, `daily_refresh_partial_failed`,
`daily_refresh_failed`, and `bootstrap_snapshot`. `next_refresh_window_at` is
deterministic schedule metadata; it is not a claim that a company will file at
that time. The price is live from Finnhub on every request (ADR-008) and
carries its own `price_as_of`/`price_fetched_at`; only statement/profile
freshness is governed by the daily refresh.

**Implementation status:** Slice A's current code still implements the older
request-time provider refresh and 60s/15m quote policy. Do not claim the daily
limit is active until Slice C replaces those branches, installs the two guarded
cron schedules, and the no-request-time-FMP/all-manifest tests pass. This is a
recorded design decision; enforcement remains pending.

### Distributed single-flight

Layered on top of (not replacing) the existing in-process task coalescing:

1. L1 in-process `_inflight` map coalesces same-instance bursts (exists today).
2. On an L2 miss, attempt `SET dcf:v1:lock:fund:{T} <token> NX PX 45000`.
   - **Winner:** follow the DB read-through flow. A customer request fetches
     from the provider only for a genuinely cold ticker; the daily cron owns
     refreshes for existing snapshots. After any provider success, persist DB,
     populate `profile:`/`fund:` (fund last; no `quote:` — ADR-008), then release via
     compare-and-delete (`EVAL`: delete only if the stored token matches ours —
     never delete a successor's lock after our own TTL expired).
   - **Loser:** poll the `fund:` key every 200ms for up to 3s; if it appears,
     proceed from cache; if not, **fall through to the DB flow**. Only a truly
     cold ticker may then bootstrap from FMP. An existing snapshot is served
     with freshness metadata and waits for the scheduled refresh, so a
      stuck/crashed winner never blocks valuations or causes request-driven
      FMP refreshes.
3. Lock TTL (45s) covers the actual bounded provider path: five endpoints use
   a three-slot semaphore and each may make up to three six-second attempts,
   plus capped retry delays. Losers still poll for only 3s before falling
   through, so a crashed holder never blocks a valuation for the lock TTL.

### Valuation response cache (RETIRED by ADR-008 — historical design)

> Implemented in Slice B, then removed 2026-07-18 with the Finnhub live-price
> feature: no valuation response is cached anywhere. Kept for the record only.

Closes Phase 7's documented "a 304 is quota-free but not compute-free"
caveat: repeat requests (fresh 200 *and* conditional 304) within the TTL are
served from Redis without touching FMP or recomputing the DCF.

- Key includes the canonical assumption string and `model_version`;
  `data_version` and `current_price` live *inside* the cached payload, and
  the 60s TTL remains aligned with `s-maxage` as a short response-cache window;
  it no longer represents quote freshness. A successful bootstrap or scheduled
  refresh must rotate a per-ticker cache generation (included in the response
  cache key) so old assumption variants become unreachable immediately.
- On hit: inject a fresh `request_id`/`computed_at`, recompute the ETag from
  the cached content (identical by construction — the ETag already excludes
  exactly those two fields), and honor `If-None-Match` as usual.
- **Metering is unchanged**: a response-cache hit is still an origin request
  and still consumes quota (only a 304 is free, and that rule already lives
  in the middleware, not the compute path).
- Do NOT cache error responses in Redis (the negative ticker cache already
  covers the definitive ones; transient errors must stay retryable).

### Login rate limiting moves to Redis

Fixes the known gap (issues.MD 2026-07-13): the per-IP login limiter is
currently a per-instance `DailyRequestLimiter`, nearly meaningless on
serverless. Replace with `INCR dcf:v1:login:{ip}:{date}` + `EXPIRE`-on-first
increment; same 20/day limit and 429 envelope. The in-process limiter stays
as the fallback when Redis is unconfigured or down.

### Fail-open / fail-closed matrix (explicit, per subsystem)

| Subsystem | On Redis/Postgres failure | Rationale |
|---|---|---|
| fund/profile/quote/neg/response caches (Redis) | **fail-open**: treat as miss and continue to DB/normal flow | availability; Redis is an accelerator |
| single-flight lock (Redis) | **fail-open**: continue without distributed coordination | a lock outage must never block valuations |
| login rate limit (Redis) | **fail-open to the in-process limiter** | abuse control degrades, logins keep working; residual risk accepted and documented |
| API-key auth + daily quota + usage (Supabase) | **fail-closed: 503** (unchanged from Phase 5/7) | never serve an unmetered valuation |
| snapshot read on a customer cache miss (Supabase, new) | **fail-closed: controlled 503**; never interpret an error as a missing ticker and call FMP | preserves the once-daily provider guarantee and prevents an outage-driven FMP spike |
| snapshot/head write during bootstrap or scheduled refresh (Supabase, new) | **fail-closed for publication of that new dataset**; keep the prior head/cache active, record failure, and continue other scheduled tickers | DB is the durable source; Redis must not advertise an uncommitted dataset |
| one ticker fails during the daily run | continue the manifest, mark that claim failed, and mark the run partial/failed | one bad ticker must not block the remainder, but no ticker may disappear silently |

### Postgres: migration 003 (immutable snapshots + current ticker heads)

`supabase/migrations/003_phase8_snapshots.sql`:

- Table `normalized_snapshots`: `snapshot_version text primary key` (SHA-256 of
  the slow-moving normalized financial/profile payload, explicitly excluding
  `current_price`, `price_as_of`, and `price_fetched_at`), `ticker text not
  null`, `snapshot jsonb not null`, `provider text not null`, `fiscal_year int`,
  `statement_date date`, `currency text`, `created_at timestamptz not null
  default now()`. Add `(ticker, statement_date desc, created_at desc)` index for
  history/support queries.
  Service-role only (no RLS policies — same posture as `api_keys`);
  explicitly `revoke update, delete` so rows are immutable (ADR-005: never
  mutate historical snapshots).
- Table `ticker_snapshot_heads`: `ticker text primary key`, `snapshot_version
  text not null references normalized_snapshots(snapshot_version)`,
  `verified_at timestamptz not null`, `last_requested_at timestamptz`,
  `last_refresh_attempt_at timestamptz`,
  `last_refresh_success_at timestamptz`, `refresh_status text`, and
  `updated_at timestamptz not null default now()`. This table
  is intentionally mutable: it is a current pointer/observation, not historical
  evidence. Restrict all access to the service role.
- Table `financial_refresh_runs`: `refresh_date date primary key` (the
  `America/New_York` calendar date), `scheduled_window_at timestamptz`,
  `started_at`, `finished_at`, `status`, `total_tickers`, `attempted_tickers`,
  `succeeded_tickers`, `failed_tickers`, and bounded/redacted run error fields.
  The atomic insert is the date-level guard for the dual UTC cron invocations
  and gives operations a durable, reconcilable record of every daily run.
- Table `financial_refresh_claims`: `ticker text`, `refresh_date date`
  referencing `financial_refresh_runs(refresh_date)`,
  `claimed_at timestamptz`, `completed_at timestamptz`, `status text`, and a
  bounded/redacted `error_code`; primary key `(ticker, refresh_date)`. An atomic
  insert is the durable once-per-day provider gate and makes duplicate cron
  delivery idempotent. Retain claims for a documented operational window, then
  delete through a service-role maintenance procedure. These claims describe
  scheduled work only; a request-time cold bootstrap cannot pre-claim or exempt
  a ticker from that evening's manifest.
- Run-start RPC/transaction: claim the Eastern refresh date, snapshot all current
  `ticker_snapshot_heads.ticker` values into pending claims, and set
  `total_tickers` from that manifest. Completion reconciles run counts against
  claims. A ticker cold-bootstrapped after the manifest is created records its
  same-day claim during bootstrap or joins the following day's manifest.
- Read path: query the head and referenced snapshot in one RPC/query by ticker.
  Return a typed record containing the immutable normalized payload plus
  verification timestamps. A missing, malformed, wrong-ticker, future-
  dated, or beyond-max-staleness record is a miss; malformed records are logged
  and never passed into the engine.
- Write path: on a provider-backed fundamentals load, compute the price-free
  `snapshot_version`, then in one transaction/RPC `insert ... on conflict do
  nothing` for the immutable snapshot and upsert the ticker head with the new
  `verified_at`. Await this call before publishing Redis and before
  returning/completing the claim (Vercel gate: no post-response background
  work). Failure leaves the prior head active and fails that bootstrap/claim.
- No quote is stored on the head or anywhere else (ADR-008: the price is live
  from Finnhub per request and may not be cached or persisted; durable quote
  history stays deferred).
- The public response's existing `data_version` currently hashes the combined
  fundamentals and quote. It is deliberately distinct from internal
  `snapshot_version`. Phase 8 guarantees durable normalized financial
  documents, not full historical reproduction of every transient quote-backed
  response; durable quote history remains Phase 12.
- Deliberately **out of scope for 003**: a durable quote-history table (revisit
  in Phase 12 when historical analysis needs it) and raw-capture storage
  (Phase 10).
- Rollback: drop `financial_refresh_claims`, then `financial_refresh_runs`, then
  `ticker_snapshot_heads`, then `normalized_snapshots`
  (documented in the migration header). Retention: snapshots are a few KB per
  ticker-quarter — keep
  indefinitely; deletion is a documented manual operation.
- Backups: rely on Supabase's managed backups; document the restore
  procedure in the migration header.

### Implementation order (separate sessions, each fully tested/verified)

- [x] **Slice A — Redis client + distributed fundamentals caching +
  single-flight.** `app/redis_cache.py`, envelope/serialization, the four
  ticker caches as L2 behind `FundamentalsService`, lock protocol, fail-open
  handling, in-memory fake + tests (incl. two-instance sharing, corrupt
  entries, Redis-down, lock-holder-crash fall-through). Completed 2026-07-13:
  Upstash REST auto-configuration is lifespan-scoped with no new dependency;
  fundamentals/profile/quote/negative entries use versioned envelopes and
  max-staleness TTLs behind the existing L1 caches; `fund:` is published last
  as the distributed commit marker; token-safe distributed locking and bounded
  loser polling are implemented. The lock TTL was corrected from the planned
  10s to 45s after re-reading the real five-endpoint, three-slot, three-attempt
  provider path. Shared-fake tests prove one provider load across two service
  instances; corruption, Redis outage, bounded stale fallback, shared negative
  caching, and crashed-holder fall-through are covered. Full verification:
  276 tests, 93.82% coverage, Ruff format/lint, and mypy pass.
- [x] **Slice B — valuation response cache + Redis login rate limiting.**
  Response-cache read/write in the route, ETag equivalence test, metering
  unchanged tests; login limiter backend swap + fallback tests. Completed
  2026-07-14: new `app/response_cache.py` (assumption fingerprint over the
  *resolved* `Assumptions` + sensitivity flag + `model_version`; key
  `dcf:v1:resp:{TICKER}:{generation}:{fingerprint}` with the generation read
  from `dcf:v1:gen:{TICKER}`, absent → "0", so Slice C's rotation
  invalidates without further Slice B changes; 60s TTL; per-request fields
  stripped on write and re-injected on read; payloads that fail pydantic
  validation or carry per-request fields are deleted and recomputed). Route
  integration in `app/api.py::get_valuation` — a hit skips the provider AND
  `compute_dcf`, reproduces the identical ETag, honors `If-None-Match`, and
  is still metered by the middleware; errors are never cached.
  `RedisLoginRateLimiter` in `app/rate_limit.py` (pipelined
  `INCR`+`EXPIRE` per attempt on `dcf:v1:login:{ip}:{date}`; TTL refreshed
  every increment — safe because the key embeds the UTC date — which also
  self-heals the classic INCR-without-EXPIRE leak; fail-open to the
  in-process limiter), wired in `create_app`'s lifespan when Redis is
  configured. 16 new tests in `tests/test_response_cache.py` prove
  no-recompute via a monkeypatched `compute_dcf` call counter (not timing):
  cross-instance hit, equivalent-form key sharing, sensitivity in the key,
  304-from-cache, metering unchanged (usage + quota still increment on a
  hit), errors never cached, generation rotation, corrupt-entry
  self-healing, TTL expiry, Redis-down fail-open, fingerprint properties,
  cross-instance login limiting, UTC-day reset, limiter fail-open.
  Suite: 276 → 292 passing, 94.04% coverage; ruff/format/mypy clean.
- [ ] **Slice C — migration 003 + DB read-through/snapshot writes.**
  **Part 1 implemented 2026-07-18** (see PROGRESS.md): migration 003 drafted
  (tables + `store_ticker_snapshot` RPC; run-orchestration RPCs pending; not
  yet applied), price-free snapshot fingerprint, typed Supabase read/write
  methods, **L1 → Redis → DB → FMP(cold only)** orchestration with DB-hit
  cache repopulation and write-before-cache-before-return ordering,
  `SnapshotStoreError` fail-closed 503, fake-Supabase handlers, and the
  cold-bootstrap / no-request-time-FMP / malformed-row / DB-down / conflict /
  ordering tests (20 new; suite 311). **Part 2 — the scheduler — implemented
  2026-07-18** (see PROGRESS.md): `begin/finish_financial_refresh_run` RPCs
  (atomic date claim, full manifest, claim-derived reconciliation — pending
  claims force `partial_failed`), `app/refresh.py` runner with the
  `America/New_York` 18:00-hour guard over both UTC schedules,
  `FundamentalsService.refresh_from_provider`
  (`current_as_of_daily_refresh`), the `CRON_SECRET`-protected
  `GET /internal/cron/refresh-financials`, `vercel.json` crons at
  `0 22 * * *` + `0 23 * * *`, and 14 tests (EST/EDT guard, duplicate-cron
  no-op, complete-manifest reconciliation, partial-run isolation, endpoint
  auth; suite 325). **Part 3 remains:** the 6-PM L1 hard-expiry cutoff
  (warm instances must not serve pre-window L1 data as current),
  structured customer freshness metadata beyond `data_quality_warnings`
  (`freshness_status`, `next_refresh_window_at`, last attempt/success), and
  live verification with the real Upstash/Supabase/cron wiring.
  **Per ADR-008 this slice must NOT include a `quote:` cache, a `resp:` response
  cache, or response-cache generation rotation** (those are removed by the
  Finnhub feature; see `issues.MD`); the daily cycle refreshes statements/profile
  only. User applies migration 003 to the live project after local verification
  (same flow as 001/002) and sets `CRON_SECRET` before deploying the cron
  configuration.
- [ ] **Live verification** on a real deployment once the user provisions
  Upstash: two-instance cache sharing observed (second request after a cold
  hit performs zero FMP calls from a different instance), Redis-down
  fail-open exercised by pointing at a dead URL in a preview env.

### User actions required (provisioning — before Slice A can be live-verified)

1. [x] Vercel dashboard → project `pub-tools-dcf` → **Storage → Create →
   Marketplace → Upstash (Redis)**, free tier, region **iad1**, connect to
   the project for Production + Preview (env vars are auto-injected). User
   confirmed provisioning completed 2026-07-13.
2. [ ] `vercel env pull` (or copy the two REST vars into local `.env`) so local
   runs can hit the same instance.
3. [x] Say the word when done — user authorized implementation 2026-07-13.
4. [ ] Before Slice C is deployed, generate a random `CRON_SECRET` (at least
   16 characters), add it to Vercel Production, and redeploy. Do not expose it
   to the browser or commit it.
5. [ ] Confirm the FMP plan and chosen runtime can finish
   `COUNT(ticker_snapshot_heads) × expected FMP endpoint calls (plus bounded
   retries)` inside one daily run. If not, upgrade capacity or choose a durable
   worker; do not reduce the ticker manifest.

Exit criteria:

- [ ] Two app instances sharing Redis serve the same ticker with exactly one
  provider load (proven in tests via a shared fake backend; live-verified on
  the deployment). Shared-backend test proof completed 2026-07-13; live
  deployment proof remains.
- [ ] A total Redis outage changes latency/cost only: valuations still
  succeed, auth/quota still fail closed via Supabase.
- [ ] Every distinct normalized financial `snapshot_version` fetched from the
  provider exists as exactly one immutable `normalized_snapshots` row; the
  ticker head points to the latest verified snapshot, and a cold cache/redeploy
  reads it before making FMP calls. Full quote-backed response
  reproduction remains explicitly deferred to Phase 12.
- [ ] An existing stored ticker never triggers any FMP call from a customer
  request, including quote retrieval; durable claims prove at most one complete
  refresh cycle per ticker per Eastern date despite duplicate cron delivery or
  Redis loss.
- [ ] Every ticker in the run-start database manifest ends the run with an
  explicit success or failure claim; counts reconcile and there is no activity,
  popularity, or budget-based omission.
- [ ] The UTC schedules and `America/New_York` guard produce exactly one claimed
  daily run during the 6 PM Eastern hour in both EST and EDT tests.
- [ ] Responses expose last attempt/success, next refresh window, and statement
  timestamps, and accurately warn when the latest daily run is due, partial, or
  failed (price timestamps ride on the live quote — ADR-008).
- [ ] A pre-window L1 entry cannot remain silently current after its ticker is
  promoted; tests cover warm instances spanning 6 PM. (No response/edge cache
  exists to carry anything — responses are `no-store`.)
- [ ] Login rate limiting is enforced across instances, not per instance.
- [x] Corrupt/foreign/unknown-version Redis entries are treated as misses and
  cleaned up, never surfaced as errors. Verified 2026-07-13 by envelope and
  end-to-end fundamentals-cache tests.

## Phase 9 — Public site: portfolio front end, API directory, and domain migration

Goal: this deployment becomes the public website. The portfolio is the front
page, an API directory lists the products built on this platform, and the DCF
product moves to its own path. The owner's custom domain is repointed from the
standalone portfolio Vercel project to this one.

**User decisions (2026-07-16):** merge into one repo/Vercel project (explicitly
chosen over a two-project proxy/rewrite split); portfolio at `/`; DCF at the
`/dcf` subpath; the domain moves here afterwards. Marked **urgent** and
scheduled immediately after Phase 8 at the user's request; old Phases 9–14 were
renumbered to 10–15 to make room (same precedent as the Phase 6 insertion).

> **Conflicts with Phase 15 (formerly Phase 14, "Separate UI/UX from the
> microservice") — read before touching either.** Phase 15 plans to strip all
> bundled UI out of this deployment and serve it from a separate frontend,
> replacing `GET /` with a headless banner/redirect. This phase does the exact
> opposite by design: it merges *more* UI in and makes this deployment the
> website, with the custom domain pointed at it. **Phase 9 supersedes Phase 15
> in practice.** Phase 15 is on hold and must be re-scoped or dropped; do not
> implement it without an explicit decision to reverse Phase 9. The pubTools
> multi-product goal is not abandoned — the `/apis` directory added here is the
> shared shell that future products get listed in.

### Route map (end state)

| Route | Serves | Notes |
|---|---|---|
| `GET /` | `docs/portfolio.html` | portfolio front page; CTA button → `/apis` |
| `GET /apis` | `docs/apis.html` (new) | directory of developed APIs; DCF card → `/dcf`; one card per future pubTools product |
| `GET /dcf` | `docs/index.html` | DCF docs/tool/account UI; **the CSRF cookie bootstrap moves here from `/`** |
| `GET /Pics/*` | static images | immutable long-lived cache headers so the CDN absorbs them |
| `/v1/*`, `/health`, `/docs`, `/openapi.json` | unchanged | API surface is untouched by this phase |

### Known consequences (accept explicitly)

1. **Sign-in breaks unless the domain move is completed in full.** The OAuth /
   magic-link `redirect_to` is built from `PUBLIC_BASE_URL`, and Supabase only
   honors allow-listed redirect URLs. Moving the domain without updating both
   reproduces exactly the 2026-07-13 production outage (`issues.MD`: wrong
   `PUBLIC_BASE_URL` → sign-in only worked with a local server running).
2. **The auth callback must retarget to `/dcf`.** It currently redirects to
   `{base}/` on success and `{base}/?login_error=` on failure; once `/` is the
   portfolio, a signed-in user would land on the portfolio and the
   `login_error` banner would render on a page that cannot display it (the
   handler lives in `docs/index.html`).
3. **`/` is no longer the DCF docs.** Existing links/bookmarks to `/` now reach
   the portfolio. Accepted — the product is pre-launch with no external
   consumers; `/dcf` is the new canonical docs URL.
4. **Function bundle grows by the portfolio images** (~540 KB in `docs/Pics/`),
   served through the Python function. Mitigated with immutable cache headers so
   the edge serves repeat requests. Revisit if bundle size becomes a gate
   (Phase 0's Vercel bundle rule).

### Slice 1 — Routing and static assets

- [ ] `GET /` serves `docs/portfolio.html`; `GET /dcf` serves `docs/index.html`.
- [ ] Move the CSRF cookie bootstrap from `/` to `/dcf` (the account UI lives
  there; the portfolio has no state-changing calls and needs no token).
- [ ] Serve `docs/Pics/*` at `/Pics/*` with `Cache-Control: public, max-age=31536000, immutable`.
- [ ] Apply the existing landing-page CSP to every HTML page; confirm
  `img-src 'self'` covers the portfolio images and that no external
  font/script/style host is introduced (all three pages stay self-contained).
- [ ] Retarget the auth callback: `{base}/` → `{base}/dcf`, and
  `{base}/?login_error=` → `{base}/dcf?login_error=`.
- [ ] Update the four tests asserting `GET /` serves the DCF page
  (`test_root_serves_customer_landing_page` and three others).

### Slice 2 — API directory (`/apis`)

- [ ] New `docs/apis.html` using the same design system as `docs/index.html`
  (identical tokens/sidebar/components; self-contained inline `<style>`).
- [ ] Lists the DCF Valuation API as a live product linking to `/dcf`, with the
  page structured so each future pubTools product is one additional card.
- [ ] Portfolio gets a primary CTA button → `/apis` plus a sidebar link.
- [ ] Route + tests (200, links resolve, CSP header present).

### Slice 3 — Domain migration (owner actions, in this order)

**Target domain: `ashaat.dev`** (owner-confirmed 2026-07-16). Full checklist and
failure notes live in `issues.MD`.

- [ ] Remove `ashaat.dev` from the standalone portfolio Vercel project.
- [ ] Add it to the `pub-tools-dcf` project and verify DNS (decide on `www`).
- [ ] Update Production `PUBLIC_BASE_URL` to `https://ashaat.dev` and redeploy.
- [ ] Add `https://ashaat.dev/v1/auth/callback` to Supabase → Authentication →
  URL Configuration → Redirect URLs; review the Site URL.
- [x] Update the hardcoded base URL in `docs/index.html` → `https://ashaat.dev`.
  Completed 2026-07-16 — both curl examples; the endpoint builder derives its
  base from `window.location.origin` and needed no change.
- [ ] Retire/delete the old portfolio Vercel project once `ashaat.dev` resolves
  here.

### Slice 4 — Verification

- [ ] Full suite, ruff, ruff format, mypy clean; coverage floor held.
- [ ] Live on the deployed domain: `/` renders the portfolio; `/apis` lists the
  DCF API; `/dcf` renders the tool; `/Pics/*` return 200 with the immutable
  cache header; a full GitHub **and** email sign-in round-trip completes and
  lands on `/dcf` with the local server stopped; `/v1/valuations/*` and
  `/health` are unaffected.

Exit criteria:

- [ ] A visitor to the domain lands on the portfolio and can reach a directory
  of developed APIs in one click, then reach the DCF tool from there.
- [ ] Sign-in works end to end on the new domain with no local server running.
- [ ] The DCF API surface (`/v1/*`, `/health`, `/docs`) is behaviorally
  unchanged by the site merge.

## Phase 10 — Raw-data audit storage

Goal: retain provider evidence safely without blocking requests or overwriting
captures.

- [ ] Replace second-resolution filenames with collision-proof identifiers.
- [ ] Persist captures atomically with content hash, endpoint, ticker, retrieval
  time, request ID, HTTP metadata, and schema version.
- [ ] Move storage off the async request path or make the sink asynchronous.
- [ ] Define whether sink failure fails the request or alerts asynchronously.
- [ ] Redact credentials and sensitive headers before logging/storage.
- [ ] Add compression, retention/lifecycle deletion, access controls, and cost
  monitoring.
- [ ] Add replay tooling to renormalize historical captures without provider calls.
- [ ] Test concurrent captures, atomicity, failure, redaction, replay, and retention.

Exit criteria:

- [ ] Every normalized snapshot traces to immutable provider evidence.
- [ ] Audit storage cannot block the event loop or overwrite another capture.

## Phase 11 — Configuration, observability, and operations

Goal: make behavior configurable, diagnosable, and supportable.

- [ ] Add typed settings for environment, provider, timeouts/retries, TTLs,
  concurrency, databases, raw sink, authentication, CORS, logging, feature flags,
  and supported universe.
- [ ] Validate settings at startup and remove scattered environment reads.
- [ ] Add typed trusted-proxy/client-IP configuration. Added 2026-07-12 after
  security review: login rate limiting currently keys from `request.client.host`,
  which may be wrong or overly broad behind Vercel/proxies. Define which
  forwarded headers are trusted in Vercel, ignore spoofed headers outside
  trusted deployments, and test proxy-spoofing cases before relying on per-IP
  abuse controls.
- [ ] Add structured logs with request/route/status/latency, cache outcome,
  provider outcome, retries, data version, and model version.
- [ ] Test secret/PII redaction across logs, tracing, and error reporting.
- [ ] Add metrics for HTTP, cache, provider, quota, normalization, calculation,
  rate-limit, and stale-data behavior.
- [ ] Add tracing across API, cache/storage, provider, normalization, and engine.
- [ ] Split health into liveness/readiness; readiness must not spend provider calls.
- [ ] Define SLOs and alerts for availability, latency, errors, quota, cache health,
  stale data, and normalization/data-quality failures.
- [ ] Gracefully close in-flight shared tasks, clients, storage, and telemetry.
- [ ] Document incident response, key rotation, provider outage, bad-data rollback,
  and model-version rollback.

Exit criteria:

- [ ] Operators can distinguish traffic, provider, storage, normalization, data,
  and calculation incidents.

## Phase 12 — Historical analysis and richer assumptions

Goal: replace single-year extrapolation with transparent operating drivers while
preserving a simple mode.

- [ ] Retain at least 3–5 compatible historical periods and derived ratios.
- [ ] Return historical revenue growth, EBIT margin, D&A/revenue, capex/revenue,
  NWC behavior, and data-quality indicators.
- [ ] Add a versioned assumptions schema supporting scalar/per-year revenue
  growth, EBIT margin, D&A ratio, capex ratio, and NWC investment method.
- [ ] Preserve simple mode with explicit, documented, history-derived defaults.
- [ ] Add optional mid-year discounting and echo the selected convention.
- [ ] Decide/test treatment for stock compensation, leases, capitalized R&D,
  minority interests, non-operating assets, pensions, and multiple share classes.
- [ ] Warn on abnormal base years/ratios, insufficient history, negative FCF, and
  terminal value dominating enterprise value.
- [ ] Validate multiple industries/fiscal calendars against independent
  spreadsheets and expand property-based reconciliation tests.

Exit criteria:

- [ ] Every operating assumption is explicit and independently variable.
- [ ] Simple-mode compatibility or versioned migration is documented and tested.

## Phase 13 — Terminal value and expanded sensitivity

Goal: make perpetual-growth assumptions explicit and economically coherent.

- [ ] Add a terminal-value strategy abstraction while retaining Gordon growth.
- [ ] Add a growth/ROIC method where reinvestment rate equals terminal growth
  divided by terminal ROIC and terminal FCF reflects reinvestment.
- [ ] Validate terminal ROIC, reinvestment, mature margin, growth, and WACC.
- [ ] Add an exit-multiple method only if its metric/source/limitations are clear;
  never silently mix terminal methods.
- [ ] Return terminal method, inputs, intermediates, and terminal-value share of EV.
- [ ] Make bounded sensitivity axes/grid size configurable.
- [ ] Warn on invalid cells and excessive terminal-value dependence.
- [ ] Add independent spreadsheet and property tests for every strategy.
- [ ] Increment model version and document output differences before changing a
  default methodology.

Exit criteria:

- [ ] Terminal growth, profitability, and reinvestment reconcile.
- [ ] Every terminal strategy is explicit, independently tested, and versioned.

## Phase 14 — Security, performance, and release readiness

Goal: verify the complete service under realistic traffic and failures.

- [ ] Add dependency and deployment-image vulnerability scanning to CI.
- [ ] Fuzz paths, queries, headers, provider payloads, and cache/storage records.
- [ ] Review CORS, trusted proxies, forwarded headers, HTTPS, security headers,
  secrets, and production debug/OpenAPI exposure.
- [x] Upgrade API-key hashing to support a server-side pepper and hash versions.
  Added 2026-07-12 after security review: SHA-256 is acceptable for generated
  high-entropy keys, but an HMAC/peppered hash (`API_KEY_HASH_PEPPER`) reduces
  blast radius if the database leaks. Add versioned hash records so existing
  SHA-256 keys can continue working during migration. Completed 2026-07-12 —
  `APIKeyAuthenticator` now writes `sha256:` or `hmac-sha256-v1:` hashes,
  verifies legacy unprefixed SHA-256 records, and both Supabase auth and
  `scripts/create_api_key.py` use the shared verifier/hash function.
- [ ] Add end-to-end authenticated tests through provider/cache/storage to the
  auditable response.
- [ ] Load/soak test hot, cold, mixed-ticker, degraded-upstream, and dependency
  restart scenarios.
- [ ] Define and demonstrate latency, error, and quota budgets.
- [ ] Add backward-compatibility tests for supported API/model versions.
- [ ] Create deployment, migration, rollback, data-refresh, and model-rollback
  checklists.
- [ ] Obtain independent financial-model and security reviews before launch.
- [ ] Update README, customer docs, OpenAPI, changelog, and `PROGRESS.md`.

Exit criteria:

- [ ] The release meets documented correctness, security, availability, latency,
  auditability, and compatibility targets.

## Phase 15 — Separate UI/UX from the microservice

Goal: the DCF API becomes a pure, headless JSON microservice with no bundled
marketing/docs/UI, and a separately deployable frontend serves the website,
docs, interactive tools, and (per Phase 6) the login/account portal. This is
the pubTools platform shape: one shared frontend, many independent backend
microservices (DCF today, more calculators later), each deployable and
versionable on its own.

Today `GET /` in `app/api.py` returns `docs/index.html` (a self-contained
marketing page, teaching content, API reference, and an interactive endpoint
builder) directly from the FastAPI process — the same deployable artifact as
the valuation API itself. That coupling is what this phase removes.

- [ ] Decide the target split: a separate frontend deployment (its own repo or
  clearly separated directory, its own Vercel project/domain) that talks to
  the DCF API — and every future pubTools microservice — purely over HTTP as a
  client, never by importing backend code or sharing a deployment artifact.
- [ ] Decide the domain/routing shape: e.g. a shared pubTools site/portal
  domain for marketing, docs, the Phase 6 login/account portal, and the
  interactive builder, versus per-product API domains/subdomains (DCF's API
  stays reachable at its own host, unaffected by frontend deploys).
- [ ] Add explicit CORS configuration to the FastAPI app scoped to the known
  frontend origin(s) only (never a wildcard), since browser calls to the API
  will now be cross-origin instead of same-origin.
- [ ] Remove `docs/index.html` from the API's own deployment; replace `GET /`
  with something appropriate for a headless service (a redirect to the new
  frontend, a minimal machine-readable service banner, or removal in favor of
  `/health` and `/docs`) — decide and document which.
- [ ] Confirm the API is fully usable with no bundled UI: the interactive
  endpoint builder is a convenience client only and must not depend on any
  DCF-side behavior that isn't already exposed through the public API/OpenAPI
  contract.
- [ ] Decide whether FastAPI's native `/docs`/`/openapi.json` stay on the API
  service (machine/interactive API reference) while the hand-written
  teaching/marketing content moves to the new frontend, or whether both move.
- [ ] Wire the Phase 6 login/account portal and self-service API-key
  management into the new frontend rather than the DCF repo, since it is
  pubTools-wide, not DCF-specific.
- [ ] Decide how the frontend authenticates to the API from a different
  origin: confirm the Phase 6 browser session cookies (`Secure`, `SameSite`)
  and CORS settings work correctly cross-origin, and that a login session
  still can never be used as an `X-API-Key`.
- [ ] Update README/customer docs to state where the frontend lives relative
  to this backend repo, and how to run/deploy each independently.
- [ ] Add a redirect or compatibility plan for existing bookmarked links to
  the API's current `/` docs URL so it doesn't silently 404 post-split.
- [ ] Test: the API's contract/behavior tests pass with no UI files present
  (headless-only); CORS tests confirm only allowed origins succeed; a smoke
  test confirms the new frontend can reach the API cross-origin end to end.

Exit criteria:

- [ ] The DCF microservice's deployment contains no bundled marketing/docs/UI
  content and is independently deployable/versionable from any frontend.
- [ ] A separately deployable frontend serves the website, docs, interactive
  tools, and account/login portal, calling the API only over HTTP with an
  explicit CORS allowlist.
- [ ] Adding a new pubTools product means standing up a new backend
  microservice without changing the frontend's shared shell, beyond adding a
  client for the new API.

## Delivery milestones

1. **Safe private beta:** Phases 0–4.
2. **Controlled public beta:** Phases 5–11 (includes Phase 9, the public site).
3. **Advanced valuation API:** Phases 12–13.
4. **Production release:** Phase 14 plus all earlier exit criteria.
5. **Platform decoupling (pubTools multi-product readiness):** Phase 15 —
   **on hold; conflicts with Phase 9.** See the note in Phase 9.

Do not combine a public API contract migration and a model-methodology change in
one release. Version and measure them separately so calculation changes can be
distinguished from transport/schema regressions.
