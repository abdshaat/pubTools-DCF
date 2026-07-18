"""Fundamentals service: fetch + normalize + cache (statements only).

This is what the API layer calls. In-process TTL cache keyed per ticker
(CLAUDE.md layer 3; Redis L2 behind the same interface). On Vercel this
cache is only a warm-instance optimization: it is never relied on for
correctness, metering, or cross-instance coordination. Fundamentals change
quarterly so a long TTL is fine. Per ADR-008 the cached snapshot carries NO
market price — the live price comes from Finnhub in the API layer and is
never cached at any level.

Negative caching: definitive "this ticker can't be valued" outcomes (unknown
symbol, not covered by the data plan, unsupported sector) are cached too, so
a client repeatedly requesting a bad ticker doesn't spend a provider API call
every time — the daily provider-call budget is small (~250/day on the FMP
free tier). Transient failures (network, 5xx, provider unavailable) are NOT
cached, so they remain retryable.
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from math import isfinite
from typing import Any, Protocol
from uuid import uuid4

from .exceptions import (
    NormalizationError,
    ProviderError,
    SnapshotStoreError,
    TickerNotCoveredError,
    TickerNotFoundError,
    UnsupportedSectorError,
)
from .models import BaseFinancials
from .normalization import normalize_fmp_fundamentals
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

# Bumped only on incompatible layout changes of the durable snapshot document;
# readers treat an unknown version as a malformed row (bootstrap repairs it).
_SNAPSHOT_DOCUMENT_VERSION = 1

# A head verified this far in the future is corrupt, not merely clock-skewed.
_FUTURE_VERIFIED_TOLERANCE_SECONDS = 300.0


@dataclass(frozen=True)
class TickerSnapshotRecord:
    """One ticker's durable snapshot: the immutable normalized document plus
    the mutable head's verification metadata. Price-free by construction
    (ADR-008) — nothing in the stored document can represent a market price."""

    ticker: str
    snapshot_version: str
    verified_at: datetime
    refresh_status: str | None
    snapshot: dict[str, Any]


class SnapshotStore(Protocol):
    """Durable statement store (Supabase in production). The service treats
    any raised exception as a storage outage — fail-closed, never a miss."""

    async def get_ticker_snapshot(self, ticker: str) -> TickerSnapshotRecord | None: ...

    async def store_ticker_snapshot(
        self,
        *,
        ticker: str,
        snapshot_version: str,
        snapshot: dict[str, Any],
        provider: str,
        fiscal_year: int | None,
        statement_date: str | None,
        currency: str | None,
        refresh_status: str,
    ) -> None: ...


def snapshot_fingerprint(base: BaseFinancials) -> str:
    """Content address of the price-free normalized payload — the same
    canonical-JSON/SHA-256 recipe as the response's data_version, so a later
    provider fetch that re-confirms an identical filing hashes identically
    and the store's ON CONFLICT DO NOTHING keeps exactly one row."""
    canonical = json.dumps(
        _base_to_payload(base), sort_keys=True, separators=(",", ":"), default=str
    )
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _snapshot_document(base: BaseFinancials, profile: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "v": _SNAPSHOT_DOCUMENT_VERSION,
        "base": _base_to_payload(base),
        "profile": profile,
    }


def _snapshot_base_and_profile(
    record: TickerSnapshotRecord, ticker: str
) -> tuple[BaseFinancials, dict[str, Any] | None] | None:
    """Decode a stored document; None means a malformed/wrong-ticker row that
    must never reach the engine (a bootstrap will overwrite the head)."""
    document = record.snapshot
    if not isinstance(document, dict) or document.get("v") != _SNAPSHOT_DOCUMENT_VERSION:
        return None
    base = _base_from_payload(document.get("base"))
    if base is None or base.ticker.upper() != ticker:
        return None
    profile = document.get("profile")
    return base, profile if isinstance(profile, dict) else None


def _fiscal_year_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _iso_date_or_none(value: str | None) -> str | None:
    """Only a parseable ISO date may reach the store's `date` column — a
    malformed string would fail the whole RPC call."""
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _base_to_payload(base: BaseFinancials) -> dict[str, Any]:
    # The snapshot is price-free by construction (ADR-008): BaseFinancials
    # has no price field, so nothing here can leak a market price into Redis.
    return asdict(base)


def _base_from_payload(payload: Any) -> BaseFinancials | None:
    if not isinstance(payload, dict):
        return None
    values = dict(payload)
    try:
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
        )
        if (
            not base.ticker
            or not all(isinstance(value, (int, float)) and isfinite(value) for value in numbers)
            or base.revenue <= 0
            or base.diluted_shares <= 0
            or base.da < 0
            or base.capex < 0
        ):
            return None
        return base
    except (TypeError, ValueError):
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
        negative_ttl_seconds: float | None = None,
        max_statement_staleness_seconds: float = 24 * 3600,
        now: Callable[[], float] = time.monotonic,
        wall_now: Callable[[], float] = time.time,
        redis: RedisBackend | None = None,
        redis_poll_seconds: float = 3.0,
        redis_poll_interval_seconds: float = 0.2,
        sleep: Callable[[float], Any] = asyncio.sleep,
        snapshots: SnapshotStore | None = None,
    ):
        self._client = client
        self._statement_ttl = ttl_seconds
        self._profile_ttl = profile_ttl_seconds
        self._negative_ttl = negative_ttl_seconds or ttl_seconds
        self._max_statement_staleness = max_statement_staleness_seconds
        self._now = now
        self._wall_now = wall_now
        self._redis = redis
        self._snapshots = snapshots
        self._redis_poll_seconds = redis_poll_seconds
        self._redis_poll_interval = redis_poll_interval_seconds
        self._sleep = sleep
        self._cache: dict[str, tuple[float, BaseFinancials]] = {}
        self._raw_cache: dict[str, tuple[float, FMPFundamentals]] = {}
        self._profile_cache: dict[str, tuple[float, dict[str, Any]]] = {}
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

    async def _read_snapshot_store(
        self,
        ticker: str,
        stale_base: BaseFinancials | None,
        stale_age: float | None,
        lock_token: str | None,
    ) -> BaseFinancials | None:
        """Resolve the ticker from the durable store. Returns the snapshot to
        serve, or None for a confirmed miss/malformed row (bootstrap may
        proceed). A store *error* is not a miss: degrade to a bounded stale
        cache copy or raise SnapshotStoreError — never fall through to FMP."""
        assert self._snapshots is not None
        try:
            record = await self._snapshots.get_ticker_snapshot(ticker)
        except Exception as exc:
            if (
                stale_base is not None
                and stale_age is not None
                and stale_age <= self._max_statement_staleness
            ):
                if lock_token is not None:
                    await self._release_distributed_lock(ticker, lock_token)
                return replace(
                    stale_base,
                    data_quality_warnings=(
                        *stale_base.data_quality_warnings,
                        "Durable snapshot store unavailable; a bounded stale statement "
                        "snapshot was used.",
                    ),
                )
            if lock_token is not None:
                await self._release_distributed_lock(ticker, lock_token)
            raise SnapshotStoreError(ticker) from exc

        if record is None:
            return None
        age = self._wall_now() - record.verified_at.timestamp()
        if age < -_FUTURE_VERIFIED_TOLERANCE_SECONDS:
            # Future-dated head: corrupt, never served; bootstrap repairs it.
            return None
        parsed = _snapshot_base_and_profile(record, ticker)
        if parsed is None:
            return None
        base, profile = parsed
        age = max(0.0, age)
        if age > self._max_statement_staleness:
            base = replace(
                base,
                data_quality_warnings=(
                    *base.data_quality_warnings,
                    f"Statement snapshot last verified {record.verified_at.isoformat()}; "
                    "served as stored — customer requests never trigger a provider "
                    "refresh (scheduled refresh pending).",
                ),
            )
        self._cache[ticker] = (self._now() - age, base)
        if profile is not None:
            self._profile_cache[ticker] = (self._now(), profile)
            await self._remote_set("profile", ticker, profile, self._profile_ttl)
        # `fund:` last: it is the distributed commit marker lock losers poll.
        await self._remote_set(
            "fund", ticker, _base_to_payload(base), self._max_statement_staleness
        )
        if lock_token is not None:
            await self._release_distributed_lock(ticker, lock_token)
        return base

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
                    # Request-time profile refresh calls FMP for an existing
                    # ticker, which the durable-store policy forbids (ADR-007:
                    # only the scheduled cycle refreshes existing tickers).
                    self._snapshots is None
                    and profile is not None
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
                return value

        remote_base = await self._remote_base(ticker)
        if remote_base is not None and (stale_age is None or remote_base[0] < stale_age):
            stale_age, stale_base = remote_base
            self._cache[ticker] = (self._now() - stale_age, stale_base)
        if stale_base is not None and stale_age is not None and stale_age < self._statement_ttl:
            return stale_base

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
                return refreshed

        # Durable store before provider (ADR-006 read order: L1 -> Redis ->
        # DB -> FMP). An existing DB ticker is served as stored — customer
        # traffic never refreshes it from FMP; only a genuinely confirmed
        # miss may fall through to the one-time provider bootstrap below.
        if self._snapshots is not None:
            from_store = await self._read_snapshot_store(ticker, stale_base, stale_age, lock_token)
            if from_store is not None:
                return from_store

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

            raw = await self._client.fetch_fundamentals(ticker, profile_override=profile_override)
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
                return stale_with_warning
            if lock_token is not None:
                await self._release_distributed_lock(ticker, lock_token)
            raise

        if self._snapshots is not None:
            # The durable write is awaited BEFORE any cache publication and
            # before returning (ADR-006: Redis must never advertise a dataset
            # the database did not commit; Vercel allows no post-response
            # work). Failure fails the bootstrap — nothing is published.
            try:
                await self._persist_snapshot(ticker, raw, normalized, "bootstrap_snapshot")
            except Exception as exc:
                if lock_token is not None:
                    await self._release_distributed_lock(ticker, lock_token)
                raise SnapshotStoreError(ticker) from exc

        self._cache[ticker] = (self._now(), normalized)
        self._raw_cache[ticker] = (self._now(), raw)
        self._profile_cache[ticker] = (self._now(), raw.profile)
        await self._remote_set("profile", ticker, raw.profile, self._profile_ttl)
        # Publish `fund:` last: lock losers poll it as the commit marker and
        # must not observe the snapshot before its companion caches exist.
        await self._remote_set(
            "fund", ticker, _base_to_payload(normalized), self._max_statement_staleness
        )
        if lock_token is not None:
            await self._release_distributed_lock(ticker, lock_token)
        return normalized

    async def _persist_snapshot(
        self,
        ticker: str,
        raw: FMPFundamentals,
        normalized: BaseFinancials,
        refresh_status: str,
    ) -> None:
        assert self._snapshots is not None
        await self._snapshots.store_ticker_snapshot(
            ticker=ticker,
            snapshot_version=snapshot_fingerprint(normalized),
            snapshot=_snapshot_document(normalized, raw.profile),
            provider=normalized.data_provider,
            fiscal_year=_fiscal_year_or_none(normalized.fiscal_year),
            statement_date=_iso_date_or_none(normalized.fundamentals_as_of),
            currency=normalized.currency,
            refresh_status=refresh_status,
        )

    async def refresh_from_provider(
        self, ticker: str, *, refresh_status: str = "current_as_of_daily_refresh"
    ) -> BaseFinancials:
        """Scheduled-refresh path (ADR-007): one full provider cycle for a
        ticker in the daily manifest — fetch, normalize, durably store, then
        replace the caches (`fund:` last, as always). Never called from
        customer requests; any failure propagates so the caller can fail the
        ticker's claim while the prior head/caches stay active."""
        if self._snapshots is None:
            raise RuntimeError("refresh_from_provider requires the durable snapshot store")
        ticker = ticker.upper()
        raw = await self._client.fetch_fundamentals(ticker)
        normalized = normalize_fmp_fundamentals(raw)
        await self._persist_snapshot(ticker, raw, normalized, refresh_status)
        self._cache[ticker] = (self._now(), normalized)
        self._raw_cache[ticker] = (self._now(), raw)
        self._profile_cache[ticker] = (self._now(), raw.profile)
        await self._remote_set("profile", ticker, raw.profile, self._profile_ttl)
        await self._remote_set(
            "fund", ticker, _base_to_payload(normalized), self._max_statement_staleness
        )
        return normalized

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
            self._negative.clear()
            self._inflight.clear()
        else:
            key = ticker.upper()
            self._cache.pop(key, None)
            self._raw_cache.pop(key, None)
            self._profile_cache.pop(key, None)
            self._negative.pop(key, None)
            self._inflight.pop(key, None)
