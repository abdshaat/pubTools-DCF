"""Small in-process daily request limiter.

This protects the current unauthenticated valuation endpoint from casual
overuse. On Vercel it is intentionally only a warm-instance guard; Phase 5/7
will replace it with Redis/Postgres-backed counters for a strict global quota.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


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
        self._bucket = self._bucket_for(now())
        self._count = 0

    @staticmethod
    def _bucket_for(epoch_seconds: float) -> str:
        return datetime.fromtimestamp(epoch_seconds, tz=UTC).date().isoformat()

    @staticmethod
    def _reset_epoch_for(epoch_seconds: float) -> int:
        current = datetime.fromtimestamp(epoch_seconds, tz=UTC)
        tomorrow = current.date() + timedelta(days=1)
        reset = datetime.combine(tomorrow, datetime.min.time(), tzinfo=UTC)
        return int(reset.timestamp())

    def check_and_increment(self) -> RateLimitResult:
        now = self._now()
        bucket = self._bucket_for(now)
        if bucket != self._bucket:
            self._bucket = bucket
            self._count = 0

        reset_epoch = self._reset_epoch_for(now)
        retry_after = max(1, reset_epoch - int(now))
        if self._count >= self._limit:
            return RateLimitResult(
                allowed=False,
                limit=self._limit,
                remaining=0,
                reset_epoch=reset_epoch,
                retry_after=retry_after,
            )

        self._count += 1
        return RateLimitResult(
            allowed=True,
            limit=self._limit,
            remaining=self._limit - self._count,
            reset_epoch=reset_epoch,
            retry_after=retry_after,
        )
