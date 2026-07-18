"""Phase 8 Slice C part 2: the daily 6 PM Eastern all-ticker refresh.

Covers the America/New_York window guard over both UTC cron schedules in EST
and EDT, the durable date claim (duplicate delivery is a no-op), complete
manifest processing with reconciled counts (no silent omission), per-ticker
failure isolation, and the CRON_SECRET-protected endpoint. All against the
in-memory fakes; no network.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.fundamentals import FundamentalsService
from app.redis_cache import InMemoryRedisBackend, get_envelope
from app.refresh import DailyRefreshRunner
from tests.fake_supabase import FakeSupabaseBackend
from tests.test_data_layer import fixture_transport, load_fixture, make_client
from tests.test_snapshots import CONFIG, fixture_base, make_store, snapshot_document

# 18:30 in New York on each date — inside the refresh window.
EDT_WINDOW = datetime(2026, 7, 18, 22, 30, tzinfo=UTC).timestamp()  # 22 UTC cron
EST_WINDOW = datetime(2026, 1, 15, 23, 30, tzinfo=UTC).timestamp()  # 23 UTC cron
# The same crons on the other side of the DST switch — 19:30/17:30 local.
EDT_OUTSIDE = datetime(2026, 7, 18, 23, 30, tzinfo=UTC).timestamp()
EST_OUTSIDE = datetime(2026, 1, 15, 22, 30, tzinfo=UTC).timestamp()


class LedgerStub:
    """Records ledger calls; manifest comes from the given tickers."""

    def __init__(self, tickers: tuple[str, ...] = ()):
        self.tickers = list(tickers)
        self.begin_calls: list[tuple[str, str]] = []
        self.claims: list[tuple[str, str, str, str | None]] = []
        self.finish_calls: list[str] = []

    async def begin_refresh_run(self, *, refresh_date: str, scheduled_window_at: str) -> dict:
        self.begin_calls.append((refresh_date, scheduled_window_at))
        return {
            "already_claimed": False,
            "status": "running",
            "total_tickers": len(self.tickers),
            "tickers": self.tickers,
        }

    async def complete_refresh_claim(
        self, *, ticker: str, refresh_date: str, status: str, error_code: str | None
    ) -> None:
        self.claims.append((ticker, refresh_date, status, error_code))

    async def finish_refresh_run(self, *, refresh_date: str) -> dict:
        self.finish_calls.append(refresh_date)
        succeeded = sum(1 for claim in self.claims if claim[2] == "succeeded")
        failed = sum(1 for claim in self.claims if claim[2] == "failed")
        return {
            "status": "succeeded" if failed == 0 else "partial_failed",
            "total": len(self.tickers),
            "succeeded": succeeded,
            "failed": failed,
            "pending": len(self.tickers) - succeeded - failed,
        }


class FundamentalsStub:
    def __init__(self) -> None:
        self.refreshed: list[str] = []

    async def refresh_from_provider(self, ticker: str, **kwargs: Any) -> None:
        self.refreshed.append(ticker)


def any_symbol_transport(call_log: list | None = None) -> httpx.MockTransport:
    """Serves the AAPL fixture payloads for every symbol, so multi-ticker
    manifests can succeed without per-ticker fixture files."""

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if call_log is not None:
            call_log.append((endpoint, request.url.params.get("symbol", "")))
        return httpx.Response(200, json=load_fixture("AAPL")[endpoint])

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Eastern window guard (the dual-UTC-cron contract)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wall", "expected_date", "expected_window_utc"),
    [
        (EDT_WINDOW, "2026-07-18", "2026-07-18T22:00:00+00:00"),
        (EST_WINDOW, "2026-01-15", "2026-01-15T23:00:00+00:00"),
    ],
)
def test_invocation_inside_the_6pm_eastern_hour_runs(wall, expected_date, expected_window_utc):
    ledger = LedgerStub()
    runner = DailyRefreshRunner(FundamentalsStub(), ledger, wall_now=lambda: wall)  # type: ignore[arg-type]

    result = asyncio.run(runner.run_if_in_window())

    assert result["run"] == "completed"
    assert result["refresh_date"] == expected_date
    assert ledger.begin_calls == [(expected_date, expected_window_utc)]
    assert ledger.finish_calls == [expected_date]


@pytest.mark.parametrize("wall", [EDT_OUTSIDE, EST_OUTSIDE])
def test_invocation_outside_the_6pm_eastern_hour_is_a_noop(wall):
    ledger = LedgerStub(("AAPL",))
    fundamentals = FundamentalsStub()
    runner = DailyRefreshRunner(fundamentals, ledger, wall_now=lambda: wall)  # type: ignore[arg-type]

    result = asyncio.run(runner.run_if_in_window())

    assert result == {
        "run": "skipped",
        "reason": "outside_refresh_window",
        "eastern_time": result["eastern_time"],
    }
    assert ledger.begin_calls == []  # no run row, no claims, no provider calls
    assert fundamentals.refreshed == []


# ---------------------------------------------------------------------------
# Full runs against the Supabase fake (durable claims + reconciliation)
# ---------------------------------------------------------------------------


def _seed_head(backend: FakeSupabaseBackend, ticker: str) -> None:
    backend.seed_snapshot(
        ticker=ticker,
        snapshot=snapshot_document(replace(fixture_base(), ticker=ticker)),
        snapshot_version=f"sha256:seeded-{ticker}",
        verified_at="2026-07-17T00:00:00+00:00",
    )


def test_full_run_refreshes_every_db_ticker_and_duplicate_delivery_is_noop():
    backend = FakeSupabaseBackend()
    _seed_head(backend, "AAPL")
    _seed_head(backend, "MSFT")
    call_log: list = []

    async def scenario():
        redis = InMemoryRedisBackend()
        async with make_client(transport=any_symbol_transport(call_log)) as client:
            store = make_store(backend)
            service = FundamentalsService(client, redis=redis, snapshots=store)
            runner = DailyRefreshRunner(service, store, wall_now=lambda: EDT_WINDOW)
            first = await runner.run_if_in_window()
            calls_after_first = len(call_log)
            second = await runner.run_if_in_window()  # duplicate cron delivery
            fund = await get_envelope(redis, "dcf:v1:fund:MSFT")
            return first, second, calls_after_first, fund

    first, second, calls_after_first, fund = asyncio.run(scenario())

    assert first["run"] == "completed"
    assert (first["status"], first["total"], first["succeeded"], first["failed"]) == (
        "succeeded",
        2,
        2,
        0,
    )
    assert calls_after_first == 8  # 4 FMP endpoints per ticker, no quote
    assert second == {
        "run": "skipped",
        "reason": "already_claimed",
        "refresh_date": "2026-07-18",
        "status": "succeeded",
    }
    assert len(call_log) == 8  # the duplicate spent zero provider calls

    run = backend.refresh_runs["2026-07-18"]
    assert (run["status"], run["total_tickers"], run["succeeded_tickers"]) == ("succeeded", 2, 2)
    for ticker in ("AAPL", "MSFT"):
        claim = backend.refresh_claims[(ticker, "2026-07-18")]
        assert claim["status"] == "succeeded"
        assert claim["completed_at"] is not None
        assert backend.snapshot_heads[ticker]["refresh_status"] == "current_as_of_daily_refresh"
    assert fund is not None and fund.data["ticker"] == "MSFT"


def test_one_failing_ticker_fails_its_claim_but_not_the_run():
    backend = FakeSupabaseBackend()
    _seed_head(backend, "AAPL")
    _seed_head(backend, "GONE")  # no fixture -> provider answers [] -> not found
    call_log: list = []

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            store = make_store(backend)
            service = FundamentalsService(client, snapshots=store)
            runner = DailyRefreshRunner(service, store, wall_now=lambda: EDT_WINDOW)
            return await runner.run_if_in_window()

    result = asyncio.run(scenario())

    assert (result["status"], result["succeeded"], result["failed"], result["pending"]) == (
        "partial_failed",
        1,
        1,
        0,  # every manifest ticker ended with an explicit outcome
    )
    assert backend.refresh_claims[("GONE", "2026-07-18")]["status"] == "failed"
    assert backend.refresh_claims[("GONE", "2026-07-18")]["error_code"] == "TickerNotFoundError"
    assert backend.refresh_claims[("AAPL", "2026-07-18")]["status"] == "succeeded"
    # The failed ticker's prior head stays active and untouched.
    assert backend.snapshot_heads["GONE"]["refresh_status"] == "bootstrap_snapshot"
    assert backend.snapshot_heads["AAPL"]["refresh_status"] == "current_as_of_daily_refresh"


def test_empty_manifest_run_completes_trivially():
    backend = FakeSupabaseBackend()

    async def scenario():
        async with make_client(transport=fixture_transport()) as client:
            store = make_store(backend)
            service = FundamentalsService(client, snapshots=store)
            runner = DailyRefreshRunner(service, store, wall_now=lambda: EDT_WINDOW)
            return await runner.run_if_in_window()

    result = asyncio.run(scenario())
    assert result["run"] == "completed"
    assert (result["status"], result["total"]) == ("succeeded", 0)


# ---------------------------------------------------------------------------
# The per-ticker refresh path on the service
# ---------------------------------------------------------------------------


def test_refresh_from_provider_stores_status_and_replaces_caches():
    backend = FakeSupabaseBackend()
    _seed_head(backend, "AAPL")
    call_log: list = []

    async def scenario():
        redis = InMemoryRedisBackend()
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = FundamentalsService(client, redis=redis, snapshots=make_store(backend))
            refreshed = await service.refresh_from_provider("aapl")
            calls_after_refresh = len(call_log)
            # The refreshed data is now the warm-instance L1 copy.
            served = await service.get_base_financials("AAPL")
            return refreshed, served, calls_after_refresh

    refreshed, served, calls_after_refresh = asyncio.run(scenario())

    assert refreshed == served
    assert calls_after_refresh == 4
    assert len(call_log) == 4  # the follow-up request hit L1, not FMP
    stored = backend.snapshot_store_calls[-1]
    assert stored["p_refresh_status"] == "current_as_of_daily_refresh"
    assert backend.snapshot_heads["AAPL"]["refresh_status"] == "current_as_of_daily_refresh"


def test_refresh_from_provider_requires_the_snapshot_store():
    async def scenario():
        async with make_client(transport=fixture_transport()) as client:
            service = FundamentalsService(client)  # no snapshots configured
            await service.refresh_from_provider("AAPL")

    with pytest.raises(RuntimeError, match="durable snapshot store"):
        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# The CRON_SECRET-protected endpoint
# ---------------------------------------------------------------------------


class RunnerStub:
    def __init__(self, result: dict[str, Any]):
        self.result = result
        self.calls = 0

    async def run_if_in_window(self) -> dict[str, Any]:
        self.calls += 1
        return self.result


def test_cron_endpoint_rejects_when_no_secret_is_configured():
    app = create_app(fmp_client=make_client(), refresh_runner=RunnerStub({"run": "completed"}))
    with TestClient(app) as http:
        response = http.get(
            "/internal/cron/refresh-financials",
            headers={"Authorization": "Bearer anything"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "cron_unauthorized"


def test_cron_endpoint_rejects_a_wrong_or_missing_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "correct-horse-battery-staple")
    runner = RunnerStub({"run": "completed"})
    app = create_app(fmp_client=make_client(), refresh_runner=runner)
    with TestClient(app) as http:
        missing = http.get("/internal/cron/refresh-financials")
        wrong = http.get(
            "/internal/cron/refresh-financials",
            headers={"Authorization": "Bearer wrong"},
        )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert runner.calls == 0


def test_cron_endpoint_runs_with_the_correct_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "correct-horse-battery-staple")
    runner = RunnerStub({"run": "skipped", "reason": "outside_refresh_window"})
    app = create_app(fmp_client=make_client(), refresh_runner=runner)
    with TestClient(app) as http:
        response = http.get(
            "/internal/cron/refresh-financials",
            headers={"Authorization": "Bearer correct-horse-battery-staple"},
        )
    assert response.status_code == 200
    assert response.json() == {"run": "skipped", "reason": "outside_refresh_window"}
    assert response.headers["Cache-Control"] == "no-store"
    assert runner.calls == 1


def test_cron_endpoint_503_when_refresh_is_unconfigured(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "correct-horse-battery-staple")
    app = create_app(fmp_client=make_client())  # no Supabase -> no runner
    with TestClient(app) as http:
        response = http.get(
            "/internal/cron/refresh-financials",
            headers={"Authorization": "Bearer correct-horse-battery-staple"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "refresh_not_configured"


def test_cron_endpoint_end_to_end_with_supabase_wired(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "correct-horse-battery-staple")
    backend = FakeSupabaseBackend()
    _seed_head(backend, "AAPL")
    from app.supabase import SupabaseClient

    supabase = SupabaseClient(CONFIG, transport=backend.transport())
    app = create_app(
        fmp_client=make_client(),
        supabase_client=supabase,
        authenticator=None,
    )
    with TestClient(app) as http:
        # The lifespan built the real runner off the Supabase client; pin its
        # clock inside the 6 PM EDT hour so the guard opens.
        app.state.refresh_runner._wall_now = lambda: EDT_WINDOW
        response = http.get(
            "/internal/cron/refresh-financials",
            headers={"Authorization": "Bearer correct-horse-battery-staple"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["run"] == "completed"
    assert (payload["status"], payload["total"], payload["succeeded"]) == ("succeeded", 1, 1)
    assert backend.refresh_runs["2026-07-18"]["status"] == "succeeded"
