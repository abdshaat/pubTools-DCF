"""Fundamentals service: fetch + normalize + cache.

This is what the API layer calls. In-process TTL cache keyed per ticker
(CLAUDE.md layer 3; Redis can replace this later behind the same
interface). Fundamentals change quarterly so a long TTL is fine — but note
the cached BaseFinancials includes `current_price`, which moves intraday;
the default TTL is set to hours, not days, until price is fetched
separately.
"""

import time
from typing import Callable, Optional

from .models import BaseFinancials
from .normalization import normalize_fmp_fundamentals
from .providers.fmp import FMPClient


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

    async def get_base_financials(self, ticker: str) -> BaseFinancials:
        ticker = ticker.upper()

        cached = self._cache.get(ticker)
        if cached is not None:
            fetched_at, value = cached
            if self._now() - fetched_at < self._ttl:
                return value

        raw = await self._client.fetch_fundamentals(ticker)
        normalized = normalize_fmp_fundamentals(raw)
        self._cache[ticker] = (self._now(), normalized)
        return normalized

    def invalidate(self, ticker: Optional[str] = None) -> None:
        if ticker is None:
            self._cache.clear()
        else:
            self._cache.pop(ticker.upper(), None)
