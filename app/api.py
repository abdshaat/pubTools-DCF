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
from uuid import uuid4

from fastapi import FastAPI, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

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
from .schemas import ErrorResponse, ValuationResponse, build_valuation_response

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


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}

_LANDING_PAGE_CSP = "; ".join(
    [
        "default-src 'self'",
        "base-uri 'none'",
        "connect-src 'self' http://127.0.0.1:* http://localhost:*",
        "form-action 'none'",
        "frame-ancestors 'none'",
        "img-src 'self' data:",
        "object-src 'none'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
    ]
)


def create_app(
    fmp_client: FMPClient | None = None,
    ttl_seconds: float = 4 * 3600,
    profile_ttl_seconds: float = 24 * 3600,
    quote_ttl_seconds: float = 60,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_client = fmp_client is None
        client = fmp_client or FMPClient(raw_sink=_default_raw_sink())
        app.state.fundamentals = FundamentalsService(
            client,
            ttl_seconds=ttl_seconds,
            profile_ttl_seconds=profile_ttl_seconds,
            quote_ttl_seconds=quote_ttl_seconds,
        )
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

    @app.middleware("http")
    async def _request_id(request: Request, call_next: Any) -> JSONResponse:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers.update(_SECURITY_HEADERS)
        return response

    # --- error mapping (see app/exceptions.py for the rationale) ---

    def _error(
        request: Request,
        status: int,
        detail: Any,
        code: str,
        message: str,
        fields: list[dict[str, str]] | None = None,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={
                "detail": jsonable_encoder(detail),
                "error": {
                    "version": "1",
                    "code": code,
                    "message": message,
                    "request_id": request.state.request_id,
                    "fields": fields or [],
                },
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        detail = exc.errors()
        fields = [
            {
                "field": str(error["loc"][-1]),
                "code": str(error["type"]),
                "message": str(error["msg"]),
            }
            for error in detail
        ]
        return _error(
            request,
            422,
            detail,
            "request_validation_failed",
            "Request parameters failed validation.",
            fields,
        )

    @app.exception_handler(DCFValidationError)
    async def _validation_error(request: Request, exc: DCFValidationError) -> JSONResponse:
        detail = [{"field": exc.field, "message": exc.message}]
        fields = [{"field": exc.field, "code": "invalid_value", "message": exc.message}]
        return _error(
            request, 422, detail, "invalid_assumptions", "DCF assumptions are invalid.", fields
        )

    @app.exception_handler(UnsupportedSectorError)
    async def _sector_error(request: Request, exc: UnsupportedSectorError) -> JSONResponse:
        detail = [{"field": "ticker", "message": str(exc)}]
        fields = [{"field": "ticker", "code": "unsupported_sector", "message": str(exc)}]
        return _error(
            request, 422, detail, "unsupported_sector", "Ticker sector is unsupported.", fields
        )

    @app.exception_handler(TickerNotFoundError)
    async def _not_found(request: Request, exc: TickerNotFoundError) -> JSONResponse:
        detail = f"ticker not found: {exc.ticker}"
        return _error(request, 404, detail, "ticker_not_found", detail)

    @app.exception_handler(TickerNotCoveredError)
    async def _not_covered(request: Request, exc: TickerNotCoveredError) -> JSONResponse:
        # 404: from the customer's side there is no valuation to return for
        # this ticker. The message explains the cause (may not exist, or may
        # be outside our data coverage) without leaking that it's our upstream
        # subscription — the customer can't act on that.
        return _error(request, 404, str(exc), "ticker_unavailable", str(exc))

    @app.exception_handler(NormalizationError)
    async def _normalization_error(request: Request, exc: NormalizationError) -> JSONResponse:
        detail = f"provider data for {exc.ticker} could not be normalized"
        return _error(request, 502, detail, "normalization_failed", detail)

    @app.exception_handler(ProviderAuthError)
    async def _auth_error(request: Request, exc: ProviderAuthError) -> JSONResponse:
        detail = "data provider authentication is misconfigured"
        return _error(request, 500, detail, "provider_auth_misconfigured", detail)

    @app.exception_handler(ProviderError)
    async def _provider_error(request: Request, exc: ProviderError) -> JSONResponse:
        detail = "data provider is unavailable, try again shortly"
        return _error(request, 503, detail, "provider_unavailable", detail)

    # --- routes ---

    @app.get("/", include_in_schema=False)
    async def landing_page() -> FileResponse:
        return FileResponse(
            FilePath(__file__).parent.parent / "docs" / "index.html",
            headers={"Content-Security-Policy": _LANDING_PAGE_CSP},
        )

    @app.get(
        "/v1/valuations/{ticker}",
        response_model=ValuationResponse,
        responses={
            400: {"model": ErrorResponse, "description": "Malformed request"},
            401: {"model": ErrorResponse, "description": "Authentication required (reserved)"},
            403: {"model": ErrorResponse, "description": "Insufficient scope (reserved)"},
            404: {"model": ErrorResponse, "description": "Ticker unavailable"},
            422: {"model": ErrorResponse, "description": "Invalid request or assumptions"},
            429: {"model": ErrorResponse, "description": "Rate limit exceeded (reserved)"},
            500: {"model": ErrorResponse, "description": "Server configuration error"},
            502: {"model": ErrorResponse, "description": "Provider data normalization failed"},
            503: {"model": ErrorResponse, "description": "Provider unavailable"},
        },
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
            description="Discount rate as a decimal; finite and between 0.001 and 0.50",
        ),
        terminal_growth: float = Query(
            description="Finite perpetual growth rate from -0.10 to 0.10; must be below wacc",
        ),
        ebit_margin: float = Query(
            description="Finite projected EBIT margin from -1.0 to 1.0",
        ),
        revenue_growth: str = Query(
            description=(
                "Single decimal applied to every year (0.05) or "
                "comma-separated per-year values (0.08,0.07,0.06,0.05,0.04)"
            ),
        ),
        tax_rate: float = Query(
            default=0.21,
            description="Finite effective tax rate from 0.0 to 1.0; defaults to 0.21",
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
        return build_valuation_response(
            base,
            assumptions,
            valuation,
            grid,
            request_id=request.state.request_id,
        )

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "model_version": MODEL_VERSION}

    return app


# Default instance for `uvicorn app.api:app` (real FMP client, needs FMP_API_KEY)
app = create_app()
