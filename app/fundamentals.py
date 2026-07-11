"""Fundamentals service: fetch + normalize + cache.

This is what the API layer calls. In-process TTL cache keyed per ticker
(CLAUDE.md layer 3; Redis can replace this later behind the same
interface). Fundamentals change quarterly so a long TTL is fine — but note
the cached BaseFinancials includes `current_price`, which moves intraday;
the default TTL is set to hours, not days, until price is fetched
separately.

Negative caching: definitive "this ticker can't be valued" outcomes (unknown
symbol, not covered by the data plan, unsupported sector) are cached too, so
a client repeatedly requesting a bad ticker doesn't spend a provider API call
every time — the daily provider-call budget is small (~250/day on the FMP
free tier). Transient failures (network, 5xx, provider unavailable) are NOT
cached, so they remain retryable.
"""

import time
from typing import Callable, Optional

from .exceptions import (
    TickerNotCoveredError,
    TickerNotFoundError,
    UnsupportedSectorError,
)
from .models import BaseFinancials
from .normalization import normalize_fmp_fundamentals
from .providers.fmp import FMPClient

# Definitive per-ticker rejections worth caching: re-fetching within the TTL
# would return the same answer and only burn API quota.
_CACHEABLE_REJECTIONS = (
    TickerNotFoundError,
    TickerNotCoveredError,
    UnsupportedSectorError,
)


class FundamentalsService:
    def __init__(
        self,
        client: FMPClient,
        ttl_seconds: float = 4 * 3600,
        now: Callable[[], float] = time.monotonic,
    ):
        self._client = client
        self._ttl = ttl_seconds
        self._now = now
        self._cache: dict[str, tuple[float, BaseFinancials]] = {}
        self._negative: dict[str, tuple[float, Exception]] = {}

    async def get_base_financials(self, ticker: str) -> BaseFinancials:
        ticker = ticker.upper()

        cached = self._cache.get(ticker)
        if cached is not None:
            fetched_at, value = cached
            if self._now() - fetched_at < self._ttl:
                return value

        negative = self._negative.get(ticker)
        if negative is not None:
            failed_at, error = negative
            if self._now() - failed_at < self._ttl:
                raise error
            del self._negative[ticker]

        try:
            raw = await self._client.fetch_fundamentals(ticker)
            normalized = normalize_fmp_fundamentals(raw)
        except _CACHEABLE_REJECTIONS as exc:
            self._negative[ticker] = (self._now(), exc)
            raise

        self._cache[ticker] = (self._now(), normalized)
        return normalized

    def invalidate(self, ticker: Optional[str] = None) -> None:
        if ticker is None:
            self._cache.clear()
            self._negative.clear()
        else:
            key = ticker.upper()
            self._cache.pop(key, None)
            self._negative.pop(key, None)
