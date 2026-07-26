"""Readiness: can this instance serve traffic right now?

Phase 11 Slice 3. Liveness (`/health`) answers "is the process up" and touches
nothing. Readiness answers "are the dependencies this instance needs actually
reachable", which is a different question with a different failure mode: an
instance can be perfectly alive and unable to authenticate a single request.

Three rules shape it:

- **Required is not the same as configured.** Supabase is fail-closed (auth,
  quota, metering, durable snapshots), so if it is configured and unreachable
  this instance is not ready. Redis is an accelerator whose loss costs latency,
  not correctness (ADR-004's fail-open matrix), so an outage is reported as
  degraded and the instance stays ready.
- **A probe never spends a provider call.** FMP and Finnhub are metered
  external budgets — FMP daily, Finnhub 60/min — and a readiness endpoint is
  polled. Their entries report configuration only, never a live call. Anything
  else would let a monitoring system exhaust the customer-facing quota.
- **A probe storm cannot amplify.** Results are cached for a few seconds and
  concurrent checks collapse onto one in-flight probe, so N pollers cost at
  most one round trip per window.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

DEFAULT_READINESS_CACHE_SECONDS = 5.0

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NOT_CONFIGURED = "not_configured"

# Well-known key the Redis probe reads. It is never written: a successful
# "missing key" reply proves reachability just as well as a hit.
_REDIS_PROBE_KEY = "dcf:v1:readiness"


class SupabaseProbe(Protocol):
    async def ping(self) -> None: ...


class RedisProbe(Protocol):
    async def get(self, key: str) -> str | None: ...


@dataclass(frozen=True)
class DependencyStatus:
    name: str
    status: str
    required: bool

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "required": self.required}


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    checked_at: datetime
    dependencies: tuple[DependencyStatus, ...]
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checked_at": self.checked_at.isoformat(),
            "cached": self.cached,
            "dependencies": [dependency.to_dict() for dependency in self.dependencies],
        }

    def as_cached(self) -> "ReadinessReport":
        return ReadinessReport(
            ready=self.ready,
            checked_at=self.checked_at,
            dependencies=self.dependencies,
            cached=True,
        )


class ReadinessChecker:
    """Probes the dependencies whose absence changes what this instance can do.

    Deliberately reports names and statuses only — no URLs, hostnames, or
    exception text. The endpoint is unauthenticated so it can be polled by a
    platform health check, and a probe reply should not describe the
    infrastructure to whoever asks.
    """

    def __init__(
        self,
        *,
        supabase: SupabaseProbe | None = None,
        redis: RedisProbe | None = None,
        price_configured: bool = False,
        cache_seconds: float = DEFAULT_READINESS_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if cache_seconds < 0:
            raise ValueError("cache_seconds must be non-negative")
        self._supabase = supabase
        self._redis = redis
        self._price_configured = price_configured
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = asyncio.Lock()
        self._cached: ReadinessReport | None = None
        self._cached_at: float | None = None

    async def check(self) -> ReadinessReport:
        cached = self._fresh_cached()
        if cached is not None:
            return cached
        async with self._lock:
            # A storm of pollers queues here; whoever wins refreshes and the
            # rest read that result instead of re-probing.
            cached = self._fresh_cached()
            if cached is not None:
                return cached
            report = await self._probe()
            self._cached = report
            self._cached_at = self._clock()
            return report

    def _fresh_cached(self) -> ReadinessReport | None:
        if self._cached is None or self._cached_at is None:
            return None
        if self._clock() - self._cached_at >= self._cache_seconds:
            return None
        return self._cached.as_cached()

    async def _probe(self) -> ReadinessReport:
        dependencies = [
            await self._check_supabase(),
            await self._check_redis(),
            # Provider entries are configuration facts, not live checks: see
            # the module docstring on why a polled endpoint must not spend a
            # metered provider call.
            DependencyStatus(
                name="finnhub_price",
                status=STATUS_OK if self._price_configured else STATUS_NOT_CONFIGURED,
                required=False,
            ),
        ]
        ready = all(
            dependency.status != STATUS_UNAVAILABLE
            for dependency in dependencies
            if dependency.required
        )
        return ReadinessReport(
            ready=ready,
            checked_at=self._wall_clock(),
            dependencies=tuple(dependencies),
        )

    async def _check_supabase(self) -> DependencyStatus:
        if self._supabase is None:
            # Auth, quota, and the durable store are all off in this mode; the
            # instance can still serve unauthenticated valuations, so it is
            # ready — just running a smaller product.
            return DependencyStatus("supabase", STATUS_NOT_CONFIGURED, required=False)
        try:
            await self._supabase.ping()
        except Exception:
            return DependencyStatus("supabase", STATUS_UNAVAILABLE, required=True)
        return DependencyStatus("supabase", STATUS_OK, required=True)

    async def _check_redis(self) -> DependencyStatus:
        if self._redis is None:
            return DependencyStatus("redis", STATUS_NOT_CONFIGURED, required=False)
        try:
            await self._redis.get(_REDIS_PROBE_KEY)
        except Exception:
            # Fail-open by design: valuations still work, they just cost more
            # provider calls and lose cross-instance coordination.
            return DependencyStatus("redis", STATUS_DEGRADED, required=False)
        return DependencyStatus("redis", STATUS_OK, required=False)
