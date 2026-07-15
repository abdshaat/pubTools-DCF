"""Phase 8 Slice B: distributed valuation response cache + Redis login limiter.

Route-level tests drive real HTTP requests through TestClient apps that share
one `InMemoryRedisBackend`, simulating separate serverless instances sharing
one Upstash database. Compute work is observed by wrapping `compute_dcf` via
monkeypatch, so "served from the response cache" is proven by a compute count
that does not grow — not inferred from timing.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import app.api as api_module
from app.api import create_app
from app.providers.fmp import FMPClient
from app.rate_limit import DailyRequestLimiter, RedisLoginRateLimiter
from app.redis_cache import InMemoryRedisBackend
from app.response_cache import (
    RESPONSE_CACHE_TTL_SECONDS,
    assumption_fingerprint,
    get_cached_response,
    store_response,
)
from app.supabase import SupabaseAuthClient, SupabaseClient, SupabaseConfig
from tests.fake_supabase import FakeSupabaseBackend
from tests.test_api import _seed_valuation_key
from tests.test_data_layer import fixture_transport

VALID_QUERY = (
    "wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05&projection_years=5"
)


def _app(redis):
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    return create_app(fmp_client=fmp, redis_backend=redis)


def _count_compute(monkeypatch) -> list:
    """Wraps app.api.compute_dcf so cache misses are observable."""
    calls: list = []
    real = api_module.compute_dcf

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(api_module, "compute_dcf", counting)
    return calls


def test_second_instance_serves_from_response_cache_without_recompute(monkeypatch):
    calls = _count_compute(monkeypatch)
    redis = InMemoryRedisBackend()
    with (
        TestClient(_app(redis)) as first_instance,
        TestClient(_app(redis)) as second_instance,
    ):
        first = first_instance.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        assert first.status_code == 200
        assert len(calls) == 1

        second = second_instance.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        assert second.status_code == 200

    # no recompute, identical content, identical ETag — but fresh bookkeeping
    assert len(calls) == 1
    assert second.headers["ETag"] == first.headers["ETag"]
    first_body, second_body = first.json(), second.json()
    assert first_body["request_id"] != second_body["request_id"]
    for field in ("intrinsic_value_per_share", "data_version", "ticker", "current_price"):
        assert first_body[field] == second_body[field]


def test_equivalent_request_forms_share_one_cache_entry(monkeypatch):
    calls = _count_compute(monkeypatch)
    redis = InMemoryRedisBackend()
    with TestClient(_app(redis)) as client:
        canonical = client.get(
            "/v1/valuations/AAPL?wacc=0.09&terminal_growth=0.025&ebit_margin=0.30"
            "&revenue_growth=0.05&projection_years=5&tax_rate=0.21"
        )
        reshuffled = client.get(
            "/v1/valuations/AAPL?projection_years=5"
            "&revenue_growth=0.05,0.05,0.05,0.05,0.05&terminal_growth=0.025"
            "&ebit_margin=0.30&wacc=0.09"
        )
    assert canonical.status_code == reshuffled.status_code == 200
    assert canonical.headers["ETag"] == reshuffled.headers["ETag"]
    assert len(calls) == 1


def test_sensitivity_flag_is_part_of_the_cache_key(monkeypatch):
    calls = _count_compute(monkeypatch)
    redis = InMemoryRedisBackend()
    with TestClient(_app(redis)) as client:
        with_grid = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        without_grid = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}&sensitivity=false")
    assert with_grid.json()["sensitivity"] is not None
    assert without_grid.json()["sensitivity"] is None
    assert len(calls) == 2


def test_conditional_request_against_cached_response_returns_304(monkeypatch):
    calls = _count_compute(monkeypatch)
    redis = InMemoryRedisBackend()
    with TestClient(_app(redis)) as client:
        fresh = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        etag = fresh.headers["ETag"]
        conditional = client.get(
            f"/v1/valuations/AAPL?{VALID_QUERY}", headers={"If-None-Match": etag}
        )
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert conditional.headers["ETag"] == etag
    assert len(calls) == 1  # the 304 decision came from the cache, not a recompute


def test_response_cache_hit_still_consumes_quota_and_records_usage():
    backend = FakeSupabaseBackend()
    key = "dcf_live_testsecret"
    _seed_valuation_key(backend, key)
    redis = InMemoryRedisBackend()
    config = SupabaseConfig(url="https://fake.supabase.co", service_role_key="k")
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    app = create_app(
        fmp_client=fmp,
        supabase_client=SupabaseClient(config, transport=backend.transport()),
        auth_client=SupabaseAuthClient(config, transport=backend.transport()),
        redis_backend=redis,
    )
    with TestClient(app) as client:
        first = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}", headers={"X-API-Key": key})
        assert first.status_code == 200
        usage_after_first = len(backend.usage_events)
        quota_after_first = dict(backend.quota_counters)

        hit = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}", headers={"X-API-Key": key})
        assert hit.status_code == 200

    # a response-cache hit is still an origin request: metered and counted
    assert len(backend.usage_events) == usage_after_first + 1
    today = datetime.now(UTC).date().isoformat()
    assert backend.quota_counters[("key-1", today)] == quota_after_first[("key-1", today)] + 1


def test_error_responses_are_never_cached(monkeypatch):
    redis = InMemoryRedisBackend()
    with TestClient(_app(redis)) as client:
        invalid = client.get(
            "/v1/valuations/AAPL?wacc=0.09&terminal_growth=0.20"
            "&ebit_margin=0.30&revenue_growth=0.05"
        )
        assert invalid.status_code == 422
    assert not [k for k in redis._values if k.startswith("dcf:v1:resp:")]


def test_generation_rotation_makes_cached_responses_unreachable(monkeypatch):
    calls = _count_compute(monkeypatch)
    redis = InMemoryRedisBackend()
    with TestClient(_app(redis)) as client:
        client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        assert len(calls) == 1

        # Slice C's scheduled refresh rotates the ticker generation after a
        # successful DB promotion; every cached assumption-variant must miss.
        asyncio.run(redis.set("dcf:v1:gen:AAPL", "2"))

        client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
    assert len(calls) == 2


def test_corrupt_cached_response_is_deleted_and_recomputed(monkeypatch):
    calls = _count_compute(monkeypatch)
    redis = InMemoryRedisBackend()
    with TestClient(_app(redis)) as client:
        client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        assert len(calls) == 1
        resp_keys = [k for k in redis._values if k.startswith("dcf:v1:resp:")]
        assert len(resp_keys) == 1
        asyncio.run(redis.set(resp_keys[0], "not-json-at-all", ex=60))

        recovered = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
    assert recovered.status_code == 200
    assert len(calls) == 2


def test_cached_payload_with_per_request_fields_is_rejected():
    redis = InMemoryRedisBackend()

    async def scenario():
        fingerprint = "f" * 64
        await store_response(
            redis,
            ticker="AAPL",
            fingerprint=fingerprint,
            content={"ticker": "AAPL", "request_id": "leaked"},
            stored_at=1.0,
        )
        return await get_cached_response(redis, ticker="AAPL", fingerprint=fingerprint)

    assert asyncio.run(scenario()) is None


def test_response_cache_expires_after_its_ttl(monkeypatch):
    clock = {"t": 1_000_000.0}
    redis = InMemoryRedisBackend(now=lambda: clock["t"])

    async def scenario():
        fingerprint = "a" * 64
        await store_response(
            redis,
            ticker="AAPL",
            fingerprint=fingerprint,
            content={"ticker": "AAPL"},
            stored_at=clock["t"],
        )
        fresh = await get_cached_response(redis, ticker="AAPL", fingerprint=fingerprint)
        clock["t"] += RESPONSE_CACHE_TTL_SECONDS + 1
        expired = await get_cached_response(redis, ticker="AAPL", fingerprint=fingerprint)
        return fresh, expired

    fresh, expired = asyncio.run(scenario())
    assert fresh == {"ticker": "AAPL"}
    assert expired is None


def test_redis_outage_fails_open_to_a_normal_computed_response(monkeypatch):
    class UnavailableRedis(InMemoryRedisBackend):
        async def get(self, key: str) -> str | None:
            raise OSError("redis unavailable")

        async def set(self, key: str, value: str, **kwargs) -> bool:
            raise OSError("redis unavailable")

        async def pipeline(self, commands):
            raise OSError("redis unavailable")

    calls = _count_compute(monkeypatch)
    with TestClient(_app(UnavailableRedis())) as client:
        response = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
    assert response.status_code == 200
    assert len(calls) == 1


def test_fingerprint_changes_with_any_assumption_but_not_form():
    from app.models import Assumptions

    base = Assumptions(
        wacc=0.09,
        terminal_growth=0.025,
        tax_rate=0.21,
        ebit_margin=0.30,
        projection_years=5,
        revenue_growth=0.05,
    )
    expanded = Assumptions(
        wacc=0.09,
        terminal_growth=0.025,
        tax_rate=0.21,
        ebit_margin=0.30,
        projection_years=5,
        revenue_growth=[0.05, 0.05, 0.05, 0.05, 0.05],
    )
    different = Assumptions(
        wacc=0.10,
        terminal_growth=0.025,
        tax_rate=0.21,
        ebit_margin=0.30,
        projection_years=5,
        revenue_growth=0.05,
    )
    same = assumption_fingerprint(base, sensitivity=True)
    assert assumption_fingerprint(expanded, sensitivity=True) == same
    assert assumption_fingerprint(different, sensitivity=True) != same
    assert assumption_fingerprint(base, sensitivity=False) != same


# --- Redis login rate limiter ---


def test_login_attempts_are_limited_across_instances():
    backend = FakeSupabaseBackend()
    redis = InMemoryRedisBackend()
    config = SupabaseConfig(url="https://fake.supabase.co", service_role_key="k")

    def accounts_app():
        return create_app(
            fmp_client=FMPClient(api_key="test-key", transport=fixture_transport()),
            supabase_client=SupabaseClient(config, transport=backend.transport()),
            auth_client=SupabaseAuthClient(config, transport=backend.transport()),
            redis_backend=redis,
        )

    with TestClient(accounts_app()) as first, TestClient(accounts_app()) as second:
        # both instances share one Redis counter for the same client IP
        for _ in range(10):
            assert first.get("/v1/auth/github/login", follow_redirects=False).status_code == 302
        for _ in range(10):
            assert second.get("/v1/auth/github/login", follow_redirects=False).status_code == 302
        blocked = second.get("/v1/auth/github/login", follow_redirects=False)

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "login_rate_limited"


def test_redis_login_limiter_counts_and_resets_by_utc_day():
    clock = {"t": 1_752_000_000.0}
    redis = InMemoryRedisBackend(now=lambda: clock["t"])
    limiter = RedisLoginRateLimiter(redis, limit=2, now=lambda: clock["t"])

    async def scenario():
        first = await limiter.check_and_increment(identity="1.2.3.4")
        second = await limiter.check_and_increment(identity="1.2.3.4")
        third = await limiter.check_and_increment(identity="1.2.3.4")
        other_ip = await limiter.check_and_increment(identity="5.6.7.8")
        clock["t"] += 2 * 24 * 3600  # comfortably into a later UTC day
        next_day = await limiter.check_and_increment(identity="1.2.3.4")
        return first, second, third, other_ip, next_day

    first, second, third, other_ip, next_day = asyncio.run(scenario())
    assert first.allowed and second.allowed
    assert not third.allowed
    assert third.remaining == 0
    assert third.retry_after >= 1
    assert other_ip.allowed  # independent identity, independent counter
    assert next_day.allowed  # a new UTC day starts a new counter


def test_redis_login_limiter_fails_open_to_in_process_fallback():
    class UnavailableRedis(InMemoryRedisBackend):
        async def pipeline(self, commands):
            raise OSError("redis unavailable")

    fallback = DailyRequestLimiter(2)
    limiter = RedisLoginRateLimiter(UnavailableRedis(), limit=2, fallback=fallback)

    async def scenario():
        results = [await limiter.check_and_increment(identity="1.2.3.4") for _ in range(3)]
        return results

    results = asyncio.run(scenario())
    assert [r.allowed for r in results] == [True, True, False]


def test_redis_login_limiter_rejects_a_nonpositive_limit():
    with pytest.raises(ValueError):
        RedisLoginRateLimiter(InMemoryRedisBackend(), limit=0)

    limiter = RedisLoginRateLimiter(InMemoryRedisBackend(), limit=5)
    # limit=0 is falsy and means "use the default" (same convention as
    # DailyRequestLimiter); a negative limit is a genuine caller bug.
    with pytest.raises(ValueError):
        asyncio.run(limiter.check_and_increment(identity="x", limit=-1))
