"""Financial Modeling Prep client (ingestion layer).

Fetches the raw statements needed for one ticker's DCF: income statement,
balance sheet, cash flow statement, and company profile (sector gate).
The market price is NOT fetched here — it comes live from Finnhub per
request (ADR-008) and is never cached. Handles retries with exponential
backoff on 429/5xx and honors Retry-After. Successful raw JSON is handed to
the optional `raw_sink` hook (Phase 10 / ADR-009) so normalization bugs can be
replayed against the original payloads; capture is best effort and never fails
a customer request.

This module does NO interpretation of the numbers — that belongs to
app.normalization. It only moves bytes and classifies transport errors.
"""

import asyncio
import inspect
import json
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

import httpx

from ..exceptions import (
    ProviderAuthError,
    ProviderError,
    TickerNotCoveredError,
    TickerNotFoundError,
)
from ..observability import outbound_counter
from ..raw_store import RawCapture
from ..request_context import current_request_id

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://financialmodelingprep.com/stable"
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 6.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_PROVIDER_CONCURRENCY = 3
DEFAULT_MAX_RETRY_AFTER_SECONDS = 2.0
# FMP's starter plans currently allow historical statement `limit` values up to 5.
# Fetching five annual candidates is enough to avoid mixing incomplete recent
# filings while keeping the API compatible with the user's current provider plan.
STATEMENT_FETCH_LIMIT = 5

# (endpoint path, needs limit param)
_STATEMENT_ENDPOINTS = [
    ("income-statement", True),
    ("balance-sheet-statement", True),
    ("cash-flow-statement", True),
    ("profile", False),
]

# A sink receives the whole capture record (payload plus provenance) and may be
# sync or async; the client awaits whatever it returns. See app/raw_store.py.
RawSink = Callable[[RawCapture], Awaitable[None] | None]


@dataclass(frozen=True)
class FMPFundamentals:
    """One ticker's raw payloads; normalization selects a compatible period."""

    ticker: str
    income: tuple[dict[str, Any], ...]
    balance: tuple[dict[str, Any], ...]
    cash_flow: tuple[dict[str, Any], ...]
    profile: dict[str, Any]
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class FMPClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        raw_sink: RawSink | None = None,
        provider_concurrency: int = DEFAULT_PROVIDER_CONCURRENCY,
        max_retry_after_seconds: float = DEFAULT_MAX_RETRY_AFTER_SECONDS,
        jitter: Callable[[], float] = random.random,
    ):
        if provider_concurrency < 1:
            raise ValueError("provider_concurrency must be at least 1")
        if max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds must be non-negative")
        self._api_key = api_key or os.environ.get("FMP_API_KEY")
        if not self._api_key:
            raise ProviderAuthError(
                "no FMP API key: set FMP_API_KEY in the environment or in a local "
                ".env file (copy .env.example), or pass api_key= directly"
            )
        self._max_retries = max_retries
        self._sleep = sleep
        self._raw_sink = raw_sink
        self._semaphore = asyncio.Semaphore(provider_concurrency)
        self._max_retry_after = max_retry_after_seconds
        self._jitter = jitter
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            event_hooks=outbound_counter("fmp"),
        )

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), self._max_retry_after)
            except ValueError:
                pass
        base = min(0.5 * (2**attempt), self._max_retry_after)
        return min(base + self._jitter() * 0.1, self._max_retry_after)

    async def _capture(self, capture: RawCapture) -> None:
        """Hand one successful payload to the audit sink, if any.

        Audit storage is evidence, not part of the answer: a sink failure is
        logged and counted, never surfaced (ADR-009). Sinks may be sync or
        async — the file sink is async so its filesystem work stays off the
        event loop.
        """
        if self._raw_sink is None:
            return
        try:
            result = self._raw_sink(capture)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning(
                "raw capture sink failed for %s/%s",
                capture.endpoint,
                capture.ticker,
                exc_info=True,
            )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FMPClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _get_json(
        self,
        endpoint: str,
        ticker: str,
        params: Mapping[str, str | int | float | bool | None],
    ) -> Any:
        query = {"symbol": ticker, "apikey": self._api_key, **params}
        last_error: str | None = None
        last_exception: BaseException | None = None

        for attempt in range(self._max_retries + 1):
            response: httpx.Response | None = None
            started = time.perf_counter()
            try:
                async with self._semaphore:
                    response = await self._client.get(f"/{endpoint}", params=query)
            except httpx.TransportError as exc:
                # The class name, never the exception text. This client
                # authenticates with `?apikey=` in the query string, so any
                # httpx message that happens to include the request URL would
                # put a live credential in an error string -- the same reason
                # app/providers/finnhub.py chains instead of interpolating.
                # The cause is chained, so a traceback still has everything.
                last_error = f"transport error ({type(exc).__name__})"
                last_exception = exc
            else:
                if response.status_code in (401, 403):
                    raise ProviderAuthError(
                        f"FMP rejected the API key (HTTP {response.status_code})"
                    )
                # 402 Payment Required: FMP returns this for any symbol
                # outside the account's plan coverage. On restricted plans it
                # also comes back for symbols that don't exist, so we surface
                # a distinct "not covered" error rather than asserting the
                # ticker is unknown. Neither 402 nor 404 is retried — the
                # answer won't change, and retrying would waste the daily
                # provider-call budget.
                if response.status_code == 402:
                    raise TickerNotCoveredError(ticker)
                if response.status_code == 404:
                    raise TickerNotFoundError(ticker)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    raise ProviderError(
                        f"FMP returned unsupported HTTP {response.status_code} "
                        f"for {endpoint}/{ticker}"
                    )
                else:
                    response.raise_for_status()
                    try:
                        payload = response.json()
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            f"FMP returned malformed JSON for {endpoint}/{ticker}"
                        ) from exc
                    await self._capture(
                        RawCapture(
                            provider="fmp",
                            ticker=ticker,
                            endpoint=endpoint,
                            payload=payload,
                            status_code=response.status_code,
                            url=str(response.request.url),
                            request_headers=dict(response.request.headers),
                            response_headers=dict(response.headers),
                            # Wall time for this attempt, including any wait
                            # for one of the client's concurrency slots.
                            elapsed_ms=(time.perf_counter() - started) * 1000.0,
                            attempt=attempt + 1,
                            request_id=current_request_id(),
                        )
                    )
                    return payload

            if attempt < self._max_retries:
                await self._sleep(self._retry_delay(response, attempt))

        raise ProviderError(
            f"FMP request failed after {self._max_retries + 1} attempts "
            f"({endpoint}/{ticker}): {last_error}"
        ) from last_exception

    async def fetch_fundamentals(
        self,
        ticker: str,
        *,
        profile_override: dict[str, Any] | None = None,
    ) -> FMPFundamentals:
        """Fetch candidate statements plus current profile for `ticker`."""
        ticker = ticker.upper()
        results: dict[str, Any] = {}
        if profile_override is not None:
            results["profile"] = profile_override

        async def fetch_endpoint(endpoint: str, needs_limit: bool) -> tuple[str, bool, Any]:
            params = {"limit": STATEMENT_FETCH_LIMIT} if needs_limit else {}
            payload = await self._get_json(endpoint, ticker, params)
            return endpoint, needs_limit, payload

        tasks = [
            fetch_endpoint(endpoint, needs_limit)
            for endpoint, needs_limit in _STATEMENT_ENDPOINTS
            if endpoint not in results
        ]
        for endpoint, needs_limit, payload in await asyncio.gather(*tasks):
            # FMP returns a JSON array (often empty for unknown tickers)
            if isinstance(payload, list):
                if not payload:
                    raise TickerNotFoundError(ticker)
                if needs_limit:
                    if not all(isinstance(record, dict) for record in payload):
                        raise ProviderError(
                            f"FMP returned malformed {endpoint} records for {ticker}"
                        )
                    results[endpoint] = tuple(payload)
                else:
                    # The single-record endpoint (profile). Checking the element
                    # matters as much as checking the container: a non-dict here
                    # reaches normalization as `f.profile`, where `.get("sector")`
                    # is an AttributeError and an unhandled 500. `fetch_profile`
                    # below already guards this; only this path had lost it.
                    if not isinstance(payload[0], dict):
                        raise ProviderError(
                            f"FMP returned malformed {endpoint} payload for {ticker}"
                        )
                    results[endpoint] = payload[0]
            elif needs_limit:
                if not isinstance(payload, dict):
                    raise ProviderError(f"FMP returned malformed {endpoint} payload for {ticker}")
                results[endpoint] = (payload,)
            elif isinstance(payload, dict):
                results[endpoint] = payload
            else:
                raise ProviderError(f"FMP returned malformed {endpoint} payload for {ticker}")

        return FMPFundamentals(
            ticker=ticker,
            income=results["income-statement"],
            balance=results["balance-sheet-statement"],
            cash_flow=results["cash-flow-statement"],
            profile=results["profile"],
            fetched_at=datetime.now(UTC),
        )

    async def fetch_profile(self, ticker: str) -> dict[str, Any]:
        """Fetch company profile independently of statements and quote."""
        ticker = ticker.upper()
        payload = await self._get_json("profile", ticker, {})
        if isinstance(payload, list):
            if not payload:
                raise TickerNotFoundError(ticker)
            payload = payload[0]
        if not isinstance(payload, dict):
            raise ProviderError(f"FMP returned malformed profile payload for {ticker}")
        return payload
