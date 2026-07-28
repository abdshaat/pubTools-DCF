"""ADR-010: bounded-staleness valuation response cache.

Route-level tests drive real HTTP requests through TestClient apps that share
one `InMemoryRedisBackend`, simulating separate serverless instances against
one Upstash database. "Served from the cache" is proven by counts that do not
grow — `compute_dcf` invocations, Finnhub quote calls, FMP transport calls —
never inferred from timing.

The first test in this file is the important one: with the setting left at its
default the cache is **off**, and the endpoint must behave exactly as it did
before this module existed. Everything else here describes behavior that only
exists once an owner deliberately sets `VALUATION_CACHE_TTL_SECONDS`.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import app.api as api_module
import app.response_cache as response_cache_module
from app.api import create_app
from app.exceptions import ProviderError
from app.providers.fmp import FMPClient
from app.redis_cache import InMemoryRedisBackend
from app.response_cache import (
    assumption_fingerprint,
    get_cached_response,
    store_response,
    strip_per_request_fields,
)
from app.settings import Settings
from app.supabase import SupabaseAPIKeyAuthenticator, SupabaseClient
from tests.fake_supabase import FakeSupabaseBackend
from tests.test_api import FakeFinnhubClient, _seed_valuation_key, _supabase_config
from tests.test_data_layer import fixture_transport

VALID_QUERY = (
    "wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05&projection_years=5"
)


def _settings(ttl: float) -> Settings:
    return Settings.from_env().with_overrides(valuation_cache_ttl_seconds=ttl)


def _app(redis, *, ttl: float, finnhub=None, call_log: list | None = None):
    fmp = FMPClient(api_key="test-key", transport=fixture_transport(call_log))
    return create_app(
        fmp_client=fmp,
        finnhub_client=finnhub if finnhub is not None else FakeFinnhubClient(),
        redis_backend=redis,
        settings=_settings(ttl),
    )


def _count_compute(monkeypatch) -> list:
    """Wraps app.api.compute_dcf so cache misses are observable."""
    calls: list = []
    real = api_module.compute_dcf

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(api_module, "compute_dcf", counting)
    return calls


# --------------------------------------------------------------------------
# Default: OFF. This is the contract that keeps ADR-008 true out of the box.
# --------------------------------------------------------------------------


def test_the_cache_is_off_by_default_even_with_redis_configured(monkeypatch):
    """Redis present, TTL untouched: two identical requests must both compute
    against a live price, and nothing may be written to Redis."""
    calls = _count_compute(monkeypatch)
    redis = InMemoryRedisBackend()
    finnhub = FakeFinnhubClient(price=250.0)
    default_ttl = Settings.from_env().valuation_cache_ttl_seconds
    assert default_ttl == 0

    with TestClient(_app(redis, ttl=default_ttl, finnhub=finnhub)) as client:
        first = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        second = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert first.status_code == second.status_code == 200
    assert len(calls) == 2, "the DCF must be recomputed when the cache is off"
    assert finnhub.calls == 2, "the price must be live on every request"
    assert first.headers["Cache-Control"] == "no-store"
    assert second.headers["Cache-Control"] == "no-store"
    assert not [key for key in redis._values if ":resp:" in key]


def test_a_disabled_cache_never_touches_redis():
    """Off is a short-circuit, not a zero TTL: a backend that raises on every
    call must not even be consulted."""

    class ExplodingRedis:
        async def get(self, key):
            raise AssertionError("the disabled cache must not read Redis")

        async def set(self, key, value, **kwargs):
            raise AssertionError("the disabled cache must not write Redis")

        async def delete(self, key):
            raise AssertionError("the disabled cache must not delete from Redis")

        async def compare_and_delete(self, key, expected):
            raise AssertionError("unused")

        async def aclose(self):
            pass

    hit = asyncio.run(
        get_cached_response(
            ExplodingRedis(), ticker="AAPL", fingerprint="abc", ttl_seconds=0, now=100.0
        )
    )
    assert hit is None
    asyncio.run(
        store_response(
            ExplodingRedis(),
            ticker="AAPL",
            fingerprint="abc",
            content={"a": 1},
            ttl_seconds=0,
            stored_at=100.0,
        )
    )


# --------------------------------------------------------------------------
# Enabled: the behavior the owner opts into.
# --------------------------------------------------------------------------


def test_a_second_instance_serves_from_cache_without_recompute_or_price_call(monkeypatch):
    calls = _count_compute(monkeypatch)
    redis = InMemoryRedisBackend()
    first_finnhub = FakeFinnhubClient(price=250.0)
    second_finnhub = FakeFinnhubClient(price=999.0)
    first_log: list = []
    second_log: list = []

    with TestClient(_app(redis, ttl=60, finnhub=first_finnhub, call_log=first_log)) as one:
        first = one.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
    with TestClient(_app(redis, ttl=60, finnhub=second_finnhub, call_log=second_log)) as two:
        second = two.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert first.status_code == second.status_code == 200
    # The second instance did no work at all: no DCF, no quote, no FMP.
    assert len(calls) == 1
    assert first_finnhub.calls == 1
    assert second_finnhub.calls == 0
    assert second_log == []
    # It also did not serve the second instance's different price — proof the
    # body came from the cache rather than being recomputed.
    first_body, second_body = first.json(), second.json()
    assert second_body["current_price"] == first_body["current_price"] == 250.0
    # Identical content, fresh request id.
    assert first_body["request_id"] != second_body["request_id"]
    assert {k: v for k, v in first_body.items() if k != "request_id"} == {
        k: v for k, v in second_body.items() if k != "request_id"
    }


def test_a_hit_states_its_own_age_rather_than_posing_as_fresh():
    """`computed_at` and `price_fetched_at` come back as stored (ADR-010: the
    staleness is bounded *and* visible), and Cache-Control counts down."""
    redis = InMemoryRedisBackend()
    with TestClient(_app(redis, ttl=60)) as client:
        first = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        second = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert second.json()["computed_at"] == first.json()["computed_at"]
    assert second.json()["price_fetched_at"] == first.json()["price_fetched_at"]
    assert first.headers["Cache-Control"] == "private, max-age=60"
    # Same TTL window, so the remaining lifetime is at most the full TTL.
    remaining = int(second.headers["Cache-Control"].removeprefix("private, max-age="))
    assert 0 <= remaining <= 60


def test_cache_control_is_private_never_public():
    """The body is served behind an API key and carries per-caller rate-limit
    headers, so a shared cache must never store it."""
    redis = InMemoryRedisBackend()
    with TestClient(_app(redis, ttl=30)) as client:
        response = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
    assert response.headers["Cache-Control"].startswith("private,")
    assert "public" not in response.headers["Cache-Control"]


def test_equivalent_request_forms_share_one_cache_entry(monkeypatch):
    """A scalar growth rate, its per-year expansion, an explicitly-passed
    default, and a reshuffled parameter order are one request."""
    calls = _count_compute(monkeypatch)
    redis = InMemoryRedisBackend()
    forms = (
        "wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05&projection_years=5",
        "wacc=0.09&terminal_growth=0.025&ebit_margin=0.30"
        "&revenue_growth=0.05,0.05,0.05,0.05,0.05&projection_years=5",
        "projection_years=5&revenue_growth=0.05&ebit_margin=0.30"
        "&terminal_growth=0.025&wacc=0.09&tax_rate=0.21",
    )
    with TestClient(_app(redis, ttl=60)) as client:
        for form in forms:
            assert client.get(f"/v1/valuations/AAPL?{form}").status_code == 200

    assert len(calls) == 1


@pytest.mark.parametrize(
    "different_query",
    [
        "wacc=0.10&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05&projection_years=5",
        "wacc=0.09&terminal_growth=0.030&ebit_margin=0.30&revenue_growth=0.05&projection_years=5",
        "wacc=0.09&terminal_growth=0.025&ebit_margin=0.31&revenue_growth=0.05&projection_years=5",
        "wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.06&projection_years=5",
        "wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05&projection_years=6",
        "wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05"
        "&projection_years=5&tax_rate=0.25",
        "wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05"
        "&projection_years=5&sensitivity=false",
    ],
)
def test_any_different_assumption_misses_the_cache(monkeypatch, different_query):
    calls = _count_compute(monkeypatch)
    redis = InMemoryRedisBackend()
    with TestClient(_app(redis, ttl=60)) as client:
        assert client.get(f"/v1/valuations/AAPL?{VALID_QUERY}").status_code == 200
        assert client.get(f"/v1/valuations/AAPL?{different_query}").status_code == 200
    assert len(calls) == 2


def test_a_different_ticker_misses_the_cache():
    """Identical assumptions produce an identical fingerprint, so the ticker is
    the only thing keeping two companies' valuations apart. Asserted directly
    on the key rather than over HTTP — the suite ships one valuable-sector FMP
    fixture (AAPL; JPM is the unsupported-sector case), so a second ticker
    cannot be driven end to end here."""
    redis = InMemoryRedisBackend()
    asyncio.run(
        store_response(
            redis,
            ticker="AAPL",
            fingerprint="same-assumptions",
            content={"ticker": "AAPL"},
            ttl_seconds=60,
            stored_at=1000.0,
        )
    )
    other = asyncio.run(
        get_cached_response(
            redis, ticker="MSFT", fingerprint="same-assumptions", ttl_seconds=60, now=1001.0
        )
    )
    assert other is None


def test_a_model_version_bump_can_never_serve_the_old_math(monkeypatch):
    redis = InMemoryRedisBackend()
    with TestClient(_app(redis, ttl=60)) as client:
        assert client.get(f"/v1/valuations/AAPL?{VALID_QUERY}").status_code == 200
        calls = _count_compute(monkeypatch)
        monkeypatch.setattr(response_cache_module, "MODEL_VERSION", "9.9.9")
        assert client.get(f"/v1/valuations/AAPL?{VALID_QUERY}").status_code == 200
    assert len(calls) == 1, "a bumped model_version must recompute"


# --------------------------------------------------------------------------
# What must never be cached.
# --------------------------------------------------------------------------


def test_errors_are_never_cached():
    redis = InMemoryRedisBackend()
    with TestClient(_app(redis, ttl=60)) as client:
        assert client.get(f"/v1/valuations/NOSUCH?{VALID_QUERY}").status_code == 404
        # terminal_growth >= wacc: the Gordon formula explodes, so this 422s.
        bad = (
            "wacc=0.02&terminal_growth=0.05&ebit_margin=0.30&revenue_growth=0.05&projection_years=5"
        )
        assert client.get(f"/v1/valuations/AAPL?{bad}").status_code == 422
    assert not [key for key in redis._values if ":resp:" in key]


def test_a_degraded_null_price_response_is_not_cached():
    """A Finnhub blip must not outlive itself: caching the degraded body would
    keep serving null prices for the whole TTL after the outage ended."""
    redis = InMemoryRedisBackend()
    down = FakeFinnhubClient(fail_with=ProviderError("finnhub is down"))
    with TestClient(_app(redis, ttl=60, finnhub=down)) as client:
        first = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
    assert first.json()["current_price"] is None
    assert not [key for key in redis._values if ":resp:" in key]

    # Recovered instance, same Redis: the caller gets a real price immediately.
    recovered = FakeFinnhubClient(price=250.0)
    with TestClient(_app(redis, ttl=60, finnhub=recovered)) as client:
        second = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
    assert second.json()["current_price"] == 250.0


# --------------------------------------------------------------------------
# Metering: a cached answer is still an answer.
# --------------------------------------------------------------------------


def test_a_cache_hit_is_still_metered_and_still_spends_quota():
    backend = FakeSupabaseBackend()
    key = "dcf_live_testsecret"
    _seed_valuation_key(backend, key)
    redis = InMemoryRedisBackend()
    config = _supabase_config()
    supabase_client = SupabaseClient(config, transport=backend.transport())
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    app = create_app(
        fmp_client=fmp,
        finnhub_client=FakeFinnhubClient(price=250.0),
        supabase_client=supabase_client,
        authenticator=SupabaseAPIKeyAuthenticator(supabase_client),
        redis_backend=redis,
        settings=_settings(60),
    )

    with TestClient(app) as client:
        first = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}", headers={"X-API-Key": key})
        second = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}", headers={"X-API-Key": key})

    assert first.status_code == second.status_code == 200
    assert len(backend.usage_events) == 2, "a hit must still write a usage row"
    assert int(second.headers["X-RateLimit-Remaining"]) == (
        int(first.headers["X-RateLimit-Remaining"]) - 1
    ), "a hit must still spend quota"


# --------------------------------------------------------------------------
# Fail-open, and the TTL as a live control.
# --------------------------------------------------------------------------


def test_an_unreachable_redis_falls_open_to_a_live_compute():
    class BrokenRedis:
        async def get(self, key):
            raise RuntimeError("upstash is down")

        async def set(self, key, value, **kwargs):
            raise RuntimeError("upstash is down")

        async def delete(self, key):
            raise RuntimeError("upstash is down")

        async def compare_and_delete(self, key, expected):
            raise RuntimeError("upstash is down")

        async def aclose(self):
            pass

    finnhub = FakeFinnhubClient(price=250.0)
    with TestClient(_app(BrokenRedis(), ttl=60, finnhub=finnhub)) as client:
        first = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        second = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert first.status_code == second.status_code == 200
    assert first.json()["current_price"] == 250.0
    assert finnhub.calls == 2, "a broken cache must degrade to computing, not to failing"


def test_lowering_the_ttl_takes_effect_immediately():
    """The stored entry carries Redis expiry from write time, so the TTL is
    re-checked locally — otherwise a reduced setting would not bind until the
    old entries drained."""
    redis = InMemoryRedisBackend()
    asyncio.run(
        store_response(
            redis,
            ticker="AAPL",
            fingerprint="abc",
            content={"ok": True},
            ttl_seconds=300,
            stored_at=1000.0,
        )
    )

    still_fresh = asyncio.run(
        get_cached_response(redis, ticker="AAPL", fingerprint="abc", ttl_seconds=300, now=1010.0)
    )
    assert still_fresh is not None and still_fresh.age_seconds == 10.0

    now_too_old = asyncio.run(
        get_cached_response(redis, ticker="AAPL", fingerprint="abc", ttl_seconds=5, now=1010.0)
    )
    assert now_too_old is None


def test_a_stored_body_carrying_per_request_fields_is_discarded():
    """Self-healing: a body written by an older/buggier writer is a miss, and
    the bad entry is removed rather than served."""
    redis = InMemoryRedisBackend()
    asyncio.run(
        store_response(
            redis,
            ticker="AAPL",
            fingerprint="abc",
            content={"request_id": "leaked", "ok": True},
            ttl_seconds=60,
            stored_at=1000.0,
        )
    )
    assert (
        asyncio.run(
            get_cached_response(redis, ticker="AAPL", fingerprint="abc", ttl_seconds=60, now=1001.0)
        )
        is None
    )
    assert not [key for key in redis._values if ":resp:" in key]


def test_strip_per_request_fields_keeps_computed_at():
    stripped = strip_per_request_fields(
        {"request_id": "abc", "computed_at": "2026-07-28T00:00:00Z", "ticker": "AAPL"}
    )
    assert stripped == {"computed_at": "2026-07-28T00:00:00Z", "ticker": "AAPL"}


def test_the_fingerprint_separates_sensitivity_from_assumptions():
    from app.models import Assumptions

    assumptions = Assumptions(
        wacc=0.09,
        terminal_growth=0.025,
        tax_rate=0.21,
        ebit_margin=0.30,
        projection_years=5,
        revenue_growth=0.05,
    )
    with_grid = assumption_fingerprint(assumptions, sensitivity=True)
    without_grid = assumption_fingerprint(assumptions, sensitivity=False)
    assert with_grid != without_grid
    assert with_grid == assumption_fingerprint(assumptions, sensitivity=True)
