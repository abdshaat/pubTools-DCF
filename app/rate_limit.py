"""Small in-process daily request limiter.

This protects the current unauthenticated valuation endpoint from casual
overuse. On Vercel it is intentionally only a warm-instance guard; Phase 5/8
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

    def check_and_increment(
        self,
        *,
        identity: str = "anonymous",
        limit: int | None = None,
    ) -> RateLimitResult:
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
