# pubTools-DCF

A REST API that computes a discounted cash flow (DCF) valuation for a single
stock ticker using caller-supplied assumptions. Send a ticker plus your
assumptions in one request; the API fetches the company's financials, runs the
DCF, and returns the intrinsic value with the full year-by-year projection so
the math is auditable.

> Outputs are model estimates driven entirely by your assumptions — not
> investment advice or recommendations.

## How it works

```
GET /v1/valuations/AAPL?wacc=0.09&terminal_growth=0.025&ebit_margin=0.30
    &revenue_growth=0.08,0.07,0.06,0.05,0.04&projection_years=5
```

- Unlevered free cash flow projected over `projection_years` (3–15, default 5):
  `FCF = EBIT × (1 − tax) + D&A − capex − ΔNWC`
- Discounted at your WACC; terminal value via Gordon growth:
  `TV = FCF_final × (1 + g) / (WACC − g)`
- Equity value = PV(FCFs) + PV(TV) − net debt; per-share = equity / diluted shares
- `revenue_growth` takes a single value or one value per projection year
- Safety bounds: WACC 0.1–50%, terminal growth -10–10% and below WACC,
  EBIT margin -100–100%, tax rate 0–100%, and annual revenue growth ±50%.
  NaN and infinity are rejected for every caller- and provider-derived number.
- Negative enterprise, equity, or per-share estimates are valid model outcomes:
  the API returns them without clipping and includes explanatory warnings.
- Calculations use finite IEEE-754 double-precision values without intermediate
  rounding. API numbers are raw calculation values; clients should round only
  for display. Billing amounts will use integer minor units or decimal arithmetic,
  separately from the valuation engine.
- Every response includes a 3×3 **sensitivity grid** (WACC ±1% × terminal
  growth ±0.5%) by default — DCF outputs swing hard on those two inputs, so
  the range matters more than the point estimate. Opt out with
  `sensitivity=false`.
- Every projected year includes the complete FCF bridge (growth, margin, taxes,
  NOPAT, D&A, capex, NWC, discount period/factor, FCF, and PV). Responses also
  include request/computation IDs, currency/units, provider and data versions,
  statement/quote dates, model version, warnings, and a disclaimer.

**v1 scope:** non-financial US large caps. Banks and insurers are rejected
with a 422 (standard FCF DCF doesn't apply). Tickers that don't exist or fall
outside the data provider's coverage return 404. Assumptions that break the
math (e.g. `terminal_growth >= wacc`) return 422 with per-field messages.

### Rate limit

Valuation requests are capped at **100 per API key per UTC day** by default.
Calls made through the website use the same `/v1/valuations/{ticker}` endpoint
and count toward the same key quota. Responses include `X-RateLimit-Limit`,
`X-RateLimit-Remaining`, and `X-RateLimit-Reset`; exhausted callers receive
`429` with `Retry-After`.

Production deployments should set `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY`. When both are present, `/v1/valuations/{ticker}`
requires `X-API-Key`, validates hashed key records from Supabase, consumes a
shared atomic daily quota, and records usage events. Without Supabase
configuration, the app falls back to the in-process limiter for local
development and tests only.

The fundamentals service also **negative-caches** these definitive rejections,
so a client repeatedly requesting a bad or uncovered ticker doesn't spend an
upstream provider call each time — the FMP free tier allows only ~250/day.

### Statement freshness

The API fetches up to five annual candidates from each financial-statement
endpoint. It joins income, balance-sheet, and cash-flow records by annual period
and exact statement date, verifies fiscal-year and currency compatibility, then
selects the newest complete filing set. Duplicate periods prefer the newest
accepted/filing date. A newer incomplete period is never mixed into an older
complete set; it produces a data-quality warning instead. If no compatible set
exists, the API fails with a controlled normalization error rather than valuing
inconsistent data.

Statements/profile data use a long cache lifetime, while quotes use a separate
60-second default TTL. A failed quote refresh may use a cached quote for at most
15 minutes and reports that fallback in `warnings`; older prices fail rather than
silently appearing current. `fundamentals_as_of`, `fiscal_year`,
`statement_period`, filing metadata, `price_as_of`, and `price_fetched_at` make
the selected data auditable.

## Architecture

| Layer | Where | Status |
|---|---|---|
| DCF engine — pure, deterministic, no I/O | `app/dcf_engine.py` | ✅ done |
| Sensitivity grid — WACC × terminal growth | `app/dcf_engine.py` | ✅ done |
| Data models (canonical schema) | `app/models.py` | ✅ done |
| Ingestion — Financial Modeling Prep client with retries/backoff | `app/providers/fmp.py` | ✅ done |
| Normalization — provider payloads → canonical `BaseFinancials` | `app/normalization.py` | ✅ done |
| Fundamentals service — fetch + normalize + TTL cache | `app/fundamentals.py` | ✅ done |
| API layer — FastAPI routes, validation, error mapping | `app/api.py`, `app/schemas.py` | ✅ done |
| Customer website — landing page, API reference, and live valuation UI | `docs/index.html` served at `/` | ✅ done |
| Supabase auth/quota/metering | `app/supabase.py`, `supabase/migrations/` | code ready; DB setup required |

The engine is a pure function — `compute_dcf(base_financials, assumptions) ->
valuation` — so it's unit-tested in isolation against hand-computed spreadsheet
cases and property-based invariants (equity value must fall as WACC rises,
rise with terminal growth, and enterprise value must always reconcile to
ΣPV(FCF) + PV(terminal value)).

## Getting started

Requires Python 3.11+.

Runtime dependencies are intentionally small for open-source and Vercel use:
FastAPI for the HTTP surface and httpx for provider calls. Local conveniences
such as `uvicorn` and `.env` loading live in the `dev` extra.

```bash
python -m venv .venv
# Windows:
.venv\Scripts\python -m pip install -e ".[dev]"
# macOS/Linux:
.venv/bin/python -m pip install -e ".[dev]"
```

If a Windows Store-created `.venv` becomes locked or its base interpreter is
removed, install the standard python.org distribution and create a replacement
under a new name without deleting the locked directory:

```powershell
$python = "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
& $python -m venv .venv313
.\.venv313\Scripts\python.exe -m pip install -e ".[dev]"
```

Use `.venv313` in place of `.venv` in the commands below. Vercel currently
supports Python 3.13, and the project declares its supported range in
`pyproject.toml`.

### Run the tests (no API key needed)

```bash
python -m pytest -q
```

The test suite — engine, data layer, API, auth, quota, and Supabase integration
contracts — runs against recorded fixture payloads (`tests/fixtures/fmp/`) and
mock transports. No network required.

### Configure Supabase auth and quotas

Run `supabase/migrations/001_phase5_auth_usage.sql` in your Supabase project,
then set these Vercel server-side environment variables:

```bash
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
```

Create an API key from a trusted terminal:

```bash
python scripts/create_api_key.py --customer-name "Example Customer"
```

Report recent usage events:

```bash
python scripts/report_usage.py --limit 50
```

Only the generated key hash is stored. The full key is printed once and should
be copied into the customer's secret manager.

### Customer sign-in with GitHub (self-service API keys)

Phase 6 lets a customer sign in with GitHub and generate/manage their own API
keys from the website, instead of only through the admin script above. Human
login sessions are a separate credential class from `X-API-Key` machine auth —
a login session is never usable as an API key.

1. Run `supabase/migrations/002_phase6_customer_login.sql` (after `001`).
2. Create a GitHub OAuth App at
   [github.com/settings/developers](https://github.com/settings/developers) →
   New OAuth App. Set **Authorization callback URL** to:
   ```
   https://YOUR-PROJECT-REF.supabase.co/auth/v1/callback
   ```
3. In the Supabase dashboard: **Authentication → Providers → GitHub** — enable
   it and paste the GitHub OAuth App's Client ID and Client Secret.
4. In the Supabase dashboard: **Authentication → URL Configuration → Redirect
   URLs** — add `{PUBLIC_BASE_URL}/v1/auth/callback` for every environment
   (e.g. `http://127.0.0.1:8000/v1/auth/callback` for local dev, and your
   production URL).
5. Set `PUBLIC_BASE_URL` (see `.env.example`) to this deployment's own public
   URL, with no trailing slash, matching step 4 exactly.

No `SUPABASE_ANON_KEY` or GitHub client secret is needed in this app's own
environment — GitHub OAuth App credentials live only in the Supabase
dashboard, and the existing `SUPABASE_SERVICE_ROLE_KEY` doubles as the
`apikey` header Supabase Auth requires; it never reaches the browser.

Once configured, a customer visits `/`, opens **Your account**, signs in with
GitHub, and can create/label/revoke up to 5 active keys — each fixed to the
`valuation:read` scope and a 100/day quota. The admin script remains the path
for support-issued keys needing a custom quota or scope.

### Run the API server

```bash
# needs FMP_API_KEY set (see below)
uvicorn app.api:app --reload
```

Then try it:

```
GET http://127.0.0.1:8000/v1/valuations/AAPL?wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05
```

Customer landing page at `http://127.0.0.1:8000/`; interactive OpenAPI docs at
`http://127.0.0.1:8000/docs`; health probe at `/health`. The landing page is
served from [`docs/index.html`](docs/index.html) and includes a browser UI that
calls `/v1/valuations/{ticker}` directly from the current site origin.

### Fetch live data

Get an API key from [Financial Modeling Prep](https://site.financialmodelingprep.com/),
then:

```bash
# Windows (PowerShell):
$env:FMP_API_KEY = "your-key"
# macOS/Linux:
export FMP_API_KEY="your-key"

python scripts/smoke_fetch.py AAPL
```

This fetches and normalizes real financials, runs a DCF with placeholder
assumptions, and prints the result. Raw provider responses are saved under
`data/raw/` for auditing.

### Probe latency and bursts

Phase 4 keeps provider work inside a bounded budget: each FMP request uses a
6-second timeout, at most 2 retries, capped 2-second retry waits, and a default
provider concurrency of 3. The worst degraded path remains short enough to
return a controlled response within normal Vercel function limits.

Run the repeatable fixture-backed cold/warm/burst probe with no API key:

```bash
python scripts/load_probe.py
```

Probe a running local server or Vercel deployment:

```bash
python scripts/load_probe.py --base-url http://127.0.0.1:8000
python scripts/load_probe.py --base-url https://your-project.vercel.app
```

## Project layout

```
app/
  models.py         # BaseFinancials, Assumptions, YearProjection, Valuation, SensitivityGrid
  dcf_engine.py     # compute_dcf() + compute_sensitivity_grid() — the pure valuation core
  api.py            # FastAPI app factory, routes, exception -> HTTP mapping
  schemas.py        # pydantic wire models (dataclasses stay internal)
  exceptions.py     # domain errors + their HTTP mappings
  normalization.py  # FMP payloads -> canonical schema (sign conventions live here)
  fundamentals.py   # FundamentalsService: fetch + normalize + TTL cache
  providers/fmp.py  # async FMP client: retries, backoff, raw-response sink
docs/
  index.html        # customer landing page + live API valuation UI
scripts/
  smoke_fetch.py    # live end-to-end check against the real FMP API
  load_probe.py     # cold/warm/same-ticker burst latency probe
tests/
  test_dcf_engine.py   # spreadsheet-verified cases, validation, hypothesis invariants
  test_data_layer.py   # client + normalization tests over fixture payloads
  test_api.py          # full HTTP request -> JSON response tests, all error paths
  fixtures/fmp/        # recorded provider responses (AAPL, JPM)
```

## Notes for contributors

- All monetary values are **raw dollars** end to end — never thousands/millions.
- Never compare floats for equality in money math; tests use `pytest.approx`.
- Provider sign conventions are handled in `app/normalization.py` and
  documented there (FMP reports capex negative; `changeInWorkingCapital` is
  cash impact and gets sign-flipped). Don't "fix" these.
- `CLAUDE.md` holds the full design spec and decisions already made.
