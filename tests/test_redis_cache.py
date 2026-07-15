import asyncio
import json

import httpx
import pytest

from app.redis_cache import (
    InMemoryRedisBackend,
    RedisConfig,
    RedisError,
    UpstashRedisClient,
    decode_envelope,
    encode_envelope,
    get_envelope,
)


def test_redis_config_prefers_upstash_names_and_supports_vercel_kv(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://upstash.example/")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "upstash-token")
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example")
    monkeypatch.setenv("KV_REST_API_TOKEN", "kv-token")
    assert RedisConfig.from_env() == RedisConfig(
        url="https://upstash.example", token="upstash-token"
    )

    monkeypatch.delenv("UPSTASH_REDIS_REST_URL")
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN")
    assert RedisConfig.from_env() == RedisConfig(url="https://kv.example", token="kv-token")


def test_redis_config_is_disabled_if_either_credential_is_missing(monkeypatch):
    for name in (
        "UPSTASH_REDIS_REST_URL",
        "UPSTASH_REDIS_REST_TOKEN",
        "KV_REST_API_URL",
        "KV_REST_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://upstash.example")
    assert RedisConfig.from_env() is None


def test_in_memory_backend_enforces_ttl_nx_and_token_safe_release():
    clock = {"t": 10.0}

    async def scenario():
        redis = InMemoryRedisBackend(now=lambda: clock["t"])
        assert await redis.set("lock", "first", px=1_000, nx=True)
        assert not await redis.set("lock", "second", px=1_000, nx=True)
        assert not await redis.compare_and_delete("lock", "wrong")
        assert await redis.get("lock") == "first"
        assert await redis.compare_and_delete("lock", "first")
        assert await redis.get("lock") is None

        assert await redis.set("cached", "value", ex=2)
        clock["t"] = 11.9
        assert await redis.get("cached") == "value"
        clock["t"] = 12.0
        assert await redis.get("cached") is None

    asyncio.run(scenario())


def test_in_memory_backend_counters_expiry_pipeline_and_validation():
    clock = {"t": 10.0}

    async def scenario():
        redis = InMemoryRedisBackend(now=lambda: clock["t"])
        assert await redis.incr("counter") == 1
        assert await redis.incr("counter") == 2
        assert not await redis.expire("missing", 5)
        assert await redis.expire("counter", 5)
        assert await redis.pipeline(
            (("GET", "counter"), ("INCR", "counter"), ("EXPIRE", "counter", 2))
        ) == ["2", 3, True]
        assert await redis.pipeline((("DEL", "counter"),)) == [1]
        with pytest.raises(RedisError, match="unsupported fake"):
            await redis.pipeline((("NOPE", "key"),))
        with pytest.raises(ValueError, match="either ex or px"):
            await redis.set("bad", "value", ex=1, px=1)
        await redis.aclose()

    asyncio.run(scenario())


def test_envelope_round_trip_and_corrupt_entry_cleanup():
    async def scenario():
        redis = InMemoryRedisBackend()
        raw = encode_envelope({"ticker": "AAPL"}, stored_at=123.5)
        assert decode_envelope(raw).data == {"ticker": "AAPL"}  # type: ignore[union-attr]
        await redis.set("bad", '{"v":99,"t":1,"d":{}}')
        assert await get_envelope(redis, "bad") is None
        assert await redis.get("bad") is None

    asyncio.run(scenario())


def test_upstash_client_encodes_commands_and_pipeline():
    requests: list[tuple[str, object, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body, request.headers.get("Authorization")))
        if request.url.path == "/pipeline":
            return httpx.Response(200, json=[{"result": "one"}, {"result": 2}])
        command = body[0]
        results = {
            "GET": "value",
            "SET": "OK",
            "DEL": 1,
            "EVAL": 1,
            "INCR": 2,
            "EXPIRE": 1,
        }
        return httpx.Response(200, json={"result": results[command]})

    async def scenario():
        client = UpstashRedisClient(
            RedisConfig("https://redis.example", "secret"),
            transport=httpx.MockTransport(handler),
        )
        try:
            assert await client.get("key") == "value"
            assert await client.set("key", "value", px=10_000, nx=True)
            assert await client.set("key", "value", ex=60)
            assert await client.delete("key") == 1
            assert await client.compare_and_delete("key", "token")
            assert await client.incr("counter") == 2
            assert await client.expire("counter", 60)
            assert await client.pipeline((("GET", "a"), ("INCR", "b"))) == ["one", 2]
            with pytest.raises(ValueError, match="either ex or px"):
                await client.set("bad", "value", ex=1, px=1)
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert requests[0] == ("/", ["GET", "key"], "Bearer secret")
    assert requests[1][1] == ["SET", "key", "value", "PX", 10_000, "NX"]
    assert any(request[0] == "/pipeline" for request in requests)


def test_upstash_client_classifies_http_and_payload_failures():
    responses = iter(
        (
            httpx.Response(503),
            httpx.Response(200, json={"error": "bad command"}),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async def scenario():
        client = UpstashRedisClient(
            RedisConfig("https://redis.example", "secret"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(RedisError):
                await client.get("one")
            with pytest.raises(RedisError):
                await client.get("two")
        finally:
            await client.aclose()

    asyncio.run(scenario())
