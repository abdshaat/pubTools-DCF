"""Repeatable cold, warm, and same-ticker burst probe for the valuation API.

Default mode runs in-process against fixture-backed FMP data, so it needs no
network or API key:

    python scripts/load_probe.py

Use --base-url to probe a running local server or deployed URL instead:

    python scripts/load_probe.py --base-url https://example.vercel.app
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api import create_app  # noqa: E402
from app.auth import APIKeyAuthenticator  # noqa: E402
from app.providers.fmp import FMPClient  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "fmp"

# External-service credentials that must not influence a fixture-backed probe.
_AMBIENT_SERVICE_ENV = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "UPSTASH_REDIS_REST_URL",
    "UPSTASH_REDIS_REST_TOKEN",
    "KV_REST_API_URL",
    "KV_REST_API_TOKEN",
    "FINNHUB_API_KEY",
    "CRON_SECRET",
)

VALUATION_PARAMS = {
    "wacc": "0.09",
    "terminal_growth": "0.025",
    "ebit_margin": "0.30",
    "revenue_growth": "0.05",
    "projection_years": "5",
    "sensitivity": "false",
}


def fixture_transport(call_log: list[tuple[str, str]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        symbol = request.url.params.get("symbol", "")
        call_log.append((endpoint, symbol))
        payload = json.loads((FIXTURES / f"{symbol}.json").read_text(encoding="utf-8"))
        return httpx.Response(200, json=payload[endpoint])

    return httpx.MockTransport(handler)


@dataclass(frozen=True)
class ProbeResult:
    name: str
    status_codes: list[int]
    latencies_ms: list[float]


async def _timed_get(
    get: Callable[[str], Awaitable[httpx.Response]],
    path: str,
) -> tuple[int, float]:
    start = time.perf_counter()
    response = await get(path)
    return response.status_code, (time.perf_counter() - start) * 1000


def _summary(result: ProbeResult) -> str:
    sorted_latencies = sorted(result.latencies_ms)
    p95_index = max(0, int(len(sorted_latencies) * 0.95) - 1)
    return (
        f"{result.name}: statuses={sorted(result.status_codes)} "
        f"count={len(result.latencies_ms)} "
        f"min={min(result.latencies_ms):.1f}ms "
        f"median={statistics.median(result.latencies_ms):.1f}ms "
        f"p95={sorted_latencies[p95_index]:.1f}ms "
        f"max={max(result.latencies_ms):.1f}ms"
    )


async def _run_probes(
    get: Callable[[str], Awaitable[httpx.Response]],
    *,
    burst: int,
) -> list[ProbeResult]:
    query = httpx.QueryParams(VALUATION_PARAMS)
    path = f"/v1/valuations/AAPL?{query}"
    cold_status, cold_latency = await _timed_get(get, path)

    warm_measurements = [await _timed_get(get, path) for _ in range(5)]
    burst_measurements = await asyncio.gather(*(_timed_get(get, path) for _ in range(burst)))

    return [
        ProbeResult("cold", [cold_status], [cold_latency]),
        ProbeResult(
            "warm",
            [status for status, _ in warm_measurements],
            [latency for _, latency in warm_measurements],
        ),
        ProbeResult(
            "same_ticker_burst",
            [status for status, _ in burst_measurements],
            [latency for _, latency in burst_measurements],
        ),
    ]


async def _with_remote(base_url: str, burst: int) -> list[ProbeResult]:
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=20.0) as client:
        return await _run_probes(client.get, burst=burst)


async def _with_fixture_app(burst: int) -> list[ProbeResult]:
    # A developer .env with real SUPABASE_*/UPSTASH_*/FINNHUB_* values would
    # otherwise flip create_app() into keyed-auth mode (every probe request
    # 401s, measuring the auth gate instead of the valuation path) and point a
    # local probe at the production database. Same isolation the test suite
    # applies in tests/conftest.py.
    for variable in _AMBIENT_SERVICE_ENV:
        os.environ.pop(variable, None)

    call_log: list[tuple[str, str]] = []
    fmp = FMPClient(api_key="test-key", transport=fixture_transport(call_log))
    app = create_app(
        fmp_client=fmp,
        daily_rate_limit=10_000,
        authenticator=APIKeyAuthenticator(required=False),
    )

    async def get(path: str) -> httpx.Response:
        return await asyncio.to_thread(test_client.get, path)

    with TestClient(app) as test_client:
        results = await _run_probes(get, burst=burst)
    print(f"fixture_provider_calls={len(call_log)}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Running API base URL. Omit for fixture-backed mode.")
    parser.add_argument("--burst", type=int, default=10, help="Concurrent same-ticker requests.")
    args = parser.parse_args()

    if args.burst < 1:
        raise SystemExit("--burst must be at least 1")

    if args.base_url:
        results = asyncio.run(_with_remote(args.base_url, args.burst))
    else:
        results = asyncio.run(_with_fixture_app(args.burst))

    for result in results:
        print(_summary(result))

    # Anything other than 200 means the probe measured something else — an auth
    # gate, a quota rejection, a provider error. Latency numbers from those runs
    # are worthless, so treat them as failures rather than printing them.
    unexpected = sorted({code for result in results for code in result.status_codes if code != 200})
    if unexpected:
        raise SystemExit(f"probe did not measure the valuation path; statuses: {unexpected}")


if __name__ == "__main__":
    main()
