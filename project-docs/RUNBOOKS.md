# Runbooks

Operational procedures for the DCF Valuation API (Phase 11 Slice 4). Each one
starts from a symptom you can see without a debugger, because the point of
Phase 11 is that logs and `/ready` answer the first question.

**Before anything else**, in this order:

1. `GET /health` — is the process alive? (Never touches a dependency.)
2. `GET /ready` — which dependency is unhappy? Look at the entry whose
   `status` is not `ok`, and whether it is `required`.
3. `GET /internal/metrics` with `Authorization: Bearer $METRICS_TOKEN` —
   `dcf_errors_total{code=…}` names the failing category; `dcf_http_requests_total`
   shows whether it is everything or one route.
4. The access log line for a failing request: `request_id`, `route`, `status`,
   `duration_ms`, `cache`, `t_*_ms` per stage, `*_calls` per service. The
   `t_*_ms` fields say *which dependency* made a request slow.

Every customer-visible error carries a `request_id`; ask for it first.

---

## 1. Incident response — "valuations are failing"

| Symptom | Likely cause | Confirm | Fix |
|---|---|---|---|
| `503 snapshot_store_unavailable` | Supabase unreachable, or a migration is missing | `/ready` shows `supabase: unavailable`; logs show `error_code=snapshot_store_unavailable` | Restore Supabase (status page); if it is reachable, check the migration list in §4 — a missing table reads as a storage error, not a miss, by design |
| `503 auth_storage_unavailable` on every request | same | `/ready` as above | as above |
| `502` with `error_code=provider_unavailable` | FMP outage or key rejected | `dcf_provider_calls_total{service="fmp"}` climbing with `dcf_errors_total{code="provider_unavailable"}` | see §3 |
| `null current_price` + warning on every response | Finnhub outage/rate limit/misconfig | `dcf_price_lookups_total{result="unavailable"}` | see §3; the valuation itself is unaffected by design (ADR-008) |
| Widespread `429` | quota exhausted or the daily limit is too low | `dcf_quota_rejections_total` rising | raise the key's `daily_quota` in Supabase, or the deployment-wide `DAILY_RATE_LIMIT` |
| All requests `401` | `SUPABASE_*` present but keys wrong; or the caller's key was revoked | logs show `status=401` with `route=/v1/valuations/{ticker}` | verify the key row in Supabase (`revoked`, `expires_at`) |
| Slow responses | one dependency | compare `t_statements_ms`, `t_price_ms`, `t_quota_ms`, `t_auth_ms` on one line | the largest one names the culprit |

**The app boots but every request 500s** → check the startup log: invalid
configuration raises `SettingsError` naming the variable (Phase 11 Slice 1). A
deploy that changed an env var is the first suspect.

---

## 2. Key rotation

**Customer API keys** (`X-API-Key`)

1. `POST /v1/account/keys/{id}/rotate` from the account UI, or issue a new key
   with `scripts/create_api_key.py`.
2. The old secret stops verifying immediately; only the hash is stored, so the
   old value cannot be recovered — communicate before rotating.
3. Confirm with a valuation request using the new key.

**`API_KEY_HASH_PEPPER`** — ⚠️ **destructive.** Every stored hash was computed
with the current pepper; changing it invalidates **all existing keys** at once.
There is no dual-pepper verification path today. If you must rotate it: plan a
window, rotate the pepper, then re-issue every key. (This is exactly why
`Settings` refuses to length-check the pepper at boot — a startup rejection
would turn a weak value into an outage with no safe fix.)

**`CRON_SECRET`** — set the new value in Vercel Production, redeploy, then
confirm the next 6 PM Eastern run appears in `financial_refresh_runs`. A
mismatch is silent by design: the endpoint returns a generic 401 and no refresh
happens, so verify rather than assume.

**`METRICS_TOKEN`** — no customer impact; scrapers 401 until updated.

**`SUPABASE_SERVICE_ROLE_KEY`** — rotate in Supabase, update Vercel, redeploy.
Auth, quota, metering, and the snapshot store all fail closed until the deploy
completes: expect 503s, not wrong answers.

---

## 3. Provider outage

**FMP (statements).** Cached and stored tickers keep serving — that is the whole
point of the ADR-006 read-through: L1 → Redis → Supabase snapshot → FMP, and
only a genuinely cold ticker reaches the provider. Expected behavior:

- Existing tickers: unaffected.
- Cold tickers: `502 provider_unavailable` after the bounded retry ladder.
- The 6 PM Eastern refresh: failed claims, run ends `partial_failed`, previous
  snapshots stay active, and responses carry a freshness warning.

Do **not** disable the refresh to "reduce load" — a failed claim is the durable
record that a ticker was not confirmed, and skipping the run loses it.

**Finnhub (price).** Every response degrades to `current_price: null`,
`upside_pct: null`, plus a warning; the DCF math never depends on the market
price (ADR-008). No action beyond waiting, unless
`dcf_price_lookups_total{result="unavailable"}` stays high after the provider
recovers — then check `FINNHUB_API_KEY`.

**Rate limits.** FMP free tier ≈250 calls/day and Finnhub 60/min. If FMP's daily
budget is exhausted, cold tickers fail until midnight; warm ones are unaffected.
Re-run the capacity gate (`TODO.md` §4.4) before adding tickers to the manifest.

---

## 4. Migrations and bad-data rollback

**Ordering rule, learned the hard way on 2026-07-18:** apply the migration
*before* deploying code that reads it. With Supabase configured, a missing table
is a storage error (503), not a cache miss — the deploy that skipped this made
production 503 on keyed valuations until migration 003 was applied.

Applied so far: `001_phase5_auth_usage`, `002_phase6_customer_login`,
`003_phase8_snapshots`, `004_phase8_freshness`.

**Bad normalized data for one ticker** (wrong filing selected, restatement
mis-picked):

1. Reproduce offline first — no provider calls:
   `python scripts/raw_captures.py replay TICKER`. That renormalizes the stored
   provider evidence and prints exactly what the API would derive.
   *(Only available where the local capture sink runs; production evidence is
   Phase 10 Slice 2, still open.)*
2. `normalized_snapshots` rows are immutable and content-addressed — never edit
   one. Roll forward: fix normalization, then let the next daily refresh write a
   new snapshot and move the ticker head.
3. To force it sooner, run the refresh endpoint inside the 6 PM Eastern window
   with the cron secret, or delete the ticker's cache entries so the next
   request re-bootstraps it.

**Suspected cache poisoning / stale serves.** Redis entries are versioned
envelopes; a corrupt or unknown-version entry is deleted and treated as a miss
automatically. To force a global refresh, flush the `dcf:v1:*` keyspace — it is
a cache, so the cost is latency and provider calls, never correctness.

---

## 5. Model-version rollback

`model_version` (currently `0.2.0`) identifies the calculation contract; every
response carries it, and `data_version` fingerprints the inputs.

1. Decide whether the defect is in the **math** (engine) or the **data**
   (normalization/provider). The response's `base_financials` plus the
   projection bridge lets you reconstruct the arithmetic without rerunning it.
2. Roll back by deploying the previous commit — the engine is pure and
   stateless, so nothing needs undoing beyond the deploy.
3. If the rollback changes results, bump `model_version` again on the way back
   and note it in `PROGRESS.md`. Never let two deployments answer differently
   under the same `model_version`; that is the one thing that makes an
   auditable response un-auditable.
4. Durable snapshots are unaffected by model rollbacks: they store normalized
   *inputs*, not results.

---

## 6. Proposed SLOs — **needs owner sign-off**

Derived from the measured baseline in `IMPLEMENTATION_PLAN.md`. These are
proposals, not commitments: an SLO is a promise about the product, so the owner
accepts or edits the numbers before they mean anything.

| Objective | Proposed target | Window | Why this number |
|---|---|---|---|
| Availability, valuation endpoint | 99.5% non-5xx | 30 days | Single-region Vercel + Supabase free tier; 99.9% would promise more than the dependencies do |
| Latency, warm valuation | p95 < 900 ms | 30 days | ~4 external round trips dominate; in-process work is ~2 ms |
| Latency, cold valuation | p95 < 4 s | 30 days | Four FMP calls plus bounded retries, inside the function timeout |
| Sign-in success | 99% of attempts reach Supabase without a 5xx | 30 days | Depends on Supabase's own auth availability |
| Data freshness | ≥95% of tickers refreshed each Eastern day | 7 days | One daily run; a single failed claim on a small manifest is a large percentage |
| Price availability | ≥98% of valuations carry a live price | 7 days | Finnhub free tier; the degrade path is designed, not an outage |

Suggested alert thresholds (page vs. notice):

- **Page:** `/ready` reports `supabase: unavailable` for >5 minutes; 5xx rate
  >2% over 10 minutes; the daily refresh run ends `failed`.
- **Notice:** `dcf_price_lookups_total{result="unavailable"}` >10% over an hour;
  refresh ends `partial_failed`; `dcf_quota_rejections_total` rising for a
  single key (a customer needs a higher quota, or is looping).

---

## 7. Deployment checklist

1. Migrations applied **before** the deploy that reads them (§4).
2. Environment variables present for the environment being deployed —
   invalid ones now fail at boot with the variable named.
3. After deploy: `/health`, `/ready`, one valuation, and `/openapi.json`.
4. For a release that changes calculations: bump `model_version`, regenerate the
   OpenAPI snapshot, and record the before/after in `PROGRESS.md`.
5. Never combine an API-contract migration with a model-methodology change in
   one release (standing rule in the implementation plan).
