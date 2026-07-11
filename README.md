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
- Every response includes a 3×3 **sensitivity grid** (WACC ±1% × terminal
  growth ±0.5%) by default — DCF outputs swing hard on those two inputs, so
  the range matters more than the point estimate. Opt out with
  `sensitivity=false`.

**v1 scope:** non-financial US large caps. Banks and insurers are rejected
with a 422 (standard FCF DCF doesn't apply). Tickers that don't exist or fall
outside the data provider's coverage return 404. Assumptions that break the
math (e.g. `terminal_growth >= wacc`) return 422 with per-field messages.

The fundamentals service also **negative-caches** these definitive rejections,
so a client repeatedly requesting a bad or uncovered ticker doesn't spend an
upstream provider call each time — the FMP free tier allows only ~250/day.

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
| Customer docs — API reference + interactive endpoint builder | `docs/index.html` | ✅ done |
| Postgres / Redis storage, per-key metering | — | planned |

The engine is a pure function — `compute_dcf(base_financials, assumptions) ->
valuation` — so it's unit-tested in isolation against hand-computed spreadsheet
cases and property-based invariants (equity value must fall as WACC rises,
rise with terminal growth, and enterprise value must always reconcile to
ΣPV(FCF) + PV(terminal value)).

## Getting started

Requires Python 3.11+.

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

All 63 tests — engine, data layer, and API — run against recorded fixture
payloads (`tests/fixtures/fmp/`) via a mock transport. No network required.

### Run the API server

```bash
# needs FMP_API_KEY set (see below)
uvicorn app.api:app --reload
```

Then try it:

```
GET http://127.0.0.1:8000/v1/valuations/AAPL?wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05
```

Interactive OpenAPI docs at `http://127.0.0.1:8000/docs`; health probe at
`/health`. Customer-facing documentation — including an endpoint builder
that turns a form of assumptions into a ready-to-hit URL — lives in
[`docs/index.html`](docs/index.html).

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
  index.html        # customer API reference + interactive endpoint builder
scripts/
  smoke_fetch.py    # live end-to-end check against the real FMP API
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
