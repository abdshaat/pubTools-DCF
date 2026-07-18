"""Phase 8 Slice C: durable statement snapshots (Supabase read-through).

Covers the ADR-006 customer-request read order — L1 -> Redis -> database ->
FMP (cold bootstrap only) — the write-before-cache-before-return ordering, and
the fail-closed rules: a database error is never treated as a miss, so an
outage can neither 200 with silently-refetched provider data nor spike FMP
usage. All tests run against the in-memory fakes; no network.
"""

import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.exceptions import SnapshotStoreError
from app.fundamentals import FundamentalsService, snapshot_fingerprint
from app.normalization import normalize_fmp_fundamentals
from app.redis_cache import InMemoryRedisBackend, get_envelope
from app.supabase import SupabaseClient, SupabaseConfig, SupabaseError
from tests.fake_supabase import FakeSupabaseBackend
from tests.test_data_layer import fixture_transport, make_client, make_fundamentals

CONFIG = SupabaseConfig(url="https://example.supabase.co", service_role_key="service-key")


class Clock:
    """Injectable monotonic clock so cache aging is deterministic."""

    def __init__(self, start: float = 1_000.0):
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def fixture_base(ticker: str = "AAPL"):
    return normalize_fmp_fundamentals(make_fundamentals(ticker))


def snapshot_document(base, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """The stored document shape: versioned, price-free base + raw profile."""
    return {
        "v": 1,
        "base": asdict(base),
        "profile": profile
        or {"companyName": "Apple Inc.", "sector": "Technology", "currency": "USD"},
    }


def make_store(backend: FakeSupabaseBackend) -> SupabaseClient:
    return SupabaseClient(CONFIG, transport=backend.transport())


def make_service(
    client,
    backend: FakeSupabaseBackend,
    *,
    redis=None,
    clock=None,
    **kwargs,
) -> FundamentalsService:
    if clock is not None:
        kwargs["now"] = clock
    return FundamentalsService(client, redis=redis, snapshots=make_store(backend), **kwargs)


# ---------------------------------------------------------------------------
# Read path: database hit
# ---------------------------------------------------------------------------


def test_db_hit_serves_statements_without_any_provider_call():
    backend = FakeSupabaseBackend()
    seeded = fixture_base()
    backend.seed_snapshot(
        ticker="AAPL",
        snapshot=snapshot_document(seeded),
        verified_at=datetime.now(UTC).isoformat(),
    )
    call_log = []

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = make_service(client, backend)
            return await service.get_base_financials("AAPL")

    base = asyncio.run(scenario())
    assert base == seeded
    assert call_log == []  # zero FMP traffic for an existing DB ticker
    assert backend.snapshot_read_count == 1


def test_db_hit_hydrates_l1_and_redis_for_later_requests():
    backend = FakeSupabaseBackend()
    seeded = fixture_base()
    backend.seed_snapshot(
        ticker="AAPL",
        snapshot=snapshot_document(seeded),
        verified_at=datetime.now(UTC).isoformat(),
    )
    call_log = []

    async def scenario():
        redis = InMemoryRedisBackend()
        async with (
            make_client(transport=fixture_transport(call_log)) as first_client,
            make_client(transport=fixture_transport(call_log)) as second_client,
        ):
            first = make_service(first_client, backend, redis=redis)
            await first.get_base_financials("AAPL")
            reads_after_first = backend.snapshot_read_count
            # Same instance again: L1 serves it, no second DB read.
            await first.get_base_financials("AAPL")
            same_instance_reads = backend.snapshot_read_count
            # A different instance sharing Redis gets it from L2, not the DB.
            second = make_service(second_client, backend, redis=redis)
            from_l2 = await second.get_base_financials("AAPL")
            fund = await get_envelope(redis, "dcf:v1:fund:AAPL")
            profile = await get_envelope(redis, "dcf:v1:profile:AAPL")
            return reads_after_first, same_instance_reads, from_l2, fund, profile

    reads_after_first, same_instance_reads, from_l2, fund, profile = asyncio.run(scenario())
    assert reads_after_first == 1
    assert same_instance_reads == 1
    assert from_l2 == fixture_base()
    assert backend.snapshot_read_count == 1
    assert call_log == []
    assert fund is not None and fund.data["ticker"] == "AAPL"
    assert profile is not None and profile.data["sector"] == "Technology"


def test_stale_db_snapshot_served_with_pending_refresh_warning():
    backend = FakeSupabaseBackend()
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    backend.seed_snapshot(
        ticker="AAPL", snapshot=snapshot_document(fixture_base()), verified_at=old
    )
    call_log = []

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = make_service(client, backend)
            return await service.get_base_financials("AAPL")

    base = asyncio.run(scenario())
    assert call_log == []  # stale is served as stored, never refreshed by a request
    assert any("scheduled refresh pending" in warning for warning in base.data_quality_warnings)
    assert any(old[:19] in warning for warning in base.data_quality_warnings)


def test_existing_db_ticker_is_never_refreshed_by_customer_requests():
    backend = FakeSupabaseBackend()
    backend.seed_snapshot(
        ticker="AAPL",
        snapshot=snapshot_document(fixture_base()),
        verified_at=datetime.now(UTC).isoformat(),
    )
    call_log = []
    clock = Clock()

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = make_service(client, backend, clock=clock)
            await service.get_base_financials("AAPL")
            clock.advance(6 * 3600)  # L1 statement TTL (4h) has passed
            await service.get_base_financials("AAPL")

    asyncio.run(scenario())
    assert call_log == []  # both requests resolved without FMP
    assert backend.snapshot_read_count == 2  # the stale L1 fell back to the DB


# ---------------------------------------------------------------------------
# Cold bootstrap: FMP once, persist before publish
# ---------------------------------------------------------------------------


def test_cold_ticker_bootstraps_once_and_persists_snapshot_and_head():
    backend = FakeSupabaseBackend()
    call_log = []

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = make_service(client, backend)
            return await service.get_base_financials("AAPL")

    base = asyncio.run(scenario())
    assert len(call_log) == 4  # statements x3 + profile; no quote endpoint
    assert len(backend.snapshot_store_calls) == 1
    stored = backend.snapshot_store_calls[0]
    assert stored["p_ticker"] == "AAPL"
    assert stored["p_snapshot_version"] == snapshot_fingerprint(base)
    assert stored["p_refresh_status"] == "bootstrap_snapshot"
    assert stored["p_snapshot"]["base"]["revenue"] == base.revenue
    assert "AAPL" in backend.snapshot_heads

    # A brand-new instance now reads it back from the DB with zero FMP calls.
    async def second_scenario():
        second_log = []
        async with make_client(transport=fixture_transport(second_log)) as client:
            service = make_service(client, backend)
            return await service.get_base_financials("AAPL"), second_log

    from_db, second_log = asyncio.run(second_scenario())
    assert from_db == base
    assert second_log == []


def test_bootstrap_awaits_db_write_before_redis_publish():
    backend = FakeSupabaseBackend()
    events: list[str] = []

    class RecordingStore:
        def __init__(self, inner):
            self._inner = inner

        async def get_ticker_snapshot(self, ticker):
            events.append("db:get")
            return await self._inner.get_ticker_snapshot(ticker)

        async def store_ticker_snapshot(self, **kwargs):
            events.append("db:store")
            return await self._inner.store_ticker_snapshot(**kwargs)

    async def scenario():
        redis = InMemoryRedisBackend()
        original_set = redis.set

        async def logged_set(key, value, **kwargs):
            events.append(f"redis:set:{key}")
            return await original_set(key, value, **kwargs)

        redis.set = logged_set  # type: ignore[method-assign]
        async with make_client(transport=fixture_transport()) as client:
            service = FundamentalsService(
                client, redis=redis, snapshots=RecordingStore(make_store(backend))
            )
            await service.get_base_financials("AAPL")

    asyncio.run(scenario())
    cache_publishes = [e for e in events if e.startswith("redis:set:dcf:v1:fund")]
    assert cache_publishes, "bootstrap must publish the fund: commit marker"
    assert events.index("db:store") < events.index(cache_publishes[0])
    # fund: stays the LAST cache write (the distributed commit marker).
    assert events[-1] == cache_publishes[-1]


def test_bootstrap_store_failure_fails_closed_and_publishes_nothing():
    backend = FakeSupabaseBackend()
    backend.fail_snapshot_writes = True
    call_log = []

    async def scenario():
        redis = InMemoryRedisBackend()
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = make_service(client, backend, redis=redis)
            with pytest.raises(SnapshotStoreError):
                await service.get_base_financials("AAPL")
            fund = await get_envelope(redis, "dcf:v1:fund:AAPL")
            # Recovery: once the store is healthy the next request re-runs the
            # bootstrap (the failure cached nothing, positively or negatively).
            backend.fail_snapshot_writes = False
            recovered = await service.get_base_financials("AAPL")
            return fund, recovered

    fund, recovered = asyncio.run(scenario())
    assert fund is None  # Redis never advertised the uncommitted dataset
    assert backend.snapshot_heads  # the retry persisted it
    assert recovered.ticker == "AAPL"
    assert len(call_log) == 8  # two full provider loads: failed try + retry


# ---------------------------------------------------------------------------
# Fail-closed: a store error is not a miss
# ---------------------------------------------------------------------------


def test_read_error_with_cold_caches_fails_closed_without_provider_call():
    backend = FakeSupabaseBackend()
    backend.fail_snapshot_reads = True
    call_log = []

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = make_service(client, backend)
            await service.get_base_financials("AAPL")

    with pytest.raises(SnapshotStoreError):
        asyncio.run(scenario())
    assert call_log == []  # an outage must never fall through to FMP


def test_read_error_serves_bounded_stale_cache_copy():
    backend = FakeSupabaseBackend()
    call_log = []
    clock = Clock()

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = make_service(client, backend, clock=clock)
            await service.get_base_financials("AAPL")  # bootstrap, healthy
            calls_after_bootstrap = len(call_log)
            clock.advance(6 * 3600)  # past the 4h statement TTL, inside 24h bound
            backend.fail_snapshot_reads = True
            stale = await service.get_base_financials("AAPL")
            return calls_after_bootstrap, stale

    calls_after_bootstrap, stale = asyncio.run(scenario())
    assert calls_after_bootstrap == 4
    assert len(call_log) == 4  # the degraded request made no provider calls
    assert any("snapshot store unavailable" in w.lower() for w in stale.data_quality_warnings)


# ---------------------------------------------------------------------------
# Corrupt rows: repaired by bootstrap, never served
# ---------------------------------------------------------------------------


def test_malformed_db_document_is_repaired_by_bootstrap():
    backend = FakeSupabaseBackend()
    backend.seed_snapshot(
        ticker="AAPL",
        snapshot={"v": 1, "base": {"nonsense": True}},
        verified_at=datetime.now(UTC).isoformat(),
    )
    call_log = []

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = make_service(client, backend)
            return await service.get_base_financials("AAPL")

    base = asyncio.run(scenario())
    assert base.revenue > 0  # served from the fresh provider load
    assert len(call_log) == 4
    assert len(backend.snapshot_store_calls) == 1  # head repaired durably


def test_wrong_ticker_document_is_treated_as_miss():
    backend = FakeSupabaseBackend()
    backend.seed_snapshot(
        ticker="AAPL",
        snapshot=snapshot_document(replace(fixture_base(), ticker="MSFT")),
        verified_at=datetime.now(UTC).isoformat(),
    )
    call_log = []

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = make_service(client, backend)
            return await service.get_base_financials("AAPL")

    base = asyncio.run(scenario())
    assert base.ticker == "AAPL"
    assert len(call_log) == 4  # bootstrapped instead of serving MSFT's numbers


def test_future_dated_head_is_treated_as_corrupt_and_repaired():
    backend = FakeSupabaseBackend()
    backend.seed_snapshot(
        ticker="AAPL",
        snapshot=snapshot_document(fixture_base()),
        verified_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    call_log = []

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = make_service(client, backend)
            return await service.get_base_financials("AAPL")

    base = asyncio.run(scenario())
    assert base.ticker == "AAPL"
    assert len(call_log) == 4
    assert len(backend.snapshot_store_calls) == 1


# ---------------------------------------------------------------------------
# Fingerprint + SupabaseClient boundary
# ---------------------------------------------------------------------------


def test_snapshot_fingerprint_is_stable_and_content_addressed():
    base = fixture_base()
    assert snapshot_fingerprint(base).startswith("sha256:")
    assert snapshot_fingerprint(base) == snapshot_fingerprint(fixture_base())
    assert snapshot_fingerprint(base) != snapshot_fingerprint(replace(base, revenue=1.0))


def test_get_ticker_snapshot_returns_none_for_missing_head():
    backend = FakeSupabaseBackend()

    async def scenario():
        return await make_store(backend).get_ticker_snapshot("AAPL")

    assert asyncio.run(scenario()) is None


def test_get_ticker_snapshot_parses_typed_record():
    backend = FakeSupabaseBackend()
    document = snapshot_document(fixture_base())
    backend.seed_snapshot(
        ticker="AAPL",
        snapshot=document,
        snapshot_version="sha256:abc",
        verified_at="2026-07-18T12:00:00+00:00",
        refresh_status="bootstrap_snapshot",
    )

    async def scenario():
        return await make_store(backend).get_ticker_snapshot("aapl")

    record = asyncio.run(scenario())
    assert record is not None
    assert record.ticker == "AAPL"
    assert record.snapshot_version == "sha256:abc"
    assert record.verified_at == datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    assert record.refresh_status == "bootstrap_snapshot"
    # The HTTP round trip turns the warnings tuple into a JSON list, so
    # compare against the JSON-normalized form of the same document.
    assert record.snapshot == json.loads(json.dumps(document))


def test_get_ticker_snapshot_raises_on_http_error():
    backend = FakeSupabaseBackend()
    backend.fail_snapshot_reads = True

    async def scenario():
        await make_store(backend).get_ticker_snapshot("AAPL")

    with pytest.raises(SupabaseError):
        asyncio.run(scenario())


def test_get_ticker_snapshot_treats_malformed_head_row_as_miss():
    backend = FakeSupabaseBackend()
    backend.seed_snapshot(ticker="AAPL", snapshot=snapshot_document(fixture_base()))
    backend.snapshot_heads["AAPL"]["verified_at"] = "not-a-timestamp"

    async def scenario():
        return await make_store(backend).get_ticker_snapshot("AAPL")

    assert asyncio.run(scenario()) is None


def test_store_ticker_snapshot_raises_on_http_error():
    backend = FakeSupabaseBackend()
    backend.fail_snapshot_writes = True

    async def scenario():
        await make_store(backend).store_ticker_snapshot(
            ticker="AAPL",
            snapshot_version="sha256:abc",
            snapshot={"v": 1, "base": {}, "profile": None},
            provider="financialmodelingprep",
            fiscal_year=2025,
            statement_date="2025-09-27",
            currency="USD",
            refresh_status="bootstrap_snapshot",
        )

    with pytest.raises(SupabaseError):
        asyncio.run(scenario())


def test_immutable_snapshot_rows_deduplicate_on_reconfirmation():
    backend = FakeSupabaseBackend()

    async def scenario():
        store = make_store(backend)
        base = fixture_base()
        for _ in range(2):  # the daily job re-confirming an identical filing
            await store.store_ticker_snapshot(
                ticker="AAPL",
                snapshot_version=snapshot_fingerprint(base),
                snapshot=snapshot_document(base),
                provider=base.data_provider,
                fiscal_year=2025,
                statement_date="2025-09-27",
                currency="USD",
                refresh_status="current_as_of_daily_refresh",
            )

    asyncio.run(scenario())
    assert len(backend.snapshots) == 1  # ON CONFLICT DO NOTHING kept one row
    assert len(backend.snapshot_store_calls) == 2


# ---------------------------------------------------------------------------
# Route mapping
# ---------------------------------------------------------------------------


def test_snapshot_store_outage_maps_to_controlled_503():
    class FailingStore:
        async def get_ticker_snapshot(self, ticker):
            raise RuntimeError("database unavailable")

        async def store_ticker_snapshot(self, **kwargs):
            raise RuntimeError("database unavailable")

    app = create_app(fmp_client=make_client(), snapshot_store=FailingStore())
    with TestClient(app) as http:
        response = http.get(
            "/v1/valuations/AAPL",
            params={
                "wacc": 0.09,
                "terminal_growth": 0.025,
                "ebit_margin": 0.30,
                "revenue_growth": "0.05",
            },
        )
    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "snapshot_store_unavailable"
    assert response.headers["Cache-Control"] == "no-store"
