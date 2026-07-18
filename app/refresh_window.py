"""The daily 6 PM America/New_York refresh window (ADR-007).

Shared by the scheduler (`app/refresh.py`) and the fundamentals cache
freshness rules (`app/fundamentals.py`): once a window boundary passes, no
instance may keep serving pre-boundary statement data as current — it must
fall back to the durable head (which the day's run promotes) and surface
freshness honestly. Kept in its own module because `refresh.py` imports the
service and the service needs these helpers (no cycles).
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
REFRESH_HOUR_EASTERN = 18


def eastern_now(wall_timestamp: float) -> datetime:
    return datetime.fromtimestamp(wall_timestamp, tz=UTC).astimezone(EASTERN)


def last_refresh_boundary(wall_timestamp: float) -> float:
    """Epoch of the most recent 6 PM Eastern before (or at) this instant.

    Wall-clock day arithmetic on the zoneinfo-aware value keeps the boundary
    at 18:00 *local* across EST/EDT transitions (which happen at 2 AM, hours
    away from this window).
    """
    local = eastern_now(wall_timestamp)
    boundary = local.replace(hour=REFRESH_HOUR_EASTERN, minute=0, second=0, microsecond=0)
    if local < boundary:
        boundary -= timedelta(days=1)
    return boundary.timestamp()


def next_refresh_boundary(wall_timestamp: float) -> datetime:
    """The next future 6 PM Eastern refresh-window boundary, returned in UTC."""
    local = eastern_now(wall_timestamp)
    boundary = local.replace(hour=REFRESH_HOUR_EASTERN, minute=0, second=0, microsecond=0)
    if local >= boundary:
        boundary += timedelta(days=1)
    return boundary.astimezone(UTC)
