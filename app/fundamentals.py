"""Fundamentals service: fetch + normalize + cache.

This is what the API layer calls. In-process TTL cache keyed per ticker
(CLAUDE.md layer 3; Redis can replace this later behind the same
interface). On Vercel this cache is only a warm-instance optimization: it is
never relied on for correctness, metering, or cross-instance coordination.
Fundamentals change quarterly so a long TTL is fine — but note
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
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .exceptions import (
    NormalizationError,
    ProviderError,
    TickerNotCoveredError,
    TickerNotFoundError,
    UnsupportedSectorError,
)
from .models import BaseFinancials
from .normalization import NormalizedQuote, normalize_fmp_fundamentals, normalize_fmp_quote
from .providers.fmp import FMPClient, FMPFundamentals

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
        profile_ttl_seconds: float = 24 * 3600,
        quote_ttl_seconds: float = 60,
        negative_ttl_seconds: float | None = None,
        max_quote_staleness_seconds: float = 15 * 60,
        now: Callable[[], float] = time.monotonic,
    ):
        self._client = client
        self._statement_ttl = ttl_seconds
        self._profile_ttl = profile_ttl_seconds
        self._quote_ttl = quote_ttl_seconds
        self._negative_ttl = negative_ttl_seconds or ttl_seconds
        self._max_quote_staleness = max_quote_staleness_seconds
        self._now = now
        self._cache: dict[str, tuple[float, BaseFinancials]] = {}
        self._raw_cache: dict[str, tuple[float, FMPFundamentals]] = {}
        self._profile_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._quote_cache: dict[str, tuple[float, NormalizedQuote]] = {}
        self._negative: dict[str, tuple[float, Exception]] = {}

    @staticmethod
    def _apply_quote(
        base: BaseFinancials, quote: NormalizedQuote, warning: str | None = None
    ) -> BaseFinancials:
        warnings = base.data_quality_warnings
        if warning is not None:
            warnings = (*warnings, warning)
        return replace(
            base,
            current_price=quote.price,
            price_as_of=quote.price_as_of,
            price_fetched_at=quote.fetched_at,
            data_quality_warnings=warnings,
        )

    async def _get_quote(self, ticker: str, base: BaseFinancials) -> BaseFinancials:
        cached = self._quote_cache.get(ticker)
        if cached is not None:
            fetched_at, quote = cached
            age = self._now() - fetched_at
            if age < self._quote_ttl:
                return self._apply_quote(base, quote)
        try:
            raw_quote, quote_fetched_at = await self._client.fetch_quote(ticker)
            quote = normalize_fmp_quote(ticker, raw_quote, quote_fetched_at)
        except (ProviderError, NormalizationError):
            if cached is not None and self._now() - cached[0] <= self._max_quote_staleness:
                return self._apply_quote(
                    base,
                    cached[1],
                    "Current quote refresh failed; a bounded stale quote was used.",
                )
            raise
        self._quote_cache[ticker] = (self._now(), quote)
        return self._apply_quote(base, quote)

    async def get_base_financials(self, ticker: str) -> BaseFinancials:
        ticker = ticker.upper()

        cached = self._cache.get(ticker)
        if cached is not None:
            fetched_at, value = cached
            if self._now() - fetched_at < self._statement_ttl:
                profile = self._profile_cache.get(ticker)
                raw_cached = self._raw_cache.get(ticker)
                if (
                    profile is not None
                    and raw_cached is not None
                    and self._now() - profile[0] >= self._profile_ttl
                ):
                    refreshed_profile = await self._client.fetch_profile(ticker)
                    raw = replace(raw_cached[1], profile=refreshed_profile)
                    value = normalize_fmp_fundamentals(raw)
                    self._cache[ticker] = (fetched_at, value)
                    self._raw_cache[ticker] = (raw_cached[0], raw)
                    self._profile_cache[ticker] = (self._now(), refreshed_profile)
                return await self._get_quote(ticker, value)

        negative = self._negative.get(ticker)
        if negative is not None:
            failed_at, error = negative
            if self._now() - failed_at < self._negative_ttl:
                raise error
            del self._negative[ticker]

        try:
            profile_override = None
            profile = self._profile_cache.get(ticker)
            if profile is not None and self._now() - profile[0] < self._profile_ttl:
                profile_override = profile[1]

            quote_override = None
            quote_fetched_at = None
            cached_quote = self._quote_cache.get(ticker)
            if cached_quote is not None:
                normalized_quote = cached_quote[1]
                quote_override = {"price": normalized_quote.price}
                if normalized_quote.price_as_of is not None:
                    quote_override["timestamp"] = normalized_quote.price_as_of.timestamp()
                quote_fetched_at = normalized_quote.fetched_at

            raw = await self._client.fetch_fundamentals(
                ticker,
                profile_override=profile_override,
                quote_override=quote_override,
                quote_fetched_at=quote_fetched_at,
            )
            normalized = normalize_fmp_fundamentals(raw)
        except _CACHEABLE_REJECTIONS as exc:
            self._negative[ticker] = (self._now(), exc)
            raise

        self._cache[ticker] = (self._now(), normalized)
        self._raw_cache[ticker] = (self._now(), raw)
        self._profile_cache[ticker] = (self._now(), raw.profile)
        if normalized.price_fetched_at is not None and cached_quote is None:
            self._quote_cache[ticker] = (
                self._now(),
                NormalizedQuote(
                    price=normalized.current_price,
                    price_as_of=normalized.price_as_of,
                    fetched_at=normalized.price_fetched_at,
                ),
            )
        return await self._get_quote(ticker, normalized)

    def invalidate(self, ticker: str | None = None) -> None:
        if ticker is None:
            self._cache.clear()
            self._raw_cache.clear()
            self._profile_cache.clear()
            self._quote_cache.clear()
            self._negative.clear()
        else:
            key = ticker.upper()
            self._cache.pop(key, None)
            self._raw_cache.pop(key, None)
            self._profile_cache.pop(key, None)
            self._quote_cache.pop(key, None)
            self._negative.pop(key, None)
