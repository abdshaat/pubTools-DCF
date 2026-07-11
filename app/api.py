"""FastAPI layer: routes, request validation, and error mapping.

Run locally:
    uvicorn app.api:app --reload
Interactive docs at http://127.0.0.1:8000/docs

The FMP client and FundamentalsService are created once at startup (lifespan)
and shared across requests so the TTL cache persists. Tests inject a
fixture-backed FMPClient via create_app(fmp_client=...).

Validation strategy: FastAPI/pydantic handles types and required params;
all *domain* rules (terminal_growth < wacc, growth bounds, year range, ...)
live in the DCF engine's validator so there is a single source of truth.
DCFValidationError is mapped here to a 422 with a per-field message, matching
the format FastAPI uses for its own validation errors closely enough that
callers handle one shape.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Any

from fastapi import FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse

from . import MODEL_VERSION
from .dcf_engine import DCFValidationError, compute_dcf, compute_sensitivity_grid
from .exceptions import (
    NormalizationError,
    ProviderAuthError,
    ProviderError,
    TickerNotCoveredError,
    TickerNotFoundError,
    UnsupportedSectorError,
)
from .fundamentals import FundamentalsService
from .models import Assumptions
from .providers.fmp import FileRawSink, FMPClient
from .schemas import ValuationResponse, build_valuation_response

# Load a local .env (gitignored) so `uvicorn app.api:app` picks up FMP_API_KEY
# without the developer having to export it every shell. No-op if python-dotenv
# isn't installed or no .env exists; never overrides a var already in the
# environment.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _default_raw_sink() -> FileRawSink | None:
    """Persist provider payloads locally except on Vercel.

    Vercel Functions are stateless and deployment files must not be treated as
    durable storage. Production audit payloads will move to external object
    storage; disabling this sink keeps requests independent of ephemeral files.
    """
    if os.environ.get("VERCEL"):
        return None
    return FileRawSink(FilePath(__file__).parent.parent / "data" / "raw")


def _parse_revenue_growth(raw: str) -> list[float]:
    """'0.05' -> [0.05]; '0.08,0.07,0.06' -> [0.08, 0.07, 0.06]."""
    try:
        values = [float(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError:
        raise DCFValidationError(
            "revenue_growth", "must be a number or comma-separated numbers"
        ) from None
    if not values:
        raise DCFValidationError("revenue_growth", "must not be empty")
    return values


def create_app(
    fmp_client: FMPClient | None = None,
    ttl_seconds: float = 4 * 3600,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_client = fmp_client is None
        client = fmp_client or FMPClient(raw_sink=_default_raw_sink())
        app.state.fundamentals = FundamentalsService(client, ttl_seconds=ttl_seconds)
        try:
            yield
        finally:
            if owns_client:
                await client.aclose()

    app = FastAPI(
        title="DCF Valuation API",
        version=MODEL_VERSION,
        description=(
            "Discounted cash flow valuations from caller-supplied assumptions. "
            "Outputs are model estimates, not investment recommendations."
        ),
        lifespan=lifespan,
    )

    # --- error mapping (see app/exceptions.py for the rationale) ---

    def _error(status: int, detail: Any) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": detail})

    @app.exception_handler(DCFValidationError)
    async def _validation_error(request: Request, exc: DCFValidationError) -> JSONResponse:
        return _error(422, [{"field": exc.field, "message": exc.message}])

    @app.exception_handler(UnsupportedSectorError)
    async def _sector_error(request: Request, exc: UnsupportedSectorError) -> JSONResponse:
        return _error(422, [{"field": "ticker", "message": str(exc)}])

    @app.exception_handler(TickerNotFoundError)
    async def _not_found(request: Request, exc: TickerNotFoundError) -> JSONResponse:
        return _error(404, f"ticker not found: {exc.ticker}")

    @app.exception_handler(TickerNotCoveredError)
    async def _not_covered(request: Request, exc: TickerNotCoveredError) -> JSONResponse:
        # 404: from the customer's side there is no valuation to return for
        # this ticker. The message explains the cause (may not exist, or may
        # be outside our data coverage) without leaking that it's our upstream
        # subscription — the customer can't act on that.
        return _error(404, str(exc))

    @app.exception_handler(NormalizationError)
    async def _normalization_error(request: Request, exc: NormalizationError) -> JSONResponse:
        return _error(502, f"provider data for {exc.ticker} could not be normalized")

    @app.exception_handler(ProviderAuthError)
    async def _auth_error(request: Request, exc: ProviderAuthError) -> JSONResponse:
        return _error(500, "data provider authentication is misconfigured")

    @app.exception_handler(ProviderError)
    async def _provider_error(request: Request, exc: ProviderError) -> JSONResponse:
        return _error(503, "data provider is unavailable, try again shortly")

    # --- routes ---

    @app.get(
        "/v1/valuations/{ticker}",
        response_model=ValuationResponse,
        summary="DCF valuation for one ticker",
    )
    async def get_valuation(
        request: Request,
        ticker: str = Path(
            min_length=1,
            max_length=10,
            pattern=r"^[A-Za-z][A-Za-z.\-]*$",
            description="US stock ticker, e.g. AAPL",
        ),
        wacc: float = Query(
            description="Discount rate as a decimal, e.g. 0.09 for 9%",
        ),
        terminal_growth: float = Query(
            description="Perpetual growth rate; must be below wacc",
        ),
        ebit_margin: float = Query(
            description="Projected EBIT margin applied to every year, e.g. 0.30",
        ),
        revenue_growth: str = Query(
            description=(
                "Single decimal applied to every year (0.05) or "
                "comma-separated per-year values (0.08,0.07,0.06,0.05,0.04)"
            ),
        ),
        tax_rate: float = Query(
            default=0.21,
            description="Effective tax rate; defaults to 0.21",
        ),
        projection_years: int = Query(
            default=5,
            description="Explicit forecast horizon, 3-15 years",
        ),
        sensitivity: bool = Query(
            default=True,
            description=(
                "Include a 3x3 sensitivity grid (WACC +/-1% x terminal growth "
                "+/-0.5%). Pass false to omit it."
            ),
        ),
    ) -> ValuationResponse:
        growth_values = _parse_revenue_growth(revenue_growth)
        assumptions = Assumptions(
            wacc=wacc,
            terminal_growth=terminal_growth,
            tax_rate=tax_rate,
            ebit_margin=ebit_margin,
            projection_years=projection_years,
            revenue_growth=growth_values[0] if len(growth_values) == 1 else growth_values,
        )

        base = await request.app.state.fundamentals.get_base_financials(ticker)
        valuation = compute_dcf(base, assumptions)
        grid = compute_sensitivity_grid(base, assumptions) if sensitivity else None
        return build_valuation_response(base, assumptions, valuation, grid)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "model_version": MODEL_VERSION}

    return app


# Default instance for `uvicorn app.api:app` (real FMP client, needs FMP_API_KEY)
app = create_app()
