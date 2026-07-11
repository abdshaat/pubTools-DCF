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

**v1 scope:** non-financial US large caps. Banks and insurers are rejected
with a 422 (standard FCF DCF doesn't apply). Unknown tickers return 404.
Assumptions that break the math (e.g. `terminal_growth >= wacc`) return 422
with per-field messages.

## Architecture

| Layer | Where | Status |
|---|---|---|
| DCF engine — pure, deterministic, no I/O | `app/dcf_engine.py` | ✅ done |
| Data models (canonical schema) | `app/models.py` | ✅ done |
| Ingestion — Financial Modeling Prep client with retries/backoff | `app/providers/fmp.py` | ✅ done |
| Normalization — provider payloads → canonical `BaseFinancials` | `app/normalization.py` | ✅ done |
| Fundamentals service — fetch + normalize + TTL cache | `app/fundamentals.py` | ✅ done |
| API layer — FastAPI routes, validation, error mapping | — | 🚧 next |
| Postgres / Redis storage | — | planned |

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

### Run the tests (no API key needed)

```bash
python -m pytest -q
```

All data-layer tests run against recorded fixture payloads
(`tests/fixtures/fmp/`) via a mock transport — no network required.

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
  models.py         # BaseFinancials, Assumptions, YearProjection, Valuation
  dcf_engine.py     # compute_dcf() — the pure valuation core
  exceptions.py     # domain errors + their HTTP mappings
  normalization.py  # FMP payloads -> canonical schema (sign conventions live here)
  fundamentals.py   # FundamentalsService: fetch + normalize + TTL cache
  providers/fmp.py  # async FMP client: retries, backoff, raw-response sink
scripts/
  smoke_fetch.py    # live end-to-end check against the real FMP API
tests/
  test_dcf_engine.py   # spreadsheet-verified cases, validation, hypothesis invariants
  test_data_layer.py   # client + normalization tests over fixture payloads
  fixtures/fmp/        # recorded provider responses (AAPL, JPM)
```

## Notes for contributors

- All monetary values are **raw dollars** end to end — never thousands/millions.
- Never compare floats for equality in money math; tests use `pytest.approx`.
- Provider sign conventions are handled in `app/normalization.py` and
  documented there (FMP reports capex negative; `changeInWorkingCapital` is
  cash impact and gets sign-flipped). Don't "fix" these.
- `CLAUDE.md` holds the full design spec and decisions already made.