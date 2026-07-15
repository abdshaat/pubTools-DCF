# Request Flow Reference

Last verified against the code: 2026-07-14

Implemented baseline: through Phase 8 Slice B

Primary application entrypoint: `app.api:app`

This document explains how every public request moves through the application,
where each operation lives, and which parts of the Phase 8 architecture are
designed but not yet implemented. Function and class names are included because
they remain useful even if line numbers move.

## Status legend

- **Implemented:** present in the current application code.
- **Planned:** specified in Phase 8 Slice C but not present in the runtime yet.
- **Conditional:** enabled only when its required environment variables or
  external service are configured.

## High-level origin flow

```text
Client or website
    |
    v
Vercel edge/CDN
    |-- public valuation cache hit --> cached response; origin is not called
    |
    `-- cache miss / non-cacheable request
            |
            v
      FastAPI ASGI app
            |
            v
      Request-ID middleware
            |
            |-- valuation request --> API-key authentication --> quota peek
            |
            v
      FastAPI routing + type/shape validation
            |
            v
      Selected route handler
            |
            v
      Exception mapping / response serialization
            |
            v
      Middleware post-processing
            |-- security + request-ID headers
            `-- valuation response --> atomic quota consume + usage event
            |
            v
      Vercel edge/client
```

The Vercel entrypoint is declared in
[`pyproject.toml`](../pyproject.toml#L66) as `app.api:app`. The default app is
created at the bottom of [`app/api.py`](../app/api.py#L1019) by calling
`create_app()`.

## 1. Process startup before requests

This happens once per FastAPI/Vercel function instance, not once per request.

1. Import-time configuration optionally loads the local `.env` file. Existing
   process environment variables are not overwritten. This is in
   [`app/api.py`](../app/api.py#L108).
2. `create_app()` reads Supabase and Redis configuration from the environment.
   The application chooses durable Supabase implementations when Supabase is
   configured and local in-process fallbacks otherwise. See
   [`app/api.py`](../app/api.py#L273), `SupabaseConfig.from_env()` in
   [`app/supabase.py`](../app/supabase.py#L43), and `RedisConfig.from_env()` in
   [`app/redis_cache.py`](../app/redis_cache.py#L34).
3. During FastAPI lifespan startup, the app creates one `FMPClient`, one
   `FundamentalsService`, and—when configured—one Upstash Redis HTTP client.
   These objects are placed on `app.state` and reused by requests handled by
   that instance. See the `lifespan()` function in
   [`app/api.py`](../app/api.py#L295).
4. With Redis configured, the login limiter is replaced with the distributed
   `RedisLoginRateLimiter`. Without Redis, it remains an in-process limiter.
5. With Supabase configured, valuation API-key authentication, daily quota
   enforcement, usage metering, customer sessions, and account management use
   Supabase. Without it, valuation authentication is disabled and the daily
   valuation counter is only local to that warm instance. See
   [`app/api.py`](../app/api.py#L339).
6. The local raw FMP response sink is disabled on Vercel because the deployment
   filesystem is not durable. See `_default_raw_sink()` in
   [`app/api.py`](../app/api.py#L120).

## 2. The Vercel edge can answer before FastAPI

Successful valuation responses are intentionally public HTTP-cacheable:

```text
Cache-Control: public, max-age=30, s-maxage=60, stale-while-revalidate=30
Vary: Accept-Encoding
```

The policy is defined in [`app/http_cache.py`](../app/http_cache.py#L22).

If Vercel has a reusable response for the exact valuation URL, it may return
that response without invoking FastAPI. In that case, request IDs,
authentication, quota checks, and usage metering do not run. This is an accepted
architecture decision because valuation inputs and outputs are treated as
public data. Account, login, error, and rate-limit responses are not intended
for shared caching.

The website calls the same API route as an external customer. The builder
constructs the query URL and sends `X-API-Key` when supplied in
[`docs/index.html`](../docs/index.html#L1227), especially `update()` and
`runValuation()` around lines 1281–1428.

## 3. Universal origin middleware

Every request that reaches FastAPI passes through `_request_id()` in
[`app/api.py`](../app/api.py#L355).

### 3.1 Request identity

The middleware creates a UUID and stores it as `request.state.request_id`.
After the route finishes, it adds the value to the `X-Request-ID` response
header. Error envelopes use the same value.

### 3.2 Valuation-route detection

The special authentication/quota flow runs for a `GET` whose path starts with
`/v1/valuations/`. Other routes skip machine API-key authentication and the
valuation quota flow.

This check occurs before FastAPI decides whether the ticker/path/query is
valid. Consequently, a malformed valuation request is authenticated and quota-
peeked before FastAPI produces its validation response. A non-304 valuation
error later consumes quota by design.

### 3.3 Security response headers

After route processing, every normal origin response receives:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- a restrictive `Permissions-Policy`

These values are defined in [`app/api.py`](../app/api.py#L145). The landing page
also receives its dedicated Content Security Policy.

## 4. Detailed valuation request flow

Route: `GET /v1/valuations/{ticker}`

Main handler: `get_valuation()` in
[`app/api.py`](../app/api.py#L881)

### Step 1: API-key authentication

Before routing or query validation, middleware reads the `X-API-Key` header and
requests the `valuation:read` scope.

In a configured production environment:

1. `SupabaseAPIKeyAuthenticator.authenticate()` parses a key in
   `dcf_{prefix}_{secret}` form.
2. It uses the public prefix to retrieve one candidate row from Supabase.
3. It verifies the stored hash with constant-time comparison. New keys use an
   HMAC-SHA-256 digest when `API_KEY_HASH_PEPPER` is configured.
4. It rejects invalid, revoked, expired, or insufficient-scope keys.
5. It updates `last_used_at` and returns the key/customer identity and that
   key's configured daily quota.

Code locations:

- Middleware call: [`app/api.py`](../app/api.py#L367)
- Key parsing and hash verification: [`app/auth.py`](../app/auth.py#L66)
- Supabase authenticator: [`app/supabase.py`](../app/supabase.py#L488)
- API-key lookup/update calls: [`app/supabase.py`](../app/supabase.py#L91)
- Database schema: [`supabase/migrations/001_phase5_auth_usage.sql`](../supabase/migrations/001_phase5_auth_usage.sql#L13)

Failures stop the request before provider/cache/DCF work:

- Invalid/missing/revoked/expired key: `401 invalid_api_key`
- Missing required scope: `403 insufficient_scope`
- Supabase authentication-storage failure: `503 auth_storage_unavailable`

### Step 2: non-consuming daily-quota check

The middleware checks whether the key is already at its daily limit without
incrementing the counter. Production calls
`SupabaseDailyQuotaLimiter.peek()`, which reads the UTC-day counter from
Supabase. The key's `daily_quota` normally defaults to 100.

Code locations:

- Pre-flight call and 429 handling: [`app/api.py`](../app/api.py#L402)
- Supabase quota adapter: [`app/supabase.py`](../app/supabase.py#L539)
- Counter lookup: [`app/supabase.py`](../app/supabase.py#L163)
- Atomic quota SQL/RPC: [`supabase/migrations/001_phase5_auth_usage.sql`](../supabase/migrations/001_phase5_auth_usage.sql#L72)

An already-exhausted key receives `429 rate_limit_exceeded` before financial
data is loaded or the DCF is computed. That rejection does not consume another
quota unit, but a best-effort rate-limited usage event is recorded.

### Step 3: FastAPI routing and request-shape validation

After middleware pre-flight succeeds, `call_next()` hands the request to
FastAPI. FastAPI validates:

- Ticker length and the `^[A-Za-z][A-Za-z.\-]*$` pattern
- Required numeric query parameters: `wacc`, `terminal_growth`, `ebit_margin`
- Required string query parameter: `revenue_growth`
- Optional `tax_rate`, `projection_years`, and `sensitivity` types/defaults

The route declaration is in [`app/api.py`](../app/api.py#L897). Pydantic/FastAPI
shape failures are converted to the versioned `422 request_validation_failed`
envelope by `_request_validation_error()` in
[`app/api.py`](../app/api.py#L503).

### Step 4: assumption parsing and normalization

The route parses `revenue_growth` as one number or a comma-separated list and
constructs the internal `Assumptions` dataclass. A scalar growth rate is
expanded to one value per projection year.

Code locations:

- `_parse_revenue_growth()`: [`app/api.py`](../app/api.py#L132)
- Route construction: [`app/api.py`](../app/api.py#L937)
- `Assumptions.__post_init__()`: [`app/models.py`](../app/models.py#L43)

Important current ordering: cross-field and range rules such as
`terminal_growth < wacc`, projection years 3–15, finite values, and ±50%
growth are validated by the DCF engine, not by the route. On a response-cache
miss, the current code can therefore load financial data before those domain
rules produce a 422. The domain rules live in `_validate()` in
[`app/dcf_engine.py`](../app/dcf_engine.py#L86).

### Step 5: distributed valuation-response cache

The route creates a SHA-256 fingerprint from the fully resolved assumptions,
the `sensitivity` flag, and `MODEL_VERSION`. It then checks Redis using:

```text
dcf:v1:resp:{TICKER}:{generation}:{fingerprint}
```

The generation comes from `dcf:v1:gen:{TICKER}` and currently defaults to `0`.
Slice C will rotate that value after durable financial-data promotion.

On a valid cache hit:

1. Provider access and DCF computation are skipped.
2. A new `request_id` and `computed_at` are injected.
3. The cached data is validated through `ValuationResponse`.

On a Redis error, corrupt payload, schema mismatch, or miss, processing falls
through to the normal data/compute path. Redis is fail-open here.

Code locations:

- Route integration: [`app/api.py`](../app/api.py#L947)
- Fingerprint/key/read/write logic: [`app/response_cache.py`](../app/response_cache.py#L52)
- Redis envelope validation: [`app/redis_cache.py`](../app/redis_cache.py#L249)

### Step 6: fundamentals service and request coalescing

On a response-cache miss, the route calls
`FundamentalsService.get_base_financials()` in
[`app/fundamentals.py`](../app/fundamentals.py#L487).

The service uppercases the ticker and checks its per-instance `_inflight` map.
Concurrent requests for the same ticker in one process await the same shielded
task instead of starting duplicate work.

### Step 7: implemented financial-data lookup order

The current runtime order inside `_load_base_financials()` is:

```text
L1 normalized fundamentals
    -> Redis L2 normalized fundamentals
    -> L1/Redis negative result
    -> distributed Redis single-flight lock
    -> FMP provider load
```

This code is in [`app/fundamentals.py`](../app/fundamentals.py#L348).

#### 7a. L1 warm-instance cache

The service first checks in-process fundamentals, profile, quote, and negative
caches. Defaults currently are:

- Statements/fundamentals fresh for 4 hours
- Profile fresh for 24 hours
- Quote fresh for 60 seconds
- Definitive negative outcomes fresh for 4 hours

If fundamentals are fresh, the service may refresh an expired profile and then
loads or refreshes the quote independently.

#### 7b. Redis L2 cache

On an L1 miss/stale entry, Redis is checked for `fund`, `profile`, `quote`, and
`neg` envelopes under the `dcf:v1:` namespace. Payloads are decoded and
strictly checked before becoming internal dataclasses. Corrupt entries are
deleted and treated as misses. Redis errors are treated as misses.

Relevant code:

- L2 helpers/decoders: [`app/fundamentals.py`](../app/fundamentals.py#L196)
- Upstash REST backend: [`app/redis_cache.py`](../app/redis_cache.py#L74)
- Versioned envelopes: [`app/redis_cache.py`](../app/redis_cache.py#L249)

#### 7c. Negative cache

Unknown, provider-uncovered, and unsupported-sector results are cached locally
and in Redis. Transient provider/network failures are not negative-cached.
Definitions and reconstruction live in
[`app/fundamentals.py`](../app/fundamentals.py#L42) and
[`app/fundamentals.py`](../app/fundamentals.py#L131).

#### 7d. Distributed single-flight

On a cache miss, the service attempts a Redis `SET NX PX` lock for the ticker.
The winner continues loading data. A loser polls the Redis `fund` entry for up
to three seconds; if no winner result appears, it falls through rather than
blocking for the full lock lifetime. Lock release uses token-safe compare-and-
delete.

Code locations:

- Acquire/release: [`app/fundamentals.py`](../app/fundamentals.py#L315)
- Loser polling: [`app/fundamentals.py`](../app/fundamentals.py#L337)
- Redis compare-and-delete: [`app/redis_cache.py`](../app/redis_cache.py#L74)

### Step 8: current provider request path

If no usable cached result exists, the current implementation calls FMP. This
will change in planned Slice C for existing database tickers.

`FMPClient.fetch_fundamentals()` requests up to five candidate annual records
from each statement endpoint plus profile and quote data:

- Income statement
- Balance sheet statement
- Cash-flow statement
- Company profile
- Quote

Requests are concurrent but limited by a three-slot semaphore. Each endpoint
has a six-second timeout and up to two retries after the initial attempt.
HTTP 429 and 5xx responses use capped retry/backoff; 401/403 become provider
configuration failures; 402 becomes ticker-not-covered; 404 becomes ticker-not-
found.

Code locations:

- Provider configuration/endpoints: [`app/providers/fmp.py`](../app/providers/fmp.py#L34)
- Transport/retry/error classification: [`app/providers/fmp.py`](../app/providers/fmp.py#L138)
- Multi-endpoint load: [`app/providers/fmp.py`](../app/providers/fmp.py#L202)

Outside Vercel, successful raw endpoint payloads may be written under
`data/raw/{ticker}` by `FileRawSink`. Vercel disables that local sink.

### Step 9: normalization and latest compatible filing selection

FMP data is transformed into `BaseFinancials` by
`normalize_fmp_fundamentals()` in
[`app/normalization.py`](../app/normalization.py#L213).

Normalization:

1. Rejects financial-sector companies that the standard unlevered-FCF model
   does not support.
2. Keeps annual (`FY`) candidates with valid dates.
3. Joins income, balance sheet, and cash flow by the same period/date.
4. Chooses the latest compatible set, preferring later accepted/filing dates
   for restatements.
5. Rejects currency conflicts and warns when a newer annual period is
   incomplete rather than mixing periods.
6. Maps provider fields into revenue, EBIT, D&A, capex, change in NWC, debt,
   cash, diluted shares, and price.
7. Normalizes signs, units, dates, quote timestamp, filing provenance, and data-
   quality warnings.

Statement matching is implemented by `_select_latest_complete_statements()` in
[`app/normalization.py`](../app/normalization.py#L87). Quote validation is in
`normalize_fmp_quote()` around line 193. The canonical structure is
`BaseFinancials` in [`app/models.py`](../app/models.py#L14).

### Step 10: stale-data fallback and cache publication

If provider or normalization refresh fails, a statement snapshot no older than
24 hours may be returned with a warning. A quote refresh failure may use a quote
no older than 15 minutes with its own warning. Otherwise the error propagates.

After successful normalization, the service writes L1 data and best-effort
Redis profile/quote data, then writes `fund` last as the distributed commit
marker. The lock is released, and the result is returned to the route. See
[`app/fundamentals.py`](../app/fundamentals.py#L438).

There is currently **no Supabase financial-snapshot lookup or write in this
path**. Supabase currently serves authentication, quota, usage, and account
data—not normalized financial documents.

### Step 11: domain validation and DCF calculation

The route calls `compute_dcf()` in
[`app/dcf_engine.py`](../app/dcf_engine.py#L135).

The engine first validates the base data and assumptions. It then:

1. Derives base-year D&A, capex, and NWC ratios to revenue.
2. Projects revenue and EBIT for every forecast year.
3. Computes taxes, NOPAT, unlevered FCF, discount factors, and present values.
4. Computes Gordon-growth terminal value and its present value.
5. Calculates enterprise value, subtracts net debt, divides by diluted shares,
   and calculates upside versus the current price.
6. Rejects all non-finite intermediate/final values and retains negative model
   values with warnings instead of silently clipping them.

The engine is pure and contains no network/database/cache access. Internal
types are in [`app/models.py`](../app/models.py).

### Step 12: optional sensitivity grid

When `sensitivity=true`, `compute_sensitivity_grid()` calculates a 3×3 matrix
using WACC ±1% and terminal growth ±0.5%. Invalid Gordon-growth cells become
`null` rather than failing the entire request. See
[`app/dcf_engine.py`](../app/dcf_engine.py#L252).

### Step 13: wire response construction

`build_valuation_response()` converts internal dataclasses into the Pydantic
wire schema, combines data-quality and model warnings, adds filing/quote
provenance and the disclaimer, and computes `data_version` as a SHA-256 of the
normalized financial snapshot. See:

- Response models: [`app/schemas.py`](../app/schemas.py#L20)
- Response builder: [`app/schemas.py`](../app/schemas.py#L207)
- Model version: [`app/__init__.py`](../app/__init__.py)

Only successful responses are stored in the 60-second Redis response cache.
`request_id` and `computed_at` are excluded from the cached content.

### Step 14: ETag and conditional response

The route computes a strong ETag over all response content except
`request_id` and `computed_at`. If `If-None-Match` matches, it returns a
bodyless 304 and sets `request.state.is_not_modified = True`.

Code locations:

- Route handling: [`app/api.py`](../app/api.py#L990)
- ETag calculation/matching: [`app/http_cache.py`](../app/http_cache.py#L37)

A conditional request still performs authentication and the quota peek. It may
also need cache/data/DCF work to reconstruct the ETag, but a resulting 304 does
not consume customer quota.

### Step 15: atomic quota consumption and usage metering

Control returns to middleware after the route/exception handler creates a
response.

- A 304 consumes no quota and writes no usage event.
- Every other route-produced valuation result that passed authentication and
  pre-flight—including 200, 404, 422, provider 500, 502, and provider 503—uses
  the atomic Supabase `consume_daily_quota` RPC. Authentication, storage, and
  already-over-limit responses returned during pre-flight do not consume.
- If a concurrent request exhausted the quota after the earlier peek, the
  computed response is replaced with a 429. It is never served unmetered.
- If durable quota storage fails, the response is replaced with a controlled
  503. Quota enforcement is fail-closed.
- Usage-event recording is best effort after a successful consume; a usage-log
  write failure is suppressed and does not replace the customer response.
- Non-200/304 valuation responses receive `Cache-Control: no-store`.

This post-route phase is in [`app/api.py`](../app/api.py#L428). The adapters are
`SupabaseDailyQuotaLimiter` and `SupabaseUsageMeter` in
[`app/supabase.py`](../app/supabase.py#L539). The SQL functions live in
[`supabase/migrations/001_phase5_auth_usage.sql`](../supabase/migrations/001_phase5_auth_usage.sql).

## 5. Error flow

Valuation exceptions are converted into a backward-compatible `detail` field
plus a versioned `error` object. Handlers are registered in
[`app/api.py`](../app/api.py#L479), while exception types live in
[`app/exceptions.py`](../app/exceptions.py).

| Source | HTTP/code | Behavior |
|---|---|---|
| FastAPI/Pydantic request validation | 422 `request_validation_failed` | Per-field request-shape errors |
| DCF domain validation | 422 `invalid_assumptions` | Assumption/base-data rule failed |
| Financial-sector gate | 422 `unsupported_sector` | Standard DCF unsupported |
| Ticker absent | 404 `ticker_not_found` | Definitive result can be negative-cached |
| Provider-plan/universe gap | 404 `ticker_unavailable` | Avoids exposing subscription details |
| Normalization failure | 502 `normalization_failed` | Provider data could not form a safe canonical snapshot |
| FMP key rejected | 500 `provider_auth_misconfigured` | Server configuration problem |
| Transient provider failure | 503 `provider_unavailable` | Retryable; not negative-cached |
| API-key/quota storage failure | 503 `auth_storage_unavailable` | Fail-closed |
| Daily quota exhausted | 429 `rate_limit_exceeded` | No provider/DCF work when caught by pre-flight |

Unless the response is a free 304 or pre-flight rejection, valuation errors
still consume one daily request. This prevents repeated invalid or expensive
requests from bypassing the 100-request allowance.

## 6. Landing-page request

Route: `GET /`

Handler: `landing_page()` in [`app/api.py`](../app/api.py#L642)

Flow:

1. Universal request-ID middleware runs; valuation authentication/quota does
   not.
2. FastAPI selects the route.
3. `docs/index.html` is returned with the landing-page CSP.
4. If `pt_csrf` is absent, the response sets a new readable CSRF cookie.
5. Middleware adds the request-ID and common security headers.

The browser-side API builder/account code lives in
[`docs/index.html`](../docs/index.html#L1227).

## 7. Browser authentication flows

Human browser sessions and machine API keys are deliberately separate:

- Browser/account routes use `pt_session` and `pt_refresh` cookies.
- Valuation routes use `X-API-Key`.
- Neither credential type is accepted in place of the other.

Cookie, PKCE, CSRF, session, and account logic is centralized in
[`app/accounts.py`](../app/accounts.py). Supabase Auth HTTP calls are in
`SupabaseAuthClient` in [`app/supabase.py`](../app/supabase.py#L385).

### 7.1 GitHub sign-in

Route: `GET /v1/auth/github/login`

1. Confirm Supabase Auth is configured.
2. Increment the per-IP daily login-attempt limiter (20/day).
3. Generate a PKCE verifier/challenge.
4. Build the Supabase GitHub authorization URL with
   `{PUBLIC_BASE_URL}/v1/auth/callback` as the redirect.
5. Return a 302 and store the verifier in a short-lived HttpOnly, SameSite=Lax
   cookie.

Locations:

- Route: [`app/api.py`](../app/api.py#L657)
- PKCE/login URL: [`app/accounts.py`](../app/accounts.py#L94)
- Supabase authorize URL: [`app/supabase.py`](../app/supabase.py#L409)

### 7.2 Email magic-link request

Route: `POST /v1/auth/email/login`

1. FastAPI validates `EmailLoginRequest`.
2. Confirm Supabase Auth is configured.
3. Require the double-submit CSRF token: `pt_csrf` cookie must match
   `X-CSRF-Token` using constant-time comparison.
4. Increment the same per-IP login limiter.
5. Validate the email, generate PKCE values, and ask Supabase to send a magic
   link whose final redirect is the common callback.
6. Return `{ "sent": true }` and store the verifier cookie.

Locations:

- Route: [`app/api.py`](../app/api.py#L670)
- Email/PKCE logic: [`app/accounts.py`](../app/accounts.py#L178)
- Supabase OTP call: [`app/supabase.py`](../app/supabase.py#L426)
- Website caller/CSRF header: [`docs/index.html`](../docs/index.html#L1491)

### 7.3 Login-attempt limiting

When Redis is available, `RedisLoginRateLimiter` increments
`dcf:v1:login:{ip}:{utc-date}` and sets its expiry in one pipeline. Redis
failure falls back to an in-process counter so login remains available, but
cross-instance abuse protection is weaker during that outage.

Locations:

- Route helper: [`app/api.py`](../app/api.py#L625)
- Redis limiter: [`app/rate_limit.py`](../app/rate_limit.py#L119)

### 7.4 Shared OAuth/magic-link callback

Route: `GET /v1/auth/callback?code=...`

1. Reject provider errors or a missing code with an error redirect.
2. Read and immediately clear the short-lived PKCE verifier cookie.
3. Exchange the authorization code plus verifier for access/refresh tokens.
4. Retrieve the authenticated Supabase user.
5. Find or create the corresponding `api_customers` row.
6. Record signup/login audit events.
7. Set HttpOnly access/refresh cookies and a readable CSRF cookie.
8. Redirect to the landing page.

Locations:

- Callback route: [`app/api.py`](../app/api.py#L692)
- Completion/provisioning: [`app/accounts.py`](../app/accounts.py#L212)
- Code exchange/user retrieval: [`app/supabase.py`](../app/supabase.py#L446)
- Account-login schema changes: [`supabase/migrations/002_phase6_customer_login.sql`](../supabase/migrations/002_phase6_customer_login.sql)

## 8. Existing browser-session resolution

Routes such as `/v1/auth/me` and `/v1/account/keys` call `_account_context()` in
[`app/api.py`](../app/api.py#L571), which delegates to
`get_current_customer()` in [`app/accounts.py`](../app/accounts.py#L294).

Flow:

1. Try the `pt_session` access token against Supabase Auth.
2. If it is valid, load the customer linked to that auth user.
3. If it is missing/expired, try `pt_refresh`.
4. Exchange a valid refresh token for a new session and load the customer.
5. Return refreshed cookies on the route's response when refresh occurred.
6. If both paths fail, return `401 not_signed_in` and clear session/CSRF
   cookies.

This is the mechanism that keeps signed-in customers logged in across visits
until the refresh cookie expires or is revoked.

## 9. Account and API-key request flows

All account routes require a valid browser session. Mutating routes also require
the matching CSRF cookie/header. They do not accept `X-API-Key` as account
authentication.

| Route | Flow | Main locations |
|---|---|---|
| `GET /v1/auth/me` | Resolve/refresh session, return customer identity, ensure CSRF cookie exists | [`app/api.py`](../app/api.py#L729), `get_current_customer()` |
| `GET /v1/account/keys` | Resolve session, list only this customer's keys, attach today's usage for active keys | [`app/api.py`](../app/api.py#L768), [`app/accounts.py`](../app/accounts.py#L333) |
| `POST /v1/account/keys` | Validate body, resolve session, check CSRF, enforce maximum five active keys, generate/store hashed key, audit, reveal full key once | [`app/api.py`](../app/api.py#L782), [`app/accounts.py`](../app/accounts.py#L351) |
| `POST /v1/account/keys/{id}/revoke` | Resolve session, check CSRF, ownership-scoped revoke, audit | [`app/api.py`](../app/api.py#L811), [`app/accounts.py`](../app/accounts.py#L382) |
| `POST /v1/account/keys/{id}/rotate` | Resolve session, check CSRF, reject missing/foreign/revoked key, replace only secret hash, audit, reveal new key once | [`app/api.py`](../app/api.py#L827), [`app/accounts.py`](../app/accounts.py#L394) |
| `POST /v1/account/keys/{id}/rename` | Validate body, resolve session, check CSRF, update only label on owned active key, audit | [`app/api.py`](../app/api.py#L854), [`app/accounts.py`](../app/accounts.py#L428) |
| `POST /v1/auth/logout` | Check CSRF, best-effort audit and upstream logout, always clear local session/refresh/CSRF cookies | [`app/api.py`](../app/api.py#L742), [`app/supabase.py`](../app/supabase.py#L478) |

The full API-key secret is never stored. `APIKeyAuthenticator.hash_secret()` in
[`app/auth.py`](../app/auth.py#L66) produces the stored digest. Supabase CRUD
methods and ownership filters live in [`app/supabase.py`](../app/supabase.py#L216).
The browser callers are in [`docs/index.html`](../docs/index.html#L1619).

## 10. Health, schema, documentation, and unmatched routes

### `GET /health`

Returns only `{ "status": "ok", "model_version": ... }`. It does not probe
FMP, Supabase, or Redis and therefore indicates that the application process is
serving—not that every dependency is healthy. Handler:
[`app/api.py`](../app/api.py#L1012).

### `/docs`, `/redoc`, and `/openapi.json`

FastAPI creates these routes automatically from the application and declared
response models. They pass through universal middleware but not valuation API-
key/quota handling. App/schema declarations are in
[`app/api.py`](../app/api.py#L330) and [`app/schemas.py`](../app/schemas.py).

### Unmatched routes

FastAPI/Starlette produces the normal 404 response. Universal middleware still
adds request/security headers. A `GET` path beginning with
`/v1/valuations/` is treated as valuation traffic by middleware even if final
routing fails, so its non-304 response can consume quota.

## 11. Implemented versus planned financial-data flow

The current runtime and final Phase 8 design must not be confused.

### Implemented now (Slices A and B)

```text
Valuation response Redis cache
    -> L1 fundamentals
    -> Redis L2 fundamentals
    -> negative caches / distributed lock
    -> FMP on an unresolved miss or stale refresh
    -> normalize
    -> compute
    -> response cache
```

Files implementing this today:

- [`app/response_cache.py`](../app/response_cache.py)
- [`app/fundamentals.py`](../app/fundamentals.py)
- [`app/redis_cache.py`](../app/redis_cache.py)
- [`app/providers/fmp.py`](../app/providers/fmp.py)

### Planned Slice C—not runtime behavior yet

The approved final architecture in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) will change an unresolved
fundamentals lookup to:

```text
L1 -> Redis -> latest verified Supabase financial snapshot
    -> FMP only for a confirmed cold ticker
```

It will also add the guarded daily 6 PM Eastern job that refreshes statements,
profile, and quote for every ticker in `ticker_snapshot_heads`, persists the
new snapshot/head before cache publication, and rotates the ticker response-
cache generation.

None of the following exists yet:

- Migration `003` and normalized financial snapshot/head tables
- `financial_refresh_runs` or `financial_refresh_claims`
- Supabase financial snapshot read/write methods
- The `/internal/cron/refresh-financials` route
- Vercel cron configuration and Eastern-time guard
- Daily all-database-ticker processing
- The real producer that rotates `dcf:v1:gen:{TICKER}`

Until Slice C is implemented and deployed, an existing ticker can still reach
FMP from customer traffic after the current caches become stale or miss.

## 12. Quick code ownership map

| Responsibility | Code location |
|---|---|
| Vercel/ASGI entrypoint | [`pyproject.toml`](../pyproject.toml#L66), [`app/api.py`](../app/api.py#L1019) |
| App startup and dependency selection | `create_app()`/`lifespan()` in [`app/api.py`](../app/api.py#L273) |
| Request ID, API-key pre-flight, quota consume, usage | `_request_id()` in [`app/api.py`](../app/api.py#L355) |
| Valuation route | `get_valuation()` in [`app/api.py`](../app/api.py#L897) |
| Browser auth/account routes | [`app/api.py`](../app/api.py#L642) |
| Machine API-key parsing/hashing | [`app/auth.py`](../app/auth.py) |
| Browser cookies, PKCE, CSRF, account/key operations | [`app/accounts.py`](../app/accounts.py) |
| Supabase REST/Auth adapters | [`app/supabase.py`](../app/supabase.py) |
| Valuation and login rate limiters | [`app/rate_limit.py`](../app/rate_limit.py) |
| Redis backend/envelopes | [`app/redis_cache.py`](../app/redis_cache.py) |
| Distributed response cache | [`app/response_cache.py`](../app/response_cache.py) |
| Fundamentals cache/orchestration | [`app/fundamentals.py`](../app/fundamentals.py) |
| FMP transport/retries | [`app/providers/fmp.py`](../app/providers/fmp.py) |
| Provider normalization | [`app/normalization.py`](../app/normalization.py) |
| Pure DCF calculation | [`app/dcf_engine.py`](../app/dcf_engine.py) |
| Internal dataclasses | [`app/models.py`](../app/models.py) |
| Public request/response schemas | [`app/schemas.py`](../app/schemas.py) |
| ETag and HTTP caching | [`app/http_cache.py`](../app/http_cache.py) |
| Error types | [`app/exceptions.py`](../app/exceptions.py) |
| Landing-page/browser client | [`docs/index.html`](../docs/index.html) |
| Auth/quota/account database schema | [`supabase/migrations/001_phase5_auth_usage.sql`](../supabase/migrations/001_phase5_auth_usage.sql), [`supabase/migrations/002_phase6_customer_login.sql`](../supabase/migrations/002_phase6_customer_login.sql) |
