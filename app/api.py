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

import asyncio
import hmac
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from inspect import isawaitable
from pathlib import Path as FilePath
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Path, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Match

from . import MODEL_VERSION
from .accounts import (
    CSRF_COOKIE,
    CSRF_HEADER,
    LOGIN_ATTEMPTS_DAILY_LIMIT,
    AccountAuthError,
    AccountKeyNotFoundError,
    AccountLimitError,
    CustomerAccount,
    InvalidEmailError,
    build_github_login,
    clear_csrf_cookie,
    clear_session_cookies,
    complete_login,
    create_key,
    csrf_tokens_match,
    get_current_customer,
    list_keys,
    rename_key,
    request_email_login,
    revoke_key,
    rotate_key,
    set_csrf_cookie,
    set_oauth_verifier_cookie,
    set_session_cookies,
)
from .auth import VALUATION_SCOPE, APIKeyAuthenticator, AuthFailure, AuthFailureReason
from .client_ip import FORWARDED_FOR_HEADER, ClientIdentity, resolve_client_identity
from .dcf_engine import DCFValidationError, compute_dcf, compute_sensitivity_grid
from .exceptions import (
    NormalizationError,
    ProviderAuthError,
    ProviderError,
    SnapshotStoreError,
    TickerNotCoveredError,
    TickerNotFoundError,
    UnsupportedSectorError,
)
from .fundamentals import FundamentalsService
from .models import Assumptions, BaseFinancials
from .normalization import NormalizedQuote, normalize_finnhub_quote
from .observability import (
    INSTANCE_ID,
    MetricsRegistry,
    configure_logging,
    log_request,
    record,
    snapshot,
    stage,
    telemetry_scope,
)
from .providers.finnhub import FinnhubClient
from .providers.fmp import FMPClient
from .rate_limit import DailyRequestLimiter, RateLimitResult, RedisLoginRateLimiter
from .raw_store import FileRawSink
from .readiness import ReadinessChecker
from .redis_cache import RedisBackend, UpstashRedisClient
from .refresh import DailyRefreshRunner
from .request_context import set_request_id
from .schemas import (
    AccountKeysOut,
    ApiKeyCreatedOut,
    ApiKeySummaryOut,
    CreateKeyRequest,
    EmailLoginRequest,
    ErrorResponse,
    MeOut,
    RenameKeyRequest,
    ValuationResponse,
    build_api_key_summary,
    build_valuation_response,
)
from .settings import Settings
from .supabase import (
    AuthSession,
    SupabaseAPIKeyAuthenticator,
    SupabaseAuthClient,
    SupabaseClient,
    SupabaseDailyQuotaLimiter,
    SupabaseError,
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


def _default_raw_sink(settings: Settings) -> FileRawSink | None:
    """Persist provider payloads locally except on Vercel.

    Vercel Functions are stateless and deployment files must not be treated as
    durable storage. Production audit payloads still need an off-box backend
    (Phase 10, open); disabling this sink keeps requests independent of
    ephemeral files. Writes are compressed, atomic, credential-redacted, and
    retention-bounded — see app/raw_store.py.
    """
    if settings.on_vercel:
        return None
    return FileRawSink(
        FilePath(__file__).parent.parent / "data" / "raw",
        retention_days=settings.raw_capture_retention_days,
        max_captures_per_endpoint=settings.raw_capture_max_per_endpoint,
    )


def _route_template(request: Request) -> str:
    """The matched route pattern, never the raw path.

    `/v1/valuations/AAPL` is logged as `/v1/valuations/{ticker}`: it keeps log
    cardinality bounded, and the ticker travels as its own field instead of
    being smeared across route names.
    """
    route = request.scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(template, str):
        return template
    # Requests refused in the pre-flight gate (401/403/429/503) never reach the
    # router, so the scope has no route. Match against the table by hand rather
    # than labelling them all "unmatched": a quota rejection nobody can
    # attribute to a route is useless in a metric. Only runs on that path.
    for candidate in request.app.routes:
        matcher = getattr(candidate, "matches", None)
        if matcher is None:
            continue
        match, _ = matcher(request.scope)
        if match is Match.FULL:
            pattern = getattr(candidate, "path_format", None) or getattr(candidate, "path", None)
            if isinstance(pattern, str):
                return pattern
    return "unmatched"


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

_DOCS_DIR = FilePath(__file__).parent.parent / "docs"
_PICS_DIR = (_DOCS_DIR / "Pics").resolve()

# Valuation responses are never HTTP-cacheable (ADR-008): every response
# carries a live, per-request market price, so no shared/browser cache may
# retain one. Errors and auth/rate-limit responses use the same directive.
NO_STORE = "no-store"

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

# FastAPI's generated docs load Swagger UI / ReDoc from a public CDN, which puts
# third-party JavaScript on the same origin as the signed-in account UI — the
# origin that holds the `pt_session` cookie and the deliberately JS-readable
# `pt_csrf` cookie. Left bare (FastAPI ships them with no CSP at all) a
# compromised CDN asset could read the CSRF token, call `/v1/account/keys` with
# the browser's session cookie, and post the new key anywhere.
#
# `default-src 'none'` + an explicit allowlist is what closes most of that, and
# `connect-src 'self'` is the load-bearing line: even a hostile script that does
# run cannot ship what it stole off this origin. `script-src` stays pinned to the
# one CDN; the font/style hosts ReDoc needs cannot execute anything.
#
# Residual risk, stated rather than hidden: a hostile script inside the origin
# can still act as the user. Removing the CDN entirely (vendored assets, or
# turning these pages off) is an owner decision — see `TODO.md` §8.
_DOCS_CDN = "https://cdn.jsdelivr.net"
_FASTAPI_ASSETS = "https://fastapi.tiangolo.com"

_SWAGGER_PAGE_CSP = "; ".join(
    [
        "default-src 'none'",
        "base-uri 'none'",
        "connect-src 'self'",
        "form-action 'none'",
        "frame-ancestors 'none'",
        f"img-src 'self' data: {_DOCS_CDN} {_FASTAPI_ASSETS}",
        "object-src 'none'",
        f"script-src {_DOCS_CDN} 'unsafe-inline'",
        f"style-src {_DOCS_CDN} 'unsafe-inline'",
    ]
)

# ReDoc additionally pulls Google Fonts and builds its search index in a
# blob: worker. Stylesheets and font files cannot execute script, so allowing
# those two hosts does not widen the script surface.
_REDOC_PAGE_CSP = "; ".join(
    [
        "default-src 'none'",
        "base-uri 'none'",
        "child-src blob:",
        "connect-src 'self'",
        "font-src https://fonts.gstatic.com data:",
        "form-action 'none'",
        "frame-ancestors 'none'",
        f"img-src 'self' data: {_DOCS_CDN} {_FASTAPI_ASSETS}",
        "object-src 'none'",
        f"script-src {_DOCS_CDN} 'unsafe-inline'",
        f"style-src {_DOCS_CDN} https://fonts.googleapis.com 'unsafe-inline'",
        "worker-src blob:",
    ]
)

# Every HTML/schema route states its own cacheability. Left unset, Vercel
# supplies `public, max-age=0, must-revalidate` — and it was applying `public`
# to `/dcf`, the response that carries a per-visitor `Set-Cookie: pt_csrf=…`.
# `must-revalidate` kept that from being exploitable in practice, but which
# responses are shareable is the application's decision to make, not a
# platform default's.
REVALIDATE = "public, max-age=0, must-revalidate"
PRIVATE_NO_STORE = "private, no-store"


def _rate_limit_headers(result: RateLimitResult) -> dict[str, str]:
    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(result.reset_epoch),
    }
    if not result.allowed:
        headers["Retry-After"] = str(result.retry_after)
    return headers


def _over_quota_response(request: Request, result: RateLimitResult) -> JSONResponse:
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
        headers=_rate_limit_headers(result),
    )
    response.headers["Cache-Control"] = NO_STORE
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers.update(_SECURITY_HEADERS)
    return response


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
    response.headers["Cache-Control"] = NO_STORE
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
    response.headers["Cache-Control"] = NO_STORE
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers.update(_SECURITY_HEADERS)
    return response


def _unauthenticated_account_response(request: Request) -> JSONResponse:
    response = _auth_error_response(
        request, status_code=401, code="not_signed_in", message="Not signed in."
    )
    clear_session_cookies(response)
    clear_csrf_cookie(response)
    return response


async def _resolve(value: Any) -> Any:
    if isawaitable(value):
        return await value
    return value


# The same shape the route's path parameter enforces. The middleware reads the
# ticker off the *raw* path, before FastAPI has validated anything, and puts it
# in the metering row -- so without this a request to `/v1/valuations/<junk>`
# that then 422s still wrote that junk into `usage_events.ticker`, letting a
# caller choose the contents of a row in our database (bounded only by the
# platform's URL limit). Anything that could not be a real ticker is metered as
# "no ticker" rather than stored verbatim. The request is still metered: it
# consumed a quota slot, so it belongs in the ledger either way.
_TICKER_PATTERN = re.compile(r"\A[A-Z][A-Z.\-]{0,9}\Z")


def _valuation_ticker_from_path(path: str) -> str | None:
    prefix = "/v1/valuations/"
    if not path.startswith(prefix):
        return None
    ticker = path[len(prefix) :].split("/", 1)[0].upper()
    return ticker if _TICKER_PATTERN.match(ticker) else None


def create_app(
    fmp_client: FMPClient | None = None,
    finnhub_client: FinnhubClient | None = None,
    ttl_seconds: float | None = None,
    profile_ttl_seconds: float | None = None,
    daily_rate_limit: int | None = None,
    rate_limiter: Any | None = None,
    authenticator: Any | None = None,
    supabase_client: SupabaseClient | None = None,
    auth_client: SupabaseAuthClient | None = None,
    redis_backend: RedisBackend | None = None,
    snapshot_store: Any | None = None,
    refresh_runner: Any | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    # One typed, validated read of the environment for the whole app (Phase 11).
    # An explicit argument always wins over the environment, so tests and the
    # load probe can pin a value without reading the environment.
    resolved = settings or Settings.from_env()
    if ttl_seconds is not None:
        resolved = resolved.with_overrides(fundamentals_ttl_seconds=ttl_seconds)
    if profile_ttl_seconds is not None:
        resolved = resolved.with_overrides(profile_ttl_seconds=profile_ttl_seconds)
    if daily_rate_limit is not None:
        resolved = resolved.with_overrides(daily_rate_limit=daily_rate_limit)

    # Structured logging is configured from settings before anything can log.
    configure_logging(level=resolved.log_level, log_format=resolved.log_format)

    supabase_config = resolved.supabase
    configured_supabase_client = supabase_client or (
        SupabaseClient(supabase_config) if supabase_config is not None else None
    )
    configured_auth_client = auth_client or (
        SupabaseAuthClient(supabase_config) if supabase_config is not None else None
    )
    redis_config = resolved.redis
    # Server-only shared secrets protecting the internal endpoints.
    cron_secret = resolved.cron_secret
    metrics_token = resolved.metrics_token

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_client = fmp_client is None
        client = fmp_client or FMPClient(
            raw_sink=_default_raw_sink(resolved),
            timeout=resolved.provider_timeout_seconds,
            max_retries=resolved.provider_max_retries,
            provider_concurrency=resolved.provider_concurrency,
        )
        # Live market price (ADR-008): auto-enables on FINNHUB_API_KEY, same
        # pattern as Supabase/Redis. Absent -> price feature off; valuations
        # return null current_price/upside_pct with a warning.
        finnhub_config = resolved.finnhub
        owns_finnhub = finnhub_client is None and finnhub_config is not None
        configured_finnhub = finnhub_client or (
            FinnhubClient(api_key=finnhub_config.api_key) if finnhub_config is not None else None
        )
        app.state.finnhub = configured_finnhub
        owns_redis = redis_backend is None and redis_config is not None
        configured_redis = redis_backend or (
            UpstashRedisClient(redis_config) if redis_config is not None else None
        )
        app.state.redis = configured_redis
        # Durable statement store (Phase 8 Slice C): rides the same Supabase
        # project/client as auth. Requires migration 003 to be applied before
        # a deploy with Supabase configured — a missing table is a storage
        # error (503 for cold tickers), not a miss.
        app.state.fundamentals = FundamentalsService(
            client,
            ttl_seconds=resolved.fundamentals_ttl_seconds,
            profile_ttl_seconds=resolved.profile_ttl_seconds,
            redis=configured_redis,
            snapshots=snapshot_store or configured_supabase_client,
        )
        # Daily 6 PM Eastern refresh (ADR-007): needs both the statement
        # store and the Supabase run/claim ledger, so it activates exactly
        # when Supabase does.
        app.state.refresh_runner = refresh_runner or (
            DailyRefreshRunner(app.state.fundamentals, configured_supabase_client)
            if configured_supabase_client is not None
            else None
        )
        # Readiness (Phase 11 Slice 3) reports dependency reachability; it
        # never probes FMP or Finnhub, whose budgets are metered per call.
        app.state.readiness = ReadinessChecker(
            supabase=configured_supabase_client,
            redis=configured_redis,
            price_configured=configured_finnhub is not None,
            cache_seconds=resolved.readiness_cache_seconds,
        )
        if configured_redis is not None:
            # Cross-instance login limiting (Phase 8 Slice B); falls back to
            # the in-process limiter set below whenever Redis is down.
            app.state.login_rate_limiter = RedisLoginRateLimiter(
                configured_redis,
                limit=LOGIN_ATTEMPTS_DAILY_LIMIT,
            )
        try:
            yield
        finally:
            # Drain before closing anything the drained work depends on: an
            # in-flight statement load still needs the FMP client and Supabase.
            await app.state.fundamentals.aclose()
            if owns_client:
                await client.aclose()
            if owns_finnhub and configured_finnhub is not None:
                await configured_finnhub.aclose()
            if configured_supabase_client is not None and supabase_client is None:
                await configured_supabase_client.aclose()
            if configured_auth_client is not None and auth_client is None:
                await configured_auth_client.aclose()
            if owns_redis and configured_redis is not None:
                await configured_redis.aclose()

    app = FastAPI(
        title="DCF Valuation API",
        version=MODEL_VERSION,
        description=(
            "Discounted cash flow valuations from caller-supplied assumptions. "
            "Outputs are model estimates, not investment recommendations."
        ),
        lifespan=lifespan,
        # The built-in docs routes are replaced below by equivalents that carry
        # a Content-Security-Policy. FastAPI's own emit no headers, and these
        # pages run CDN-hosted JavaScript on the origin that holds the customer
        # session — see _SWAGGER_PAGE_CSP.
        docs_url=None,
        redoc_url=None,
    )
    if authenticator is None and configured_supabase_client is not None:
        authenticator = SupabaseAPIKeyAuthenticator(configured_supabase_client, required=True)
    if rate_limiter is None and configured_supabase_client is not None:
        rate_limiter = SupabaseDailyQuotaLimiter(
            configured_supabase_client, default_limit=resolved.daily_rate_limit
        )

    app.state.settings = resolved
    app.state.metrics = MetricsRegistry()
    # Replaced in the lifespan once the Redis backend exists; this default keeps
    # /ready answerable if it is somehow reached before startup completes.
    app.state.readiness = ReadinessChecker(cache_seconds=resolved.readiness_cache_seconds)
    app.state.rate_limiter = rate_limiter or DailyRequestLimiter(resolved.daily_rate_limit)
    app.state.authenticator = authenticator or APIKeyAuthenticator(required=False)
    app.state.supabase_client = configured_supabase_client
    app.state.auth_client = configured_auth_client
    app.state.login_rate_limiter = DailyRequestLimiter(LOGIN_ATTEMPTS_DAILY_LIMIT)

    @app.middleware("http")
    async def _request_id(request: Request, call_next: Any) -> Response:
        request.state.request_id = str(uuid4())
        record(request_id=request.state.request_id)
        # Also bind it ambiently: the provider client and audit sink sit below
        # the route behind a shared long-lived client, so they read the id from
        # the request context instead of a threaded-through parameter. Each
        # ASGI request has its own context, so no reset is needed here.
        set_request_id(request.state.request_id)
        principal = None
        identity = "anonymous"
        limit = resolved.daily_rate_limit
        valuation_ticker = _valuation_ticker_from_path(request.url.path)
        is_valuation = request.method == "GET" and request.url.path.startswith("/v1/valuations/")

        # --- authenticate + atomically consume quota (pre-flight) ---
        # One atomic check-and-increment before any fetch/compute. With the
        # response cache and conditional 304s retired (ADR-008), nothing is
        # "free" anymore, so the old peek-then-consume split (and its
        # documented race) has no reason to exist. Every valuation request —
        # success OR error (404/422/502) — consumes quota, preserving the
        # deliberate "invalid requests count against the limit" behavior.
        consumed: RateLimitResult | None = None
        if is_valuation:
            try:
                with stage("auth"):
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
                else resolved.daily_rate_limit
            )
            limiter = request.app.state.rate_limiter
            # P3: when the limiter can meter (the Supabase one, migration 005),
            # the quota slot and the usage row are written by a single RPC in a
            # single transaction. The in-process fallback limiter has no ledger
            # to write to, so it keeps the plain consume.
            consume_and_record = getattr(limiter, "consume_and_record", None)
            try:
                with stage("quota"):
                    if consume_and_record is not None:
                        consumed = await _resolve(
                            consume_and_record(
                                identity=identity,
                                limit=limit,
                                principal=principal,
                                request_id=request.state.request_id,
                                method=request.method,
                                path=request.url.path,
                                ticker=valuation_ticker,
                            )
                        )
                    else:
                        consumed = await _resolve(
                            limiter.check_and_increment(identity=identity, limit=limit)
                        )
            except SupabaseError:
                # Fail closed: never serve a valuation we couldn't meter. Since
                # P3 that is literal -- the metering row is part of the same
                # call, so there is no longer a way to bill a request and lose
                # its ledger entry.
                return _storage_error_response(request)
            record(quota="exceeded" if not consumed.allowed else "allowed")
            if not consumed.allowed:
                # The rejection is already in the ledger with its 429: the RPC
                # derived it from the same count that produced this response.
                return _over_quota_response(request, consumed)

        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers.update(_SECURITY_HEADERS)
        # Default to not-shareable and let the routes that are safe to share say
        # so (the public pages, the images). A response nobody classified is far
        # more likely to be per-customer — an account's key list, an error naming
        # a request id — than a public asset, so the silent case must be the safe
        # one. Without this the platform picked, and it picked `public`.
        response.headers.setdefault("Cache-Control", PRIVATE_NO_STORE)

        if is_valuation and consumed is not None:
            # Quota headers are safe again now that valuation responses are
            # no-store (they were removed in Phase 7 only because a shared
            # cache could have served one caller's quota state to another).
            response.headers.update(_rate_limit_headers(consumed))
            # Error responses on the valuation path must never be cached.
            if response.status_code != 200:
                response.headers["Cache-Control"] = NO_STORE
                # The ledger row was written pre-flight with no status, which
                # reads as "admitted"; only a response that is not a 200 is
                # worth a second round trip to correct. Best effort: the
                # response is already built and the request is already billed,
                # so a failed correction must not change what the caller gets.
                finalize_usage = getattr(request.app.state.rate_limiter, "finalize_usage", None)
                if finalize_usage is not None:
                    with suppress(SupabaseError):
                        await _resolve(
                            finalize_usage(
                                request_id=request.state.request_id,
                                status_code=response.status_code,
                            )
                        )
        return response

    @app.middleware("http")
    async def _access_log(request: Request, call_next: Any) -> Response:
        """Outermost middleware: exactly one structured line per request.

        Registered last, so it wraps `_request_id` and therefore also sees the
        responses that short-circuit there (401/403/429/503) — those are the
        ones an operator most needs to see. The telemetry scope opened here is
        what every layer below annotates: cache outcome, provider and Supabase
        round trips, ticker, quota decision.
        """
        started = time.perf_counter()
        with telemetry_scope():
            try:
                response = await call_next(request)
            except Exception:
                _log_finished(request, 500, started)
                raise
            _log_finished(request, response.status_code, started)
            return response

    def _log_finished(request: Request, status: int, started: float) -> None:
        duration_seconds = time.perf_counter() - started
        request.app.state.metrics.observe_request(
            route=_route_template(request),
            status=status,
            duration_seconds=duration_seconds,
            fields=snapshot(),
        )
        log_request(
            method=request.method,
            route=_route_template(request),
            status=status,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            # The inner middleware runs in its own task context, so its
            # `set_request_id` is not visible here; `request.state` is backed by
            # the shared ASGI scope and is.
            extra={"request_id": getattr(request.state, "request_id", None)},
        )

    # --- error mapping (see app/exceptions.py for the rationale) ---

    def _error(
        request: Request,
        status: int,
        detail: Any,
        code: str,
        message: str,
        fields: list[dict[str, str]] | None = None,
    ) -> JSONResponse:
        # Stable code, not free text: metrics count by it and log lines carry
        # it, so "what is failing" is answerable without reading bodies.
        record(error_code=code)
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

    @app.exception_handler(SnapshotStoreError)
    async def _snapshot_store_error(request: Request, exc: SnapshotStoreError) -> JSONResponse:
        # A store error is not a miss (ADR-006): the request fails closed
        # rather than falling through to FMP and breaking the once-daily
        # provider guarantee.
        detail = "statement storage is unavailable, try again shortly"
        return _error(request, 503, detail, "snapshot_store_unavailable", detail)

    # --- routes ---

    async def _account_context(
        request: Request,
    ) -> tuple[CustomerAccount, SupabaseClient, AuthSession | None] | JSONResponse:
        auth_client = request.app.state.auth_client
        supabase_client = request.app.state.supabase_client
        if auth_client is None or supabase_client is None:
            return _unauthenticated_account_response(request)
        try:
            account, refreshed = await get_current_customer(
                auth_client=auth_client,
                supabase_client=supabase_client,
                request=request,
            )
        except (AccountAuthError, SupabaseError):
            return _unauthenticated_account_response(request)
        return account, supabase_client, refreshed

    def _with_refreshed_session(
        request: Request, response: JSONResponse, refreshed: AuthSession | None
    ) -> JSONResponse:
        if refreshed is not None:
            set_session_cookies(response, refreshed)
        if not request.cookies.get(CSRF_COOKIE):
            set_csrf_cookie(response)
        return response

    def _csrf_error_response(request: Request) -> JSONResponse:
        return _auth_error_response(
            request,
            status_code=403,
            code="csrf_failed",
            message="CSRF token is missing or invalid.",
        )

    def _require_csrf(request: Request) -> JSONResponse | None:
        if csrf_tokens_match(
            cookie_token=request.cookies.get(CSRF_COOKIE),
            header_token=request.headers.get(CSRF_HEADER),
        ):
            return None
        return _csrf_error_response(request)

    def _account_key_not_found(request: Request) -> JSONResponse:
        detail = "API key not found"
        return _error(request, 404, detail, "account_key_not_found", detail)

    def _auth_not_configured_response(request: Request) -> JSONResponse:
        return _auth_error_response(
            request,
            status_code=503,
            code="auth_not_configured",
            message="Sign-in is not configured.",
        )

    def _client_identity(request: Request) -> ClientIdentity:
        # Behind a proxy the socket peer is the platform, not the caller, so the
        # per-IP cap would be one shared bucket; trusting the header blindly
        # would let anyone mint a fresh bucket per request. app/client_ip.py
        # resolves it under an explicit hop count (Phase 11 Slice 4).
        return resolve_client_identity(
            peer=request.client.host if request.client else None,
            forwarded_for=request.headers.get(FORWARDED_FOR_HEADER),
            trusted_proxy_hops=resolved.trusted_proxy_hops,
        )

    async def _login_limit_response(request: Request) -> JSONResponse | None:
        identity = _client_identity(request)
        # The source, never the address: the source proves the configuration is
        # right, while the address is personal data that logs should not keep.
        record(client_ip_source=identity.source)
        result = await _resolve(
            request.app.state.login_rate_limiter.check_and_increment(
                identity=identity.address,
                limit=LOGIN_ATTEMPTS_DAILY_LIMIT,
            )
        )
        if result.allowed:
            return None
        return _auth_error_response(
            request,
            status_code=429,
            code="login_rate_limited",
            message="Too many sign-in attempts. Try again later.",
        )

    # --- public site (Phase 9): portfolio at /, API directory at /apis, and the
    # DCF product at /dcf. The API surface below is unaffected by the split.

    @app.get("/", include_in_schema=False)
    async def portfolio_page() -> FileResponse:
        return FileResponse(
            _DOCS_DIR / "portfolio.html",
            headers={"Content-Security-Policy": _LANDING_PAGE_CSP, "Cache-Control": REVALIDATE},
        )

    @app.get("/apis", include_in_schema=False)
    async def api_directory_page() -> FileResponse:
        return FileResponse(
            _DOCS_DIR / "apis.html",
            headers={"Content-Security-Policy": _LANDING_PAGE_CSP, "Cache-Control": REVALIDATE},
        )

    @app.get("/dcf", include_in_schema=False)
    async def dcf_page(request: Request) -> FileResponse:
        # The account UI lives on this page, so this is where the CSRF token is
        # minted (it moved from `/` when the portfolio took that route over).
        # It mints a per-visitor token, so the response is never shareable —
        # stated here rather than inherited from the platform's `public` default.
        response = FileResponse(
            _DOCS_DIR / "index.html",
            headers={
                "Content-Security-Policy": _LANDING_PAGE_CSP,
                "Cache-Control": PRIVATE_NO_STORE,
            },
        )
        if not request.cookies.get(CSRF_COOKIE):
            set_csrf_cookie(response)
        return response

    # --- API reference (Swagger UI / ReDoc) ---
    # Hand-rolled rather than FastAPI's built-ins purely so the pages can carry
    # a CSP and a cache directive; the rendered HTML is FastAPI's own.

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        page = get_swagger_ui_html(
            openapi_url=str(app.openapi_url),
            title=f"{app.title} — API reference",
        )
        page.headers["Content-Security-Policy"] = _SWAGGER_PAGE_CSP
        page.headers["Cache-Control"] = REVALIDATE
        return page

    @app.get("/redoc", include_in_schema=False)
    async def redoc_ui() -> HTMLResponse:
        page = get_redoc_html(
            openapi_url=str(app.openapi_url),
            title=f"{app.title} — API reference",
        )
        page.headers["Content-Security-Policy"] = _REDOC_PAGE_CSP
        page.headers["Cache-Control"] = REVALIDATE
        return page

    @app.get("/Pics/{filename}", include_in_schema=False)
    async def portfolio_image(filename: str) -> Response:
        # Served through the function rather than Vercel's static hosting, so
        # the immutable cache header is what keeps repeat loads off the origin.
        target = (_PICS_DIR / filename).resolve()
        if _PICS_DIR not in target.parents or not target.is_file():
            return Response(status_code=404)
        return FileResponse(
            target,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # --- customer login (GitHub via Supabase Auth) and self-service keys ---
    # Human browser sessions here are a distinct credential class from the
    # `X-API-Key` machine auth above: a session cookie never grants valuation
    # access, and an API key never grants access to these routes.

    @app.get("/v1/auth/github/login", include_in_schema=False)
    async def github_login(request: Request) -> Response:
        auth_client = request.app.state.auth_client
        if auth_client is None:
            return _auth_not_configured_response(request)
        limited = await _login_limit_response(request)
        if limited is not None:
            return limited
        url, verifier = build_github_login(auth_client)
        response = RedirectResponse(url=url, status_code=302)
        set_oauth_verifier_cookie(response, verifier=verifier)
        return response

    @app.post("/v1/auth/email/login", include_in_schema=False)
    async def email_login(request: Request, payload: EmailLoginRequest) -> Response:
        auth_client = request.app.state.auth_client
        if auth_client is None:
            return _auth_not_configured_response(request)
        csrf_error = _require_csrf(request)
        if csrf_error is not None:
            return csrf_error
        limited = await _login_limit_response(request)
        if limited is not None:
            return limited
        try:
            verifier = await request_email_login(auth_client, email=payload.email)
        except InvalidEmailError as exc:
            return _error(request, 422, str(exc), "invalid_email", str(exc))
        except SupabaseError:
            detail = "Failed to send the sign-in email. Try again shortly."
            return _error(request, 503, detail, "email_login_failed", detail)
        response = JSONResponse(content={"sent": True})
        set_oauth_verifier_cookie(response, verifier=verifier)
        return response

    @app.get("/v1/auth/callback", include_in_schema=False)
    async def auth_callback(
        request: Request,
        code: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        """Completes either login method -- GitHub's authorize redirect and
        Supabase's magic-link verify both land here with `?code=...`."""
        base = resolved.public_base_url

        def error_redirect(reason: str) -> RedirectResponse:
            # Phase 9: land on /dcf, not /. The portfolio owns `/` now and has
            # no account UI or `login_error` handler to render this. The
            # `#account` fragment scrolls the browser straight to the sign-in
            # section instead of the top of the page.
            resp = RedirectResponse(url=f"{base}/dcf?login_error={reason}#account", status_code=302)
            resp.delete_cookie("pt_oauth_verifier")
            return resp

        auth_client = request.app.state.auth_client
        supabase_client = request.app.state.supabase_client
        if auth_client is None or supabase_client is None:
            return error_redirect("auth_not_configured")
        if error or not code:
            return error_redirect("access_denied" if error else "invalid_request")

        # `#account` fragment lands the browser on the sign-in section rather
        # than the top of the page after a successful login round trip.
        response = RedirectResponse(url=f"{base}/dcf#account", status_code=302)
        try:
            await complete_login(
                auth_client=auth_client,
                supabase_client=supabase_client,
                request=request,
                response=response,
                code=code,
            )
        except AccountAuthError:
            return error_redirect("expired_attempt")
        except SupabaseError:
            return error_redirect("signin_failed")
        return response

    @app.get("/v1/auth/me", response_model=MeOut, include_in_schema=False)
    async def auth_me(request: Request) -> JSONResponse:
        context = await _account_context(request)
        if isinstance(context, JSONResponse):
            return context
        account, _, refreshed = context
        response = JSONResponse(
            content=MeOut(
                customer_id=account.customer_id, email=account.email, name=account.name
            ).model_dump()
        )
        return _with_refreshed_session(request, response, refreshed)

    @app.post("/v1/auth/logout", include_in_schema=False)
    async def logout(request: Request) -> JSONResponse:
        csrf_error = _require_csrf(request)
        if csrf_error is not None:
            return csrf_error
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
        clear_csrf_cookie(response)
        return response

    @app.get("/v1/account/keys", response_model=AccountKeysOut, include_in_schema=False)
    async def list_account_keys(request: Request) -> JSONResponse:
        context = await _account_context(request)
        if isinstance(context, JSONResponse):
            return context
        account, supabase_client, refreshed = context
        rows = await list_keys(supabase_client, customer_id=account.customer_id)
        response = JSONResponse(
            content=AccountKeysOut(keys=[build_api_key_summary(row) for row in rows]).model_dump(
                mode="json"
            )
        )
        return _with_refreshed_session(request, response, refreshed)

    @app.post(
        "/v1/account/keys",
        response_model=ApiKeyCreatedOut,
        status_code=201,
        include_in_schema=False,
    )
    async def create_account_key(request: Request, payload: CreateKeyRequest) -> JSONResponse:
        context = await _account_context(request)
        if isinstance(context, JSONResponse):
            return context
        csrf_error = _require_csrf(request)
        if csrf_error is not None:
            return csrf_error
        account, supabase_client, refreshed = context
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
        return _with_refreshed_session(request, response, refreshed)

    @app.post("/v1/account/keys/{key_id}/revoke", include_in_schema=False)
    async def revoke_account_key(request: Request, key_id: str) -> JSONResponse:
        context = await _account_context(request)
        if isinstance(context, JSONResponse):
            return context
        csrf_error = _require_csrf(request)
        if csrf_error is not None:
            return csrf_error
        account, supabase_client, refreshed = context
        try:
            await revoke_key(supabase_client, customer_id=account.customer_id, key_id=key_id)
        except AccountKeyNotFoundError:
            return _account_key_not_found(request)
        response = JSONResponse(content={"revoked": True})
        return _with_refreshed_session(request, response, refreshed)

    @app.post(
        "/v1/account/keys/{key_id}/rotate",
        response_model=ApiKeyCreatedOut,
        include_in_schema=False,
    )
    async def rotate_account_key(request: Request, key_id: str) -> JSONResponse:
        context = await _account_context(request)
        if isinstance(context, JSONResponse):
            return context
        csrf_error = _require_csrf(request)
        if csrf_error is not None:
            return csrf_error
        account, supabase_client, refreshed = context
        try:
            record, full_key = await rotate_key(
                supabase_client, customer_id=account.customer_id, key_id=key_id
            )
        except AccountKeyNotFoundError:
            return _account_key_not_found(request)
        summary = build_api_key_summary(record)
        response = JSONResponse(
            content=ApiKeyCreatedOut(api_key=full_key, **summary.model_dump()).model_dump(
                mode="json"
            )
        )
        return _with_refreshed_session(request, response, refreshed)

    @app.post(
        "/v1/account/keys/{key_id}/rename",
        response_model=ApiKeySummaryOut,
        include_in_schema=False,
    )
    async def rename_account_key(
        request: Request, key_id: str, payload: RenameKeyRequest
    ) -> JSONResponse:
        context = await _account_context(request)
        if isinstance(context, JSONResponse):
            return context
        csrf_error = _require_csrf(request)
        if csrf_error is not None:
            return csrf_error
        account, supabase_client, refreshed = context
        try:
            record = await rename_key(
                supabase_client,
                customer_id=account.customer_id,
                key_id=key_id,
                label=payload.label,
            )
        except AccountKeyNotFoundError:
            return _account_key_not_found(request)
        response = JSONResponse(content=build_api_key_summary(record).model_dump(mode="json"))
        return _with_refreshed_session(request, response, refreshed)

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
        response: Response,
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
    ) -> ValuationResponse | Response:
        growth_values = _parse_revenue_growth(revenue_growth)
        assumptions = Assumptions(
            wacc=wacc,
            terminal_growth=terminal_growth,
            tax_rate=tax_rate,
            ebit_margin=ebit_margin,
            projection_years=projection_years,
            revenue_growth=growth_values[0] if len(growth_values) == 1 else growth_values,
        )
        symbol = ticker.upper()

        # Statements come through the cache-aside fundamentals layer
        # (L1 -> Redis -> DB -> FMP); the DCF math is recomputed on every
        # request — it is pure and cheap, and per the 2026-07-16 decision the
        # request/response is never cached, only the statements are.
        #
        # Statements and the live price are fetched CONCURRENTLY (performance
        # item P2): the quote feeds no DCF input — `BaseFinancials` has been
        # price-free by construction since ADR-008 — so serializing them only
        # added the smaller of the two latencies to every request. Accepted
        # trade-off: a request whose statements then fail (404/422/502) has
        # already spent a Finnhub call it used to skip. Bounded by the fact
        # that auth and quota are consumed pre-flight, so only authenticated,
        # quota-paying requests ever reach here.
        finnhub = request.app.state.finnhub

        async def load_statements() -> BaseFinancials:
            with stage("statements"):
                return await request.app.state.fundamentals.get_base_financials(ticker)

        async def load_quote() -> tuple[NormalizedQuote | None, str | None]:
            """Never raises: a price failure is a warning, not an error."""
            if finnhub is None:
                return None, (
                    "Live market price is not configured; current_price and upside_pct are null."
                )
            try:
                with stage("price"):
                    raw_quote, quote_fetched_at = await finnhub.fetch_quote(symbol)
                    return normalize_finnhub_quote(symbol, raw_quote, quote_fetched_at), None
            except (ProviderError, NormalizationError):
                return None, (
                    "Live market price is temporarily unavailable; "
                    "current_price and upside_pct are null."
                )

        statements_result, quote_result = await asyncio.gather(
            load_statements(), load_quote(), return_exceptions=True
        )
        if isinstance(quote_result, BaseException):  # pragma: no cover - load_quote absorbs
            raise quote_result
        if isinstance(statements_result, BaseException):
            # The statements error is the customer-visible one; the quote task
            # has already completed, so nothing is left dangling.
            raise statements_result
        base = statements_result
        quote, price_warning = quote_result

        with stage("compute"):
            valuation = compute_dcf(base, assumptions)
            grid = compute_sensitivity_grid(base, assumptions) if sensitivity else None
        payload = build_valuation_response(
            base,
            assumptions,
            valuation,
            grid,
            request_id=request.state.request_id,
            quote=quote,
            price_warning=price_warning,
        )

        record(
            ticker=symbol,
            model_version=MODEL_VERSION,
            sensitivity=sensitivity,
            price="live" if quote is not None else "unavailable",
        )
        # Never HTTP-cacheable: the body carries a live per-request price.
        response.headers["Cache-Control"] = NO_STORE
        return payload

    @app.get("/internal/cron/refresh-financials", include_in_schema=False)
    async def refresh_financials(request: Request) -> JSONResponse:
        # Vercel cron sends `Authorization: Bearer {CRON_SECRET}`. One
        # generic 401 for every failure mode (unconfigured, missing header,
        # mismatch) so probes learn nothing; comparison is constant-time.
        presented = request.headers.get("authorization", "")
        expected = f"Bearer {cron_secret}" if cron_secret else ""
        if not cron_secret or not hmac.compare_digest(presented.encode(), expected.encode()):
            response = JSONResponse(
                status_code=401,
                content={
                    "detail": "unauthorized",
                    "error": {
                        "version": "1",
                        "code": "cron_unauthorized",
                        "message": "This internal endpoint requires the cron secret.",
                        "request_id": request.state.request_id,
                    },
                },
            )
            response.headers["Cache-Control"] = NO_STORE
            return response

        runner = request.app.state.refresh_runner
        if runner is None:
            response = JSONResponse(
                status_code=503,
                content={
                    "detail": "daily refresh is not configured",
                    "error": {
                        "version": "1",
                        "code": "refresh_not_configured",
                        "message": (
                            "The daily refresh requires the Supabase snapshot store; "
                            "it is not configured in this environment."
                        ),
                        "request_id": request.state.request_id,
                    },
                },
            )
            response.headers["Cache-Control"] = NO_STORE
            return response

        result = await runner.run_if_in_window()
        response = JSONResponse(status_code=200, content=result)
        response.headers["Cache-Control"] = NO_STORE
        return response

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        """Liveness: is this process able to answer at all?

        Touches no dependency and spends no provider call, on purpose — a
        liveness probe that fails when a database is slow gets the instance
        killed for someone else's outage. Dependency state lives at /ready.
        """
        return {
            "status": "ok",
            "model_version": MODEL_VERSION,
            "environment": resolved.environment,
            # Which process answered — the only way to tell one serverless
            # instance from another when reading logs or instance-local metrics.
            "instance": INSTANCE_ID,
        }

    @app.get("/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        """Readiness: are the dependencies this instance needs reachable?

        503 when a fail-closed dependency (Supabase) is unreachable; 200 with a
        `degraded` entry when an accelerator (Redis) is. Results are cached for
        a few seconds and concurrent probes collapse onto one check, so polling
        cannot amplify into the database.
        """
        report = await app.state.readiness.check()
        payload = JSONResponse(
            status_code=200 if report.ready else 503,
            content=report.to_dict(),
        )
        payload.headers["Cache-Control"] = NO_STORE
        return payload

    @app.get("/internal/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        """Prometheus exposition for this instance, behind a bearer token.

        Guarded because the counters describe traffic, quota rejections, and
        provider spend. Same generic-401 shape as the cron endpoint: an
        unconfigured token, a missing header, and a wrong value are
        indistinguishable to a prober.
        """
        presented = request.headers.get("authorization", "")
        expected = f"Bearer {metrics_token}" if metrics_token else ""
        if not metrics_token or not hmac.compare_digest(presented.encode(), expected.encode()):
            denied = JSONResponse(
                status_code=401,
                content={
                    "detail": "unauthorized",
                    "error": {
                        "version": "1",
                        "code": "metrics_unauthorized",
                        "message": "This internal endpoint requires the metrics token.",
                        "request_id": request.state.request_id,
                    },
                },
            )
            denied.headers["Cache-Control"] = NO_STORE
            return denied

        rendered = app.state.metrics.render()
        return Response(
            content=rendered,
            media_type="text/plain; version=0.0.4; charset=utf-8",
            headers={"Cache-Control": NO_STORE},
        )

    return app


# Default instance for `uvicorn app.api:app` (real FMP client, needs FMP_API_KEY)
app = create_app()
