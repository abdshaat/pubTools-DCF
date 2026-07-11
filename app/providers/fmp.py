"""Financial Modeling Prep client (ingestion layer).

Fetches the raw statements needed for one ticker's DCF: income statement,
balance sheet, cash flow statement, company profile (sector gate), and
quote (current price). Handles retries with exponential backoff on 429/5xx
and honors Retry-After. Raw JSON can be persisted via a `raw_sink` hook so
normalization bugs can be replayed against the original payloads.

This module does NO interpretation of the numbers — that belongs to
app.normalization. It only moves bytes and classifies transport errors.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

from ..exceptions import (
    ProviderAuthError,
    ProviderError,
    TickerNotCoveredError,
    TickerNotFoundError,
)

DEFAULT_BASE_URL = "https://financialmodelingprep.com/stable"

# (endpoint path, needs limit param)
_STATEMENT_ENDPOINTS = [
    ("income-statement", True),
    ("balance-sheet-statement", True),
    ("cash-flow-statement", True),
    ("profile", False),
    ("quote", False),
]

RawSink = Callable[[str, str, Any], None]


@dataclass(frozen=True)
class FMPFundamentals:
    """One ticker's raw (unnormalized) payloads, most recent period each."""

    ticker: str
    income: dict
    balance: dict
    cash_flow: dict
    profile: dict
    quote: dict


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
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        max_retries: int = 3,
        timeout: float = 10.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        raw_sink: Optional[RawSink] = None,
    ):
        self._api_key = api_key or os.environ.get("FMP_API_KEY")
        if not self._api_key:
            raise ProviderAuthError(
                "no FMP API key: pass api_key= or set the FMP_API_KEY env var"
            )
        self._max_retries = max_retries
        self._sleep = sleep
        self._raw_sink = raw_sink
        self._client = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FMPClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _get_json(self, endpoint: str, ticker: str, params: dict) -> Any:
        query = {"symbol": ticker, "apikey": self._api_key, **params}
        last_error: Optional[str] = None

        for attempt in range(self._max_retries + 1):
            response: Optional[httpx.Response] = None
            try:
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
                else:
                    response.raise_for_status()
                    payload = response.json()
                    if self._raw_sink is not None:
                        self._raw_sink(ticker, endpoint, payload)
                    return payload

            if attempt < self._max_retries:
                retry_after = (
                    response.headers.get("Retry-After") if response is not None else None
                )
                delay = float(retry_after) if retry_after else 0.5 * (2 ** attempt)
                await self._sleep(delay)

        raise ProviderError(
            f"FMP request failed after {self._max_retries + 1} attempts "
            f"({endpoint}/{ticker}): {last_error}"
        )

    async def fetch_fundamentals(self, ticker: str) -> FMPFundamentals:
        """Fetch the five payloads for `ticker`, most recent annual period."""
        ticker = ticker.upper()
        results: dict[str, dict] = {}

        for endpoint, needs_limit in _STATEMENT_ENDPOINTS:
            params = {"limit": 1} if needs_limit else {}
            payload = await self._get_json(endpoint, ticker, params)
            # FMP returns a JSON array (often empty for unknown tickers)
            if isinstance(payload, list):
                if not payload:
                    raise TickerNotFoundError(ticker)
                payload = payload[0]
            results[endpoint] = payload

        return FMPFundamentals(
            ticker=ticker,
            income=results["income-statement"],
            balance=results["balance-sheet-statement"],
            cash_flow=results["cash-flow-statement"],
            profile=results["profile"],
            quote=results["quote"],
        )
