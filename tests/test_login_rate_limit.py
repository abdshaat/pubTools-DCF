"""Redis-backed login rate limiting (Phase 8 Slice B).

Extracted from the retired test_response_cache.py when ADR-008 removed the
valuation response cache; the login limiter is unrelated to it and stays.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.providers.fmp import FMPClient
from app.rate_limit import DailyRequestLimiter, RedisLoginRateLimiter
from app.redis_cache import InMemoryRedisBackend
from app.supabase import SupabaseAuthClient, SupabaseClient, SupabaseConfig
from tests.fake_supabase import FakeSupabaseBackend
from tests.test_data_layer import fixture_transport


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
