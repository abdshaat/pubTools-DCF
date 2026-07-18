"""Finnhub client (real-time market price, ADR-008).

Fetches one thing only: the live quote for a ticker, on every valuation
request. Finnhub is the sole source of the market price; FMP remains the
source of statements/profile. Per ADR-008 the price is never cached, so this
client performs no retries — a quote is latency-sensitive, and the caller
degrades to a null price rather than waiting out a retry ladder.

Auto-enables via `FINNHUB_API_KEY` (same pattern as Supabase/Redis): variable
absent → `FinnhubConfig.from_env()` returns None and the price feature stays
off. This module moves bytes and classifies transport errors only; number
interpretation belongs to app.normalization.
"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

import httpx

from ..exceptions import ProviderAuthError, ProviderError, TickerNotFoundError

DEFAULT_BASE_URL = "https://finnhub.io/api/v1"
# Well under the Vercel function budget: one quote attempt, no retries.
DEFAULT_QUOTE_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class FinnhubConfig:
    api_key: str

    @classmethod
    def from_env(cls) -> "FinnhubConfig | None":
        api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(api_key=api_key)


class FinnhubClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_QUOTE_TIMEOUT_SECONDS,
    ):
        self._api_key = api_key or os.environ.get("FINNHUB_API_KEY")
        if not self._api_key:
            raise ProviderAuthError(
                "no Finnhub API key: set FINNHUB_API_KEY in the environment or "
                "pass api_key= directly"
            )
        self._client = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FinnhubClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def fetch_quote(self, ticker: str) -> tuple[dict[str, Any], datetime]:
        """Fetch the live quote for `ticker`; returns (raw payload, fetched_at).

        Raw shape (Finnhub /quote): c=current price, t=unix seconds of the
        quote, plus o/h/l/pc/d/dp which this project ignores.
        """
        ticker = ticker.upper()
        try:
            # The exception is chained, never interpolated: httpx error text can
            # embed the request URL, which carries the token.
            response = await self._client.get(
                "/quote", params={"symbol": ticker, "token": self._api_key}
            )
        except httpx.TransportError as exc:
            raise ProviderError(f"Finnhub quote request failed for {ticker}") from exc
        if response.status_code in (401, 403):
            raise ProviderAuthError(f"Finnhub rejected the API key (HTTP {response.status_code})")
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderError(
                f"Finnhub is unavailable for {ticker} (HTTP {response.status_code})"
            )
        if response.status_code != 200:
            raise ProviderError(
                f"Finnhub returned unsupported HTTP {response.status_code} for quote/{ticker}"
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Finnhub returned malformed JSON for quote/{ticker}") from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"Finnhub returned malformed quote payload for {ticker}")
        # Finnhub answers an unknown symbol with HTTP 200 and an all-zero body
        # (c=0, t=0) rather than a 404.
        if payload.get("c") in (None, 0):
            raise TickerNotFoundError(ticker)
        return payload, datetime.now(UTC)
