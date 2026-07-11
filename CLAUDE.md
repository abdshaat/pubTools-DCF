# DCF Valuation API — Project Context

## What this project is
An API that computes a discounted cash flow (DCF) valuation for a single stock ticker
using customer-supplied assumptions. The customer sends a ticker plus assumptions in
one request; the API fetches the company's financials, runs the DCF, and returns the
intrinsic value with the full projection so the math is auditable.

## Key design decisions (already made — do not revisit unless asked)
- **API style:** REST, **GET** endpoint (chosen over POST so responses are HTTP-cacheable).
  One ticker per request; ticker in the path, assumptions as query params.
  - Example: `GET /v1/valuations/AAPL?wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.08,0.07,0.06,0.05,0.04&projection_years=5`
  - `revenue_growth` accepts a single value or comma-separated per-year values.
  - Normalize query params into canonical order server-side before using the URL as a cache key.
- **Language/framework:** Python + FastAPI (async, auto validation of query params, OpenAPI docs).
- **Architecture layers:**
  1. Ingestion service — fetch financials from data provider (start with a pre-parsed
     API like Financial Modeling Prep rather than raw SEC EDGAR XBRL); handle retries,
     rate-limit backoff; store raw responses.
  2. Normalization layer — map provider data into a canonical schema: revenue, EBIT,
     D&A, capex, change in NWC, debt, cash, diluted shares.
  3. Storage/cache — Postgres for normalized statements; Redis (or in-process cache)
     for fundamentals keyed per ticker with hours/days TTL (fundamentals change quarterly).
  4. DCF engine — pure, deterministic, stateless function:
     `dcf(base_financials, assumptions) -> valuation`. No I/O inside. Unit-test heavily
     against hand-built spreadsheet DCFs.
  5. API layer — FastAPI routes, validation, error handling.

## DCF engine spec
- Project unlevered free cash flow over `projection_years` (default 5).
- FCF = EBIT × (1 − tax) + D&A − capex − ΔNWC.
- Discount at customer-supplied WACC.
- Terminal value: Gordon growth — TV = FCF_final × (1 + g) / (WACC − g).
- Equity value = PV(FCFs) + PV(TV) − net debt; per-share = equity value / diluted shares.
- Response must include: base financials used (with `source_period`), echoed resolved
  assumptions, full year-by-year projection (revenue, FCF, PV of FCF), terminal value,
  PV of terminal value, enterprise value, equity value, intrinsic value per share,
  current price, upside %. Include a `model_version` string.
- Optional: small sensitivity grid (WACC ±1% × terminal growth ±0.5%).

## Validation rules (return 422 with per-field messages)
- Reject `terminal_growth >= wacc` (Gordon formula explodes).
- `projection_years` in 3–15; growth rates within ±50%; no negative tax rates.
- Unknown ticker → 404. Unsupported sector (banks/insurers — standard FCF DCF doesn't
  apply) → 422 with explanation. v1 scope: non-financial US large caps only.

## Known risks / things to watch
- Data normalization is the hardest part: inconsistent line items, fiscal-year offsets,
  restatements, multiple share classes.
- DCF output is extremely sensitive to WACC and terminal growth — guardrails and
  sensitivity ranges matter more than the point estimate.
- Never compare floats for equality in money math; pick one unit convention
  (raw dollars vs thousands) and enforce it everywhere.
- Meter customers per API key separately from upstream provider rate limits.
- Frame outputs as model estimates, not investment recommendations.

## Suggested first milestones
1. Scaffold FastAPI project: routes, pydantic models for assumptions/response.
2. Implement the pure DCF engine + unit tests (validate vs a spreadsheet for 2–3 companies).
3. Stub the data layer with fixture JSON for a few tickers; wire end-to-end.
4. Swap the stub for a real provider client with caching.
5. Add sensitivity grid, model_version, and error handling polish.
