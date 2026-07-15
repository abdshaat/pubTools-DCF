"""Daily request limiters.

`DailyRequestLimiter` is the small in-process counter: on Vercel it is only a
warm-instance guard. The API-key valuation quota outgrew it in Phase 5
(Supabase RPC, durable and atomic). `RedisLoginRateLimiter` (Phase 8 Slice B)
gives the per-IP login limiter real cross-instance enforcement via Redis,
falling back to the in-process limiter whenever Redis is unavailable —
abuse control degrades, logins keep working.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .redis_cache import REDIS_KEY_PREFIX, RedisBackend


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_epoch: int
    retry_after: int


class DailyRequestLimiter:
    def __init__(
        self,
        limit: int = 100,
        *,
        now: Callable[[], float] = time.time,
    ):
        if limit < 1:
            raise ValueError("daily rate limit must be at least 1")
        self._limit = limit
        self._now = now
        self._counts: dict[tuple[str, str], int] = {}

    @staticmethod
    def _bucket_for(epoch_seconds: float) -> str:
        return datetime.fromtimestamp(epoch_seconds, tz=UTC).date().isoformat()

    @staticmethod
    def _reset_epoch_for(epoch_seconds: float) -> int:
        current = datetime.fromtimestamp(epoch_seconds, tz=UTC)
        tomorrow = current.date() + timedelta(days=1)
        reset = datetime.combine(tomorrow, datetime.min.time(), tzinfo=UTC)
        return int(reset.timestamp())

    def _prepare(self, limit: int | None) -> tuple[float, str, int, int, int]:
        """Common setup for peek/consume: returns
        (now, bucket, effective_limit, reset_epoch, retry_after) and prunes
        counters from prior days so the dict never grows unbounded."""
        now = self._now()
        bucket = self._bucket_for(now)
        effective_limit = limit or self._limit
        if effective_limit < 1:
            raise ValueError("daily rate limit must be at least 1")

        for key in list(self._counts):
            if key[1] != bucket:
                del self._counts[key]

        reset_epoch = self._reset_epoch_for(now)
        retry_after = max(1, reset_epoch - int(now))
        return now, bucket, effective_limit, reset_epoch, retry_after

    def peek(
        self,
        *,
        identity: str = "anonymous",
        limit: int | None = None,
    ) -> RateLimitResult:
        """Report current quota state WITHOUT consuming a request. Used as the
        pre-flight gate (Phase 7): reject an already-over-limit caller before
        any fetch/compute, while leaving the actual increment for after a
        response is confirmed to be a fresh 200 (a 304 must stay free)."""
        _, bucket, effective_limit, reset_epoch, retry_after = self._prepare(limit)
        count = self._counts.get((identity, bucket), 0)
        return RateLimitResult(
            allowed=count < effective_limit,
            limit=effective_limit,
            remaining=max(effective_limit - count, 0),
            reset_epoch=reset_epoch,
            retry_after=retry_after,
        )

    def check_and_increment(
        self,
        *,
        identity: str = "anonymous",
        limit: int | None = None,
    ) -> RateLimitResult:
        _, bucket, effective_limit, reset_epoch, retry_after = self._prepare(limit)
        key = (identity, bucket)
        count = self._counts.get(key, 0)
        if count >= effective_limit:
            return RateLimitResult(
                allowed=False,
                limit=effective_limit,
                remaining=0,
                reset_epoch=reset_epoch,
                retry_after=retry_after,
            )

        count += 1
        self._counts[key] = count
        return RateLimitResult(
            allowed=True,
            limit=effective_limit,
            remaining=effective_limit - count,
            reset_epoch=reset_epoch,
            retry_after=retry_after,
        )


class RedisLoginRateLimiter:
    """Cross-instance daily counter for login attempts (Phase 8 Slice B).

    One `INCR` + `EXPIRE` per attempt against `dcf:v1:login:{identity}:{date}`.
    The key embeds the UTC date, so unconditionally refreshing the TTL on
    every increment is safe (the key is never consulted after its day ends)
    and removes the classic "INCR succeeded but EXPIRE never ran" leak — a
    later attempt on the same key repairs the TTL.

    Fail-open by design: any Redis failure delegates the decision to the
    in-process fallback limiter, so a Redis outage can never block sign-in.
    """

    # Keys outlive their day by this much so a clock-skewed instance can't
    # expire a counter another instance still considers current.
    _TTL_SLACK_SECONDS = 60

    def __init__(
        self,
        backend: RedisBackend,
        *,
        limit: int,
        fallback: DailyRequestLimiter | None = None,
        now: Callable[[], float] = time.time,
    ):
        if limit < 1:
            raise ValueError("daily rate limit must be at least 1")
        self._backend = backend
        self._limit = limit
        self._fallback = fallback or DailyRequestLimiter(limit, now=now)
        self._now = now

    async def check_and_increment(
        self,
        *,
        identity: str = "anonymous",
        limit: int | None = None,
    ) -> RateLimitResult:
        effective_limit = limit or self._limit
        if effective_limit < 1:
            raise ValueError("daily rate limit must be at least 1")
        now = self._now()
        day = datetime.fromtimestamp(now, tz=UTC).date()
        reset = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        reset_epoch = int(reset.timestamp())
        key = f"{REDIS_KEY_PREFIX}login:{identity}:{day.isoformat()}"
        ttl = max(1, reset_epoch - int(now)) + self._TTL_SLACK_SECONDS

        try:
            results = await self._backend.pipeline(
                [["INCR", key], ["EXPIRE", key, ttl]],
            )
            count = int(results[0])
        except Exception:
            # Same broad fail-open as the other Redis paths: abuse control
            # degrades to the per-instance limiter, sign-in keeps working.
            return self._fallback.check_and_increment(identity=identity, limit=effective_limit)

        return RateLimitResult(
            allowed=count <= effective_limit,
            limit=effective_limit,
            remaining=max(effective_limit - count, 0),
            reset_epoch=reset_epoch,
            retry_after=max(1, reset_epoch - int(now)),
        )
