"""Financial Modeling Prep client (ingestion layer).

Fetches the raw statements needed for one ticker's DCF: income statement,
balance sheet, cash flow statement, and company profile (sector gate).
The market price is NOT fetched here — it comes live from Finnhub per
request (ADR-008) and is never cached. Handles retries with exponential
backoff on 429/5xx and honors Retry-After. Raw JSON can be persisted via a
`raw_sink` hook so normalization bugs can be replayed against the original
payloads.

This module does NO interpretation of the numbers — that belongs to
app.normalization. It only moves bytes and classifies transport errors.
"""

import asyncio
import json
import os
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from ..exceptions import (
    ProviderAuthError,
    ProviderError,
    TickerNotCoveredError,
    TickerNotFoundError,
)

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

RawSink = Callable[[str, str, Any], None]


@dataclass(frozen=True)
class FMPFundamentals:
    """One ticker's raw payloads; normalization selects a compatible period."""

    ticker: str
    income: tuple[dict[str, Any], ...]
    balance: tuple[dict[str, Any], ...]
    cash_flow: tuple[dict[str, Any], ...]
    profile: dict[str, Any]
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class FileRawSink:
    """Default raw-response store: data/raw/{ticker}/{endpoint}_{epoch}.json"""

    def __init__(self, root: Path):
        self._root = Path(root)

    def __call__(self, ticker: str, endpoint: str, payload: Any) -> None:
        directory = self._root / ticker.upper()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{endpoint}_{int(time.time())}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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
        self._client = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout)

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), self._max_retry_after)
            except ValueError:
                pass
        base = min(0.5 * (2**attempt), self._max_retry_after)
        return min(base + self._jitter() * 0.1, self._max_retry_after)

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

        for attempt in range(self._max_retries + 1):
            response: httpx.Response | None = None
            try:
                async with self._semaphore:
                    response = await self._client.get(f"/{endpoint}", params=query)
            except httpx.TransportError as exc:
                last_error = f"transport error: {exc}"
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
                    if self._raw_sink is not None:
                        try:
                            self._raw_sink(ticker, endpoint, payload)
                        except OSError as exc:
                            raise ProviderError(
                                f"raw provider payload sink failed for {endpoint}/{ticker}"
                            ) from exc
                    return payload

            if attempt < self._max_retries:
                await self._sleep(self._retry_delay(response, attempt))

        raise ProviderError(
            f"FMP request failed after {self._max_retries + 1} attempts "
            f"({endpoint}/{ticker}): {last_error}"
        )

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
