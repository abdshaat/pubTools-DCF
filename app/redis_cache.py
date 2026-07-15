"""Small, injectable Redis primitives for Phase 8 distributed caching.

Upstash is accessed through its HTTP REST API so Vercel functions do not keep
TCP pools.  Callers decide whether an operation is fail-open or fail-closed;
the fundamentals cache deliberately catches :class:`RedisError` and treats it
as a miss because Redis is only an accelerator/coordinator.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any, Protocol

import httpx

REDIS_KEY_PREFIX = "dcf:v1:"
ENVELOPE_VERSION = 1
_COMPARE_AND_DELETE = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


class RedisError(Exception):
    """The optional Redis accelerator could not complete an operation."""


@dataclass(frozen=True)
class RedisConfig:
    url: str
    token: str
    timeout_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> RedisConfig | None:
        url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
        token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
        if not url or not token:
            return None
        return cls(url=url.rstrip("/"), token=token)


class RedisBackend(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool: ...

    async def delete(self, key: str) -> int: ...

    async def compare_and_delete(self, key: str, expected: str) -> bool: ...

    async def incr(self, key: str) -> int: ...

    async def expire(self, key: str, seconds: int) -> bool: ...

    async def pipeline(self, commands: Sequence[Sequence[str | int]]) -> list[Any]: ...

    async def aclose(self) -> None: ...


class UpstashRedisClient:
    """Minimal Upstash REST client using the project's existing httpx dependency."""

    def __init__(
        self,
        config: RedisConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=config.url,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=config.timeout_seconds,
            transport=transport,
        )

    async def _command(self, *parts: str | int) -> Any:
        try:
            response = await self._client.post("/", json=list(parts))
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RedisError("Redis command failed") from exc
        if not isinstance(payload, dict) or "result" not in payload or payload.get("error"):
            raise RedisError("Redis returned an unexpected command response")
        return payload["result"]

    async def get(self, key: str) -> str | None:
        result = await self._command("GET", key)
        if result is None:
            return None
        if not isinstance(result, str):
            raise RedisError("Redis GET returned a non-string value")
        return result

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        if ex is not None and px is not None:
            raise ValueError("SET accepts either ex or px, not both")
        command: list[str | int] = ["SET", key, value]
        if ex is not None:
            command.extend(("EX", ex))
        if px is not None:
            command.extend(("PX", px))
        if nx:
            command.append("NX")
        return await self._command(*command) == "OK"

    async def delete(self, key: str) -> int:
        return int(await self._command("DEL", key))

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        result = await self._command("EVAL", _COMPARE_AND_DELETE, 1, key, expected)
        return int(result) == 1

    async def incr(self, key: str) -> int:
        return int(await self._command("INCR", key))

    async def expire(self, key: str, seconds: int) -> bool:
        return bool(int(await self._command("EXPIRE", key, seconds)))

    async def pipeline(self, commands: Sequence[Sequence[str | int]]) -> list[Any]:
        try:
            response = await self._client.post("/pipeline", json=[list(item) for item in commands])
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RedisError("Redis pipeline failed") from exc
        if not isinstance(payload, list):
            raise RedisError("Redis returned an unexpected pipeline response")
        results: list[Any] = []
        for item in payload:
            if not isinstance(item, dict) or "result" not in item or item.get("error"):
                raise RedisError("Redis pipeline command failed")
            results.append(item["result"])
        return results

    async def aclose(self) -> None:
        await self._client.aclose()


class InMemoryRedisBackend:
    """Test backend with real expiry and SET-NX behavior.

    Async methods intentionally contain no suspension points, making each
    operation atomic within an asyncio event loop just like one Redis command.
    """

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._values: dict[str, tuple[str, float | None]] = {}

    def _read(self, key: str) -> str | None:
        item = self._values.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at is not None and self._now() >= expires_at:
            self._values.pop(key, None)
            return None
        return value

    async def get(self, key: str) -> str | None:
        return self._read(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        if ex is not None and px is not None:
            raise ValueError("SET accepts either ex or px, not both")
        if nx and self._read(key) is not None:
            return False
        ttl = float(ex) if ex is not None else (px / 1000 if px is not None else None)
        self._values[key] = (value, self._now() + ttl if ttl is not None else None)
        return True

    async def delete(self, key: str) -> int:
        existed = self._read(key) is not None
        self._values.pop(key, None)
        return int(existed)

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        if self._read(key) != expected:
            return False
        self._values.pop(key, None)
        return True

    async def incr(self, key: str) -> int:
        current = self._read(key)
        expires_at = self._values.get(key, ("", None))[1]
        value = int(current or "0") + 1
        self._values[key] = (str(value), expires_at)
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        value = self._read(key)
        if value is None:
            return False
        self._values[key] = (value, self._now() + seconds)
        return True

    async def pipeline(self, commands: Sequence[Sequence[str | int]]) -> list[Any]:
        results: list[Any] = []
        for command in commands:
            name = str(command[0]).upper()
            if name == "GET":
                results.append(await self.get(str(command[1])))
            elif name == "DEL":
                results.append(await self.delete(str(command[1])))
            elif name == "INCR":
                results.append(await self.incr(str(command[1])))
            elif name == "EXPIRE":
                results.append(await self.expire(str(command[1]), int(command[2])))
            else:
                raise RedisError(f"unsupported fake pipeline command: {name}")
        return results

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True)
class CacheEnvelope:
    stored_at: float
    data: Any


def encode_envelope(data: Any, *, stored_at: float) -> str:
    return json.dumps(
        {"v": ENVELOPE_VERSION, "t": stored_at, "d": data},
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_envelope(raw: str) -> CacheEnvelope | None:
    try:
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or payload.get("v") != ENVELOPE_VERSION
            or not isinstance(payload.get("t"), (int, float))
            or not isfinite(float(payload["t"]))
            or "d" not in payload
        ):
            return None
        return CacheEnvelope(stored_at=float(payload["t"]), data=payload["d"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


async def get_envelope(backend: RedisBackend, key: str) -> CacheEnvelope | None:
    raw = await backend.get(key)
    if raw is None:
        return None
    envelope = decode_envelope(raw)
    if envelope is None:
        await backend.delete(key)
    return envelope


async def set_envelope(
    backend: RedisBackend,
    key: str,
    data: Any,
    *,
    ttl_seconds: int,
    stored_at: float,
) -> None:
    await backend.set(key, encode_envelope(data, stored_at=stored_at), ex=ttl_seconds)
