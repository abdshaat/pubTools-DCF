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
from contextlib import asynccontextmanager, suppress
from inspect import isawaitable
from pathlib import Path as FilePath
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Path, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import MODEL_VERSION
from .accounts import (
    LOGIN_ATTEMPTS_DAILY_LIMIT,
    AccountAuthError,
    AccountKeyNotFoundError,
    AccountLimitError,
    build_github_login,
    clear_session_cookies,
    complete_github_login,
    create_key,
    get_current_customer,
    list_keys,
    public_base_url,
    revoke_key,
    set_oauth_cookies,
    set_session_cookies,
)
from .auth import VALUATION_SCOPE, APIKeyAuthenticator, AuthFailure, AuthFailureReason
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
from .rate_limit import DailyRequestLimiter, RateLimitResult
from .schemas import (
    AccountKeysOut,
    ApiKeyCreatedOut,
    CreateKeyRequest,
    ErrorResponse,
    MeOut,
    ValuationResponse,
    build_api_key_summary,
    build_valuation_response,
)
from .supabase import (
    SupabaseAPIKeyAuthenticator,
    SupabaseAuthClient,
    SupabaseClient,
    SupabaseConfig,
    SupabaseDailyQuotaLimiter,
    SupabaseError,
    SupabaseUsageMeter,
)

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


def _rate_limit_headers(result: RateLimitResult) -> dict[str, str]:
    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(result.reset_epoch),
    }
    if not result.allowed:
        headers["Retry-After"] = str(result.retry_after)
    return headers


def _auth_error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    headers = {"WWW-Authenticate": "ApiKey"} if status_code == 401 else None
    response = JSONResponse(
        status_code=status_code,
        content={
            "detail": message,
            "error": {
                "version": "1",
                "code": code,
                "message": message,
                "request_id": request.state.request_id,
                "fields": [],
            },
        },
        headers=headers,
    )
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers.update(_SECURITY_HEADERS)
    return response


def _storage_error_response(request: Request) -> JSONResponse:
    response = JSONResponse(
        status_code=503,
        content={
            "detail": "authentication and quota storage is unavailable",
            "error": {
                "version": "1",
                "code": "auth_storage_unavailable",
                "message": "Authentication and quota storage is temporarily unavailable.",
                "request_id": request.state.request_id,
                "fields": [],
            },
        },
    )
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers.update(_SECURITY_HEADERS)
    return response


def _unauthenticated_account_response(request: Request) -> JSONResponse:
    response = _auth_error_response(
        request, status_code=401, code="not_signed_in", message="Not signed in."
    )
    clear_session_cookies(response)
    return response


async def _resolve(value: Any) -> Any:
    if isawaitable(value):
        return await value
    return value


def _valuation_ticker_from_path(path: str) -> str | None:
    prefix = "/v1/valuations/"
    if not path.startswith(prefix):
        return None
    ticker = path[len(prefix) :].split("/", 1)[0]
    return ticker.upper() or None


def create_app(
    fmp_client: FMPClient | None = None,
    ttl_seconds: float = 4 * 3600,
    profile_ttl_seconds: float = 24 * 3600,
    quote_ttl_seconds: float = 60,
    daily_rate_limit: int = 100,
    rate_limiter: Any | None = None,
    authenticator: Any | None = None,
    usage_meter: Any | None = None,
    supabase_client: SupabaseClient | None = None,
    auth_client: SupabaseAuthClient | None = None,
) -> FastAPI:
    supabase_config = SupabaseConfig.from_env()
    configured_supabase_client = supabase_client or (
        SupabaseClient(supabase_config) if supabase_config is not None else None
    )
    configured_auth_client = auth_client or (
        SupabaseAuthClient(supabase_config) if supabase_config is not None else None
    )

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
            if configured_supabase_client is not None and supabase_client is None:
                await configured_supabase_client.aclose()
            if configured_auth_client is not None and auth_client is None:
                await configured_auth_client.aclose()

    app = FastAPI(
        title="DCF Valuation API",
        version=MODEL_VERSION,
        description=(
            "Discounted cash flow valuations from caller-supplied assumptions. "
            "Outputs are model estimates, not investment recommendations."
        ),
        lifespan=lifespan,
    )
    if authenticator is None and configured_supabase_client is not None:
        authenticator = SupabaseAPIKeyAuthenticator(configured_supabase_client, required=True)
    if rate_limiter is None and configured_supabase_client is not None:
        rate_limiter = SupabaseDailyQuotaLimiter(
            configured_supabase_client, default_limit=daily_rate_limit
        )
    if usage_meter is None and configured_supabase_client is not None:
        usage_meter = SupabaseUsageMeter(configured_supabase_client)

    app.state.rate_limiter = rate_limiter or DailyRequestLimiter(daily_rate_limit)
    app.state.authenticator = authenticator or APIKeyAuthenticator(required=False)
    app.state.usage_meter = usage_meter
    app.state.supabase_client = configured_supabase_client
    app.state.auth_client = configured_auth_client
    app.state.login_rate_limiter = DailyRequestLimiter(LOGIN_ATTEMPTS_DAILY_LIMIT)

    @app.middleware("http")
    async def _request_id(request: Request, call_next: Any) -> JSONResponse:
        request.state.request_id = str(uuid4())
        rate_limit: RateLimitResult | None = None
        principal = None
        quota_consumed = False
        rate_limited = False
        valuation_ticker = _valuation_ticker_from_path(request.url.path)
        if request.method == "GET" and request.url.path.startswith("/v1/valuations/"):
            try:
                principal = await _resolve(
                    request.app.state.authenticator.authenticate(
                        request.headers.get("X-API-Key"),
                        required_scope=VALUATION_SCOPE,
                    )
                )
            except AuthFailure as exc:
                if exc.reason is AuthFailureReason.INSUFFICIENT_SCOPE:
                    return _auth_error_response(
                        request,
                        status_code=403,
                        code="insufficient_scope",
                        message="API key does not have permission to access valuations.",
                    )
                return _auth_error_response(
                    request,
                    status_code=401,
                    code="invalid_api_key",
                    message="A valid API key is required to access valuations.",
                )
            except SupabaseError:
                return _storage_error_response(request)
            request.state.auth = principal

            identity = principal.key_id if principal is not None else "anonymous"
            limit = (
                principal.daily_quota
                if principal is not None and principal.daily_quota is not None
                else daily_rate_limit
            )
            try:
                rate_limit = await _resolve(
                    request.app.state.rate_limiter.check_and_increment(
                        identity=identity,
                        limit=limit,
                    )
                )
            except SupabaseError:
                return _storage_error_response(request)
            if not rate_limit.allowed:
                rate_limited = True
                response = JSONResponse(
                    status_code=429,
                    content={
                        "detail": "daily valuation request limit exceeded",
                        "error": {
                            "version": "1",
                            "code": "rate_limit_exceeded",
                            "message": "Daily valuation request limit exceeded.",
                            "request_id": request.state.request_id,
                            "fields": [],
                        },
                    },
                    headers=_rate_limit_headers(rate_limit),
                )
                response.headers["X-Request-ID"] = request.state.request_id
                response.headers.update(_SECURITY_HEADERS)
                if request.app.state.usage_meter is not None:
                    with suppress(SupabaseError):
                        await request.app.state.usage_meter.record(
                            request_id=request.state.request_id,
                            principal=principal,
                            method=request.method,
                            path=request.url.path,
                            status_code=response.status_code,
                            ticker=valuation_ticker,
                            quota_consumed=False,
                            rate_limited=True,
                        )
                return response
            quota_consumed = True

        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers.update(_SECURITY_HEADERS)
        if rate_limit is not None:
            response.headers.update(_rate_limit_headers(rate_limit))
        if request.app.state.usage_meter is not None and valuation_ticker is not None:
            with suppress(SupabaseError):
                await request.app.state.usage_meter.record(
                    request_id=request.state.request_id,
                    principal=principal,
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    ticker=valuation_ticker,
                    quota_consumed=quota_consumed,
                    rate_limited=rate_limited,
                )
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

    # --- customer login (GitHub via Supabase Auth) and self-service keys ---
    # Human browser sessions here are a distinct credential class from the
    # `X-API-Key` machine auth above: a session cookie never grants valuation
    # access, and an API key never grants access to these routes.

    @app.get("/v1/auth/github/login", include_in_schema=False)
    async def github_login(request: Request) -> Response:
        auth_client = request.app.state.auth_client
        if auth_client is None:
            return _auth_error_response(
                request,
                status_code=503,
                code="auth_not_configured",
                message="Sign-in is not configured.",
            )
        ip = request.client.host if request.client else "unknown"
        result = await _resolve(
            request.app.state.login_rate_limiter.check_and_increment(
                identity=ip, limit=LOGIN_ATTEMPTS_DAILY_LIMIT
            )
        )
        if not result.allowed:
            return _auth_error_response(
                request,
                status_code=429,
                code="login_rate_limited",
                message="Too many sign-in attempts. Try again later.",
            )
        url, state, verifier = build_github_login(auth_client)
        response = RedirectResponse(url=url, status_code=302)
        set_oauth_cookies(response, state=state, verifier=verifier)
        return response

    @app.get("/v1/auth/callback", include_in_schema=False)
    async def github_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        base = public_base_url()

        def error_redirect(reason: str) -> RedirectResponse:
            resp = RedirectResponse(url=f"{base}/?login_error={reason}", status_code=302)
            resp.delete_cookie("pt_oauth_state")
            resp.delete_cookie("pt_oauth_verifier")
            return resp

        auth_client = request.app.state.auth_client
        supabase_client = request.app.state.supabase_client
        if auth_client is None or supabase_client is None:
            return error_redirect("auth_not_configured")
        if error or not code or not state:
            return error_redirect("access_denied" if error else "invalid_request")

        response = RedirectResponse(url=f"{base}/", status_code=302)
        try:
            await complete_github_login(
                auth_client=auth_client,
                supabase_client=supabase_client,
                request=request,
                response=response,
                code=code,
                state=state,
            )
        except AccountAuthError:
            return error_redirect("invalid_state")
        except SupabaseError:
            return error_redirect("signin_failed")
        return response

    @app.get("/v1/auth/me", response_model=MeOut, include_in_schema=False)
    async def auth_me(request: Request) -> JSONResponse:
        auth_client = request.app.state.auth_client
        supabase_client = request.app.state.supabase_client
        if auth_client is None or supabase_client is None:
            return _unauthenticated_account_response(request)
        try:
            account, refreshed = await get_current_customer(
                auth_client=auth_client, supabase_client=supabase_client, request=request
            )
        except (AccountAuthError, SupabaseError):
            return _unauthenticated_account_response(request)
        response = JSONResponse(
            content=MeOut(
                customer_id=account.customer_id, email=account.email, name=account.name
            ).model_dump()
        )
        if refreshed is not None:
            set_session_cookies(response, refreshed)
        return response

    @app.post("/v1/auth/logout", include_in_schema=False)
    async def logout(request: Request) -> JSONResponse:
        auth_client = request.app.state.auth_client
        supabase_client = request.app.state.supabase_client
        access_token = request.cookies.get("pt_session")
        if auth_client is not None and access_token:
            if supabase_client is not None:
                with suppress(AccountAuthError, SupabaseError):
                    account, _ = await get_current_customer(
                        auth_client=auth_client, supabase_client=supabase_client, request=request
                    )
                    await supabase_client.record_audit_event(
                        customer_id=account.customer_id,
                        api_key_id=None,
                        action="account.logout",
                        metadata={},
                    )
            await auth_client.logout(access_token=access_token)
        response = JSONResponse(content={"signed_out": True})
        clear_session_cookies(response)
        return response

    @app.get("/v1/account/keys", response_model=AccountKeysOut, include_in_schema=False)
    async def list_account_keys(request: Request) -> JSONResponse:
        auth_client = request.app.state.auth_client
        supabase_client = request.app.state.supabase_client
        if auth_client is None or supabase_client is None:
            return _unauthenticated_account_response(request)
        try:
            account, refreshed = await get_current_customer(
                auth_client=auth_client, supabase_client=supabase_client, request=request
            )
        except (AccountAuthError, SupabaseError):
            return _unauthenticated_account_response(request)
        rows = await list_keys(supabase_client, customer_id=account.customer_id)
        response = JSONResponse(
            content=AccountKeysOut(keys=[build_api_key_summary(row) for row in rows]).model_dump(
                mode="json"
            )
        )
        if refreshed is not None:
            set_session_cookies(response, refreshed)
        return response

    @app.post(
        "/v1/account/keys",
        response_model=ApiKeyCreatedOut,
        status_code=201,
        include_in_schema=False,
    )
    async def create_account_key(request: Request, payload: CreateKeyRequest) -> JSONResponse:
        auth_client = request.app.state.auth_client
        supabase_client = request.app.state.supabase_client
        if auth_client is None or supabase_client is None:
            return _unauthenticated_account_response(request)
        try:
            account, refreshed = await get_current_customer(
                auth_client=auth_client, supabase_client=supabase_client, request=request
            )
        except (AccountAuthError, SupabaseError):
            return _unauthenticated_account_response(request)
        try:
            record, full_key = await create_key(
                supabase_client, customer_id=account.customer_id, label=payload.label
            )
        except AccountLimitError as exc:
            return _error(request, 422, str(exc), "account_key_limit", str(exc))
        summary = build_api_key_summary(record)
        response = JSONResponse(
            status_code=201,
            content=ApiKeyCreatedOut(api_key=full_key, **summary.model_dump()).model_dump(
                mode="json"
            ),
        )
        if refreshed is not None:
            set_session_cookies(response, refreshed)
        return response

    @app.post("/v1/account/keys/{key_id}/revoke", include_in_schema=False)
    async def revoke_account_key(request: Request, key_id: str) -> JSONResponse:
        auth_client = request.app.state.auth_client
        supabase_client = request.app.state.supabase_client
        if auth_client is None or supabase_client is None:
            return _unauthenticated_account_response(request)
        try:
            account, refreshed = await get_current_customer(
                auth_client=auth_client, supabase_client=supabase_client, request=request
            )
        except (AccountAuthError, SupabaseError):
            return _unauthenticated_account_response(request)
        try:
            await revoke_key(supabase_client, customer_id=account.customer_id, key_id=key_id)
        except AccountKeyNotFoundError:
            detail = "API key not found"
            return _error(request, 404, detail, "account_key_not_found", detail)
        response = JSONResponse(content={"revoked": True})
        if refreshed is not None:
            set_session_cookies(response, refreshed)
        return response

    @app.get(
        "/v1/valuations/{ticker}",
        response_model=ValuationResponse,
        responses={
            400: {"model": ErrorResponse, "description": "Malformed request"},
            401: {"model": ErrorResponse, "description": "Authentication required (reserved)"},
            403: {"model": ErrorResponse, "description": "Insufficient scope (reserved)"},
            404: {"model": ErrorResponse, "description": "Ticker unavailable"},
            422: {"model": ErrorResponse, "description": "Invalid request or assumptions"},
            429: {"model": ErrorResponse, "description": "Daily rate limit exceeded"},
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
