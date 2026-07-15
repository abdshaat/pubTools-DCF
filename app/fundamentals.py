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

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import datetime
from math import isfinite
from typing import Any
from uuid import uuid4

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
from .redis_cache import REDIS_KEY_PREFIX, RedisBackend, get_envelope, set_envelope

# Definitive per-ticker rejections worth caching: re-fetching within the TTL
# would return the same answer and only burn API quota.
_CACHEABLE_REJECTIONS = (
    TickerNotFoundError,
    TickerNotCoveredError,
    UnsupportedSectorError,
)

# Five endpoints run through a three-slot semaphore and each may make three
# six-second attempts. Forty-five seconds covers that bounded healthy path;
# losers still wait only three seconds before fetching independently.
_LOCK_TTL_MILLISECONDS = 45_000


def _base_to_payload(base: BaseFinancials) -> dict[str, Any]:
    payload = asdict(base)
    for field in ("price_as_of", "price_fetched_at"):
        value = payload[field]
        if isinstance(value, datetime):
            payload[field] = value.isoformat()
    return payload


def _base_from_payload(payload: Any) -> BaseFinancials | None:
    if not isinstance(payload, dict):
        return None
    values = dict(payload)
    try:
        for field in ("price_as_of", "price_fetched_at"):
            if values.get(field) is not None:
                values[field] = datetime.fromisoformat(str(values[field]))
        warnings = tuple(values.get("data_quality_warnings", ()))
        if not all(isinstance(item, str) for item in warnings):
            return None
        values["data_quality_warnings"] = warnings
        base = BaseFinancials(**values)
        numbers = (
            base.revenue,
            base.ebit,
            base.da,
            base.capex,
            base.delta_nwc,
            base.net_debt,
            base.diluted_shares,
            base.current_price,
        )
        if (
            not base.ticker
            or not all(isinstance(value, (int, float)) and isfinite(value) for value in numbers)
            or base.revenue <= 0
            or base.diluted_shares <= 0
            or base.current_price <= 0
            or base.da < 0
            or base.capex < 0
        ):
            return None
        return base
    except (TypeError, ValueError):
        return None


def _quote_to_payload(quote: NormalizedQuote) -> dict[str, Any]:
    return {
        "price": quote.price,
        "price_as_of": quote.price_as_of.isoformat() if quote.price_as_of else None,
        "fetched_at": quote.fetched_at.isoformat(),
    }


def _quote_from_payload(payload: Any) -> NormalizedQuote | None:
    if not isinstance(payload, dict):
        return None
    try:
        quote = NormalizedQuote(
            price=float(payload["price"]),
            price_as_of=(
                datetime.fromisoformat(str(payload["price_as_of"]))
                if payload.get("price_as_of") is not None
                else None
            ),
            fetched_at=datetime.fromisoformat(str(payload["fetched_at"])),
        )
        if not isfinite(quote.price) or quote.price <= 0:
            return None
        return quote
    except (KeyError, TypeError, ValueError):
        return None


def _error_to_payload(error: Exception) -> dict[str, str]:
    if isinstance(error, TickerNotFoundError):
        code = "ticker_not_found"
    elif isinstance(error, TickerNotCoveredError):
        code = "ticker_not_covered"
    elif isinstance(error, UnsupportedSectorError):
        code = "unsupported_sector"
    else:  # guarded by _CACHEABLE_REJECTIONS
        raise TypeError("unsupported negative-cache error")
    payload = {"error": code, "message": str(error)}
    if isinstance(error, UnsupportedSectorError):
        payload["sector"] = error.sector
    return payload


def _error_from_payload(ticker: str, payload: Any) -> Exception | None:
    if not isinstance(payload, dict):
        return None
    code = payload.get("error")
    if code == "ticker_not_found":
        return TickerNotFoundError(ticker)
    if code == "ticker_not_covered":
        return TickerNotCoveredError(ticker)
    if code == "unsupported_sector" and isinstance(payload.get("sector"), str):
        return UnsupportedSectorError(ticker, payload["sector"])
    return None


class FundamentalsService:
    def __init__(
        self,
        client: FMPClient,
        ttl_seconds: float = 4 * 3600,
        profile_ttl_seconds: float = 24 * 3600,
        quote_ttl_seconds: float = 60,
        negative_ttl_seconds: float | None = None,
        max_statement_staleness_seconds: float = 24 * 3600,
        max_quote_staleness_seconds: float = 15 * 60,
        now: Callable[[], float] = time.monotonic,
        wall_now: Callable[[], float] = time.time,
        redis: RedisBackend | None = None,
        redis_poll_seconds: float = 3.0,
        redis_poll_interval_seconds: float = 0.2,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self._client = client
        self._statement_ttl = ttl_seconds
        self._profile_ttl = profile_ttl_seconds
        self._quote_ttl = quote_ttl_seconds
        self._negative_ttl = negative_ttl_seconds or ttl_seconds
        self._max_statement_staleness = max_statement_staleness_seconds
        self._max_quote_staleness = max_quote_staleness_seconds
        self._now = now
        self._wall_now = wall_now
        self._redis = redis
        self._redis_poll_seconds = redis_poll_seconds
        self._redis_poll_interval = redis_poll_interval_seconds
        self._sleep = sleep
        self._cache: dict[str, tuple[float, BaseFinancials]] = {}
        self._raw_cache: dict[str, tuple[float, FMPFundamentals]] = {}
        self._profile_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._quote_cache: dict[str, tuple[float, NormalizedQuote]] = {}
        self._negative: dict[str, tuple[float, Exception]] = {}
        self._inflight: dict[str, asyncio.Task[BaseFinancials]] = {}

    @staticmethod
    def _redis_key(kind: str, ticker: str) -> str:
        return f"{REDIS_KEY_PREFIX}{kind}:{ticker}"

    async def _remote_get(self, kind: str, ticker: str) -> tuple[float, Any] | None:
        if self._redis is None:
            return None
        key = self._redis_key(kind, ticker)
        try:
            envelope = await get_envelope(self._redis, key)
        except Exception:
            return None
        if envelope is None:
            return None
        return max(0.0, self._wall_now() - envelope.stored_at), envelope.data

    async def _remote_delete(self, kind: str, ticker: str) -> None:
        if self._redis is None:
            return
        with suppress(Exception):
            await self._redis.delete(self._redis_key(kind, ticker))

    async def _remote_set(self, kind: str, ticker: str, data: Any, ttl: float) -> None:
        if self._redis is None:
            return
        with suppress(Exception):
            await set_envelope(
                self._redis,
                self._redis_key(kind, ticker),
                data,
                ttl_seconds=max(1, int(ttl)),
                stored_at=self._wall_now(),
            )

    async def _remote_base(self, ticker: str) -> tuple[float, BaseFinancials] | None:
        cached = await self._remote_get("fund", ticker)
        if cached is None:
            return None
        age, payload = cached
        base = _base_from_payload(payload)
        if base is None or base.ticker.upper() != ticker:
            await self._remote_delete("fund", ticker)
            return None
        return age, base

    async def _remote_quote(self, ticker: str) -> tuple[float, NormalizedQuote] | None:
        cached = await self._remote_get("quote", ticker)
        if cached is None:
            return None
        age, payload = cached
        quote = _quote_from_payload(payload)
        if quote is None:
            await self._remote_delete("quote", ticker)
            return None
        return age, quote

    async def _remote_error(self, ticker: str) -> tuple[float, Exception] | None:
        cached = await self._remote_get("neg", ticker)
        if cached is None:
            return None
        age, payload = cached
        error = _error_from_payload(ticker, payload)
        if error is None:
            await self._remote_delete("neg", ticker)
            return None
        return age, error

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
        if cached is None:
            remote = await self._remote_quote(ticker)
            if remote is not None:
                age, quote = remote
                cached = (self._now() - age, quote)
                self._quote_cache[ticker] = cached
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
        await self._remote_set("quote", ticker, _quote_to_payload(quote), self._max_quote_staleness)
        return self._apply_quote(base, quote)

    def _track_inflight(self, ticker: str, task: asyncio.Task[BaseFinancials]) -> None:
        self._inflight[ticker] = task

        def cleanup(done: asyncio.Task[BaseFinancials]) -> None:
            if self._inflight.get(ticker) is done:
                self._inflight.pop(ticker, None)

        task.add_done_callback(cleanup)

    async def _try_distributed_lock(self, ticker: str) -> tuple[bool, str | None]:
        """Return (Redis reachable, holder token or None when another holder won)."""
        if self._redis is None:
            return False, None
        token = uuid4().hex
        try:
            acquired = await self._redis.set(
                self._redis_key("lock:fund", ticker),
                token,
                px=_LOCK_TTL_MILLISECONDS,
                nx=True,
            )
        except Exception:
            return False, None
        return True, token if acquired else None

    async def _release_distributed_lock(self, ticker: str, token: str) -> None:
        if self._redis is None:
            return
        with suppress(Exception):
            await self._redis.compare_and_delete(self._redis_key("lock:fund", ticker), token)

    async def _wait_for_remote_refresh(self, ticker: str) -> BaseFinancials | None:
        deadline = self._now() + self._redis_poll_seconds
        while self._now() < deadline:
            await self._sleep(self._redis_poll_interval)
            cached = await self._remote_base(ticker)
            if cached is not None and cached[0] < self._statement_ttl:
                age, base = cached
                self._cache[ticker] = (self._now() - age, base)
                return base
        return None

    async def _load_base_financials(self, ticker: str) -> BaseFinancials:
        stale_base: BaseFinancials | None = None
        stale_age: float | None = None
        cached = self._cache.get(ticker)
        if cached is not None:
            fetched_at, value = cached
            stale_base = value
            stale_age = self._now() - fetched_at
            if stale_age < self._statement_ttl:
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
                    await self._remote_set("profile", ticker, refreshed_profile, self._profile_ttl)
                    await self._remote_set(
                        "fund", ticker, _base_to_payload(value), self._max_statement_staleness
                    )
                return await self._get_quote(ticker, value)

        remote_base = await self._remote_base(ticker)
        if remote_base is not None and (stale_age is None or remote_base[0] < stale_age):
            stale_age, stale_base = remote_base
            self._cache[ticker] = (self._now() - stale_age, stale_base)
        if stale_base is not None and stale_age is not None and stale_age < self._statement_ttl:
            return await self._get_quote(ticker, stale_base)

        negative = self._negative.get(ticker)
        if negative is not None:
            failed_at, error = negative
            if self._now() - failed_at < self._negative_ttl:
                raise error
            del self._negative[ticker]
        remote_negative = await self._remote_error(ticker)
        if remote_negative is not None and remote_negative[0] < self._negative_ttl:
            age, error = remote_negative
            self._negative[ticker] = (self._now() - age, error)
            raise error

        redis_reachable, lock_token = await self._try_distributed_lock(ticker)
        if redis_reachable and lock_token is None:
            refreshed = await self._wait_for_remote_refresh(ticker)
            if refreshed is not None:
                return await self._get_quote(ticker, refreshed)

        try:
            profile_override = None
            profile = self._profile_cache.get(ticker)
            if profile is None:
                remote_profile = await self._remote_get("profile", ticker)
                if remote_profile is not None and isinstance(remote_profile[1], dict):
                    age, profile_value = remote_profile
                    profile = (self._now() - age, profile_value)
                    self._profile_cache[ticker] = profile
                elif remote_profile is not None:
                    await self._remote_delete("profile", ticker)
            if profile is not None and self._now() - profile[0] < self._profile_ttl:
                profile_override = profile[1]

            cached_quote = self._quote_cache.get(ticker)
            if cached_quote is None:
                remote_quote = await self._remote_quote(ticker)
                if remote_quote is not None:
                    age, quote_value = remote_quote
                    cached_quote = (self._now() - age, quote_value)
                    self._quote_cache[ticker] = cached_quote
            quote_override = None
            quote_fetched_at = None
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
            await self._remote_set("neg", ticker, _error_to_payload(exc), self._negative_ttl)
            if lock_token is not None:
                await self._release_distributed_lock(ticker, lock_token)
            raise
        except (ProviderError, NormalizationError):
            if (
                stale_base is not None
                and stale_age is not None
                and stale_age <= self._max_statement_staleness
            ):
                stale_with_warning = replace(
                    stale_base,
                    data_quality_warnings=(
                        *stale_base.data_quality_warnings,
                        "Fundamentals refresh failed; a bounded stale statement snapshot was used.",
                    ),
                )
                if lock_token is not None:
                    await self._release_distributed_lock(ticker, lock_token)
                return await self._get_quote(ticker, stale_with_warning)
            if lock_token is not None:
                await self._release_distributed_lock(ticker, lock_token)
            raise

        self._cache[ticker] = (self._now(), normalized)
        self._raw_cache[ticker] = (self._now(), raw)
        self._profile_cache[ticker] = (self._now(), raw.profile)
        await self._remote_set("profile", ticker, raw.profile, self._profile_ttl)
        if normalized.price_fetched_at is not None and cached_quote is None:
            quote = NormalizedQuote(
                price=normalized.current_price,
                price_as_of=normalized.price_as_of,
                fetched_at=normalized.price_fetched_at,
            )
            self._quote_cache[ticker] = (self._now(), quote)
            await self._remote_set(
                "quote", ticker, _quote_to_payload(quote), self._max_quote_staleness
            )
        # Publish `fund:` last: lock losers poll it as the commit marker and
        # must not observe the snapshot before its companion caches exist.
        await self._remote_set(
            "fund", ticker, _base_to_payload(normalized), self._max_statement_staleness
        )
        if lock_token is not None:
            await self._release_distributed_lock(ticker, lock_token)
        return await self._get_quote(ticker, normalized)

    async def get_base_financials(self, ticker: str) -> BaseFinancials:
        ticker = ticker.upper()

        task = self._inflight.get(ticker)
        if task is None:
            task = asyncio.create_task(self._load_base_financials(ticker))
            self._track_inflight(ticker, task)
        return await asyncio.shield(task)

    def invalidate(self, ticker: str | None = None) -> None:
        if ticker is None:
            self._cache.clear()
            self._raw_cache.clear()
            self._profile_cache.clear()
            self._quote_cache.clear()
            self._negative.clear()
            self._inflight.clear()
        else:
            key = ticker.upper()
            self._cache.pop(key, None)
            self._raw_cache.pop(key, None)
            self._profile_cache.pop(key, None)
            self._quote_cache.pop(key, None)
            self._negative.pop(key, None)
            self._inflight.pop(key, None)
