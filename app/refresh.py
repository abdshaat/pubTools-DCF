"""Daily all-database-ticker refresh (Phase 8 Slice C, ADR-007).

One complete FMP statements+profile cycle for EVERY ticker in
`ticker_snapshot_heads`, once per Eastern calendar day, during the 6 PM
Eastern hour. Vercel cron is UTC-only, so the same guarded endpoint is
scheduled at both 22:00 and 23:00 UTC; this module's window guard lets only
the invocation that lands in the 6 PM America/New_York hour proceed (22 UTC
during EDT, 23 UTC during EST) and the durable run claim in Supabase makes
duplicate deliveries no-ops. No quote is fetched or stored anywhere in this
cycle (ADR-008 — the price is live per request).

The durable (ticker, refresh_date) claims are the once-per-day provider
gate; a Redis lock is deliberately NOT part of the correctness story (the
plan allows it as extra concurrency control only — the transactional date
claim already makes a second same-day run impossible).
"""

import asyncio
import time as time_module
from datetime import UTC, datetime, time
from typing import Any, Protocol

from .fundamentals import FundamentalsService
from .refresh_window import EASTERN, REFRESH_HOUR_EASTERN, eastern_now

# Enough to move a small manifest quickly without stacking full FMP retry
# ladders; the provider client's own three-slot semaphore bounds each cycle.
_DEFAULT_TICKER_CONCURRENCY = 3


class RefreshLedger(Protocol):
    """Durable run/claim bookkeeping (Supabase in production)."""

    async def begin_refresh_run(
        self, *, refresh_date: str, scheduled_window_at: str
    ) -> dict[str, Any]: ...

    async def complete_refresh_claim(
        self, *, ticker: str, refresh_date: str, status: str, error_code: str | None
    ) -> None: ...

    async def finish_refresh_run(self, *, refresh_date: str) -> dict[str, Any]: ...


class DailyRefreshRunner:
    def __init__(
        self,
        fundamentals: FundamentalsService,
        ledger: RefreshLedger,
        *,
        wall_now: Any = time_module.time,
        ticker_concurrency: int = _DEFAULT_TICKER_CONCURRENCY,
    ):
        self._fundamentals = fundamentals
        self._ledger = ledger
        self._wall_now = wall_now
        self._concurrency = ticker_concurrency

    async def run_if_in_window(self) -> dict[str, Any]:
        """The cron endpoint's whole job: guard the Eastern hour, claim the
        Eastern date, refresh every claimed ticker, reconcile the run."""
        local = eastern_now(self._wall_now())
        if local.hour != REFRESH_HOUR_EASTERN:
            # The other UTC schedule for today (or a manual poke outside the
            # window). A successful no-op by design, not an error.
            return {
                "run": "skipped",
                "reason": "outside_refresh_window",
                "eastern_time": local.isoformat(),
            }

        refresh_date = local.date().isoformat()
        window_start = datetime.combine(local.date(), time(REFRESH_HOUR_EASTERN), tzinfo=EASTERN)
        begin = await self._ledger.begin_refresh_run(
            refresh_date=refresh_date,
            scheduled_window_at=window_start.astimezone(UTC).isoformat(),
        )
        if begin.get("already_claimed"):
            # Duplicate cron delivery (both UTC schedules can never land in
            # the window on the same day, but Vercel may re-deliver one).
            return {
                "run": "skipped",
                "reason": "already_claimed",
                "refresh_date": refresh_date,
                "status": begin.get("status"),
            }

        tickers = [str(ticker) for ticker in begin.get("tickers") or []]
        semaphore = asyncio.Semaphore(self._concurrency)

        async def refresh_one(ticker: str) -> None:
            async with semaphore:
                try:
                    await self._fundamentals.refresh_from_provider(ticker)
                except Exception as exc:
                    # Bounded/redacted: the class name only — provider error
                    # text can embed URLs and keys and belongs in no ledger.
                    error_code = type(exc).__name__[:64]
                    await self._complete_claim(ticker, refresh_date, "failed", error_code)
                else:
                    await self._complete_claim(ticker, refresh_date, "succeeded", None)

        await asyncio.gather(*(refresh_one(ticker) for ticker in tickers))
        summary = await self._ledger.finish_refresh_run(refresh_date=refresh_date)
        return {"run": "completed", "refresh_date": refresh_date, **summary}

    async def _complete_claim(
        self, ticker: str, refresh_date: str, status: str, error_code: str | None
    ) -> None:
        try:
            await self._ledger.complete_refresh_claim(
                ticker=ticker, refresh_date=refresh_date, status=status, error_code=error_code
            )
        except Exception:
            # A claim left pending is the visible, durable signal that this
            # ticker was not confirmed processed: finish_refresh_run can then
            # only end the run partial_failed. Never let one bookkeeping
            # write abort the rest of the manifest.
            return
