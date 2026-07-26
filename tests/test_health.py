"""Liveness, readiness, and metrics (Phase 11 Slice 3).

The properties that make these endpoints safe to poll: liveness never touches a
dependency, readiness never spends a metered provider call and cannot amplify
under a probe storm, and metrics are not public.
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.auth import APIKeyAuthenticator
from app.observability import MetricsRegistry
from app.providers.fmp import FMPClient
from app.readiness import (
    STATUS_DEGRADED,
    STATUS_NOT_CONFIGURED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    ReadinessChecker,
)
from app.redis_cache import InMemoryRedisBackend
from app.supabase import SupabaseClient, SupabaseConfig
from tests.fake_supabase import FakeSupabaseBackend
from tests.test_data_layer import fixture_transport

VALID_QUERY = (
    "wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05&projection_years=5"
)


class CountingBackend:
    """Wraps the Supabase fake so probes can be counted (and made to fail)."""

    def __init__(self) -> None:
        self.fake = FakeSupabaseBackend()
        self.probes = 0
        self.down = False

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/v1/api_keys" and "select=id" in str(request.url.query):
                self.probes += 1
                if self.down:
                    return httpx.Response(500, json={"message": "boom"})
                return httpx.Response(200, json=[])
            return await self.fake.handler(request)

        return httpx.MockTransport(handler)


class CountingRedis(InMemoryRedisBackend):
    def __init__(self) -> None:
        super().__init__()
        self.down = False

    async def get(self, key: str) -> str | None:
        if self.down:
            raise RuntimeError("redis is unreachable")
        return await super().get(key)


def _app(*, supabase=None, redis=None, fmp_calls=None, **kwargs):
    fmp = FMPClient(api_key="test-key", transport=fixture_transport(fmp_calls))
    kwargs.setdefault("authenticator", APIKeyAuthenticator(required=False))
    return create_app(fmp_client=fmp, supabase_client=supabase, redis_backend=redis, **kwargs)


def _supabase(backend: CountingBackend) -> SupabaseClient:
    return SupabaseClient(
        SupabaseConfig(url="https://example.supabase.co", service_role_key="service-key"),
        transport=backend.transport(),
    )


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


def test_liveness_answers_without_touching_any_dependency():
    backend = CountingBackend()
    redis = CountingRedis()
    fmp_calls: list[tuple[str, str]] = []
    with TestClient(_app(supabase=_supabase(backend), redis=redis, fmp_calls=fmp_calls)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["model_version"]
    assert body["environment"] == "local"
    # A liveness probe that fails when a database is slow gets the instance
    # killed for someone else's outage.
    assert backend.probes == 0
    assert fmp_calls == []


def test_liveness_stays_up_while_a_dependency_is_down():
    backend = CountingBackend()
    backend.down = True
    with TestClient(_app(supabase=_supabase(backend))) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_readiness_reports_every_dependency_when_healthy():
    backend = CountingBackend()
    with TestClient(_app(supabase=_supabase(backend), redis=CountingRedis())) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    statuses = {item["name"]: item["status"] for item in body["dependencies"]}
    assert statuses["supabase"] == STATUS_OK
    assert statuses["redis"] == STATUS_OK
    assert statuses["finnhub_price"] == STATUS_NOT_CONFIGURED
    assert response.headers["Cache-Control"] == "no-store"


def test_a_failed_closed_dependency_makes_the_instance_unready():
    backend = CountingBackend()
    backend.down = True
    with TestClient(_app(supabase=_supabase(backend), redis=CountingRedis())) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    statuses = {item["name"]: item["status"] for item in body["dependencies"]}
    assert statuses["supabase"] == STATUS_UNAVAILABLE
    assert statuses["redis"] == STATUS_OK


def test_a_redis_outage_is_degraded_not_unready():
    """Redis loss costs latency, not correctness (ADR-004 fail-open matrix)."""
    backend = CountingBackend()
    redis = CountingRedis()
    redis.down = True
    with TestClient(_app(supabase=_supabase(backend), redis=redis)) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    statuses = {item["name"]: item["status"] for item in response.json()["dependencies"]}
    assert statuses["redis"] == STATUS_DEGRADED
    assert statuses["supabase"] == STATUS_OK


def test_unconfigured_dependencies_do_not_make_an_instance_unready():
    with TestClient(_app()) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    statuses = {item["name"]: item["status"] for item in response.json()["dependencies"]}
    assert statuses == {
        "supabase": STATUS_NOT_CONFIGURED,
        "redis": STATUS_NOT_CONFIGURED,
        "finnhub_price": STATUS_NOT_CONFIGURED,
    }


def test_readiness_never_spends_a_provider_call():
    """FMP is a daily budget and Finnhub is 60/min; probes are polled."""
    backend = CountingBackend()
    fmp_calls: list[tuple[str, str]] = []
    with TestClient(_app(supabase=_supabase(backend), fmp_calls=fmp_calls)) as client:
        for _ in range(5):
            client.get("/ready")

    assert fmp_calls == []


def test_readiness_body_describes_status_only_not_infrastructure():
    backend = CountingBackend()
    with TestClient(_app(supabase=_supabase(backend), redis=CountingRedis())) as client:
        body = json.dumps(client.get("/ready").json())

    # The endpoint is unauthenticated; it must not describe the deployment.
    assert "supabase.co" not in body
    assert "service-key" not in body
    assert "http" not in body.replace("https://", "")


def test_a_probe_storm_collapses_onto_one_check():
    backend = CountingBackend()
    checker = ReadinessChecker(supabase=_supabase(backend), cache_seconds=5.0)

    async def storm() -> None:
        await asyncio.gather(*(checker.check() for _ in range(20)))

    asyncio.run(storm())
    assert backend.probes == 1


def test_cached_readiness_expires_after_its_window():
    backend = CountingBackend()
    clock = {"t": 1000.0}
    checker = ReadinessChecker(
        supabase=_supabase(backend), cache_seconds=5.0, clock=lambda: clock["t"]
    )

    first = asyncio.run(checker.check())
    assert first.cached is False
    assert asyncio.run(checker.check()).cached is True
    assert backend.probes == 1

    clock["t"] += 5.0
    refreshed = asyncio.run(checker.check())
    assert refreshed.cached is False
    assert backend.probes == 2


def test_readiness_recovers_when_the_dependency_comes_back():
    backend = CountingBackend()
    backend.down = True
    clock = {"t": 0.0}
    checker = ReadinessChecker(
        supabase=_supabase(backend), cache_seconds=1.0, clock=lambda: clock["t"]
    )
    assert asyncio.run(checker.check()).ready is False

    backend.down = False
    clock["t"] += 2.0
    assert asyncio.run(checker.check()).ready is True


def test_negative_cache_window_is_refused():
    with pytest.raises(ValueError):
        ReadinessChecker(cache_seconds=-1)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_are_not_public(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    with TestClient(_app()) as client:
        unconfigured = client.get("/internal/metrics")
    assert unconfigured.status_code == 401
    assert unconfigured.headers["Cache-Control"] == "no-store"

    monkeypatch.setenv("METRICS_TOKEN", "metrics-token-value")
    with TestClient(_app()) as client:
        assert client.get("/internal/metrics").status_code == 401
        assert (
            client.get(
                "/internal/metrics", headers={"Authorization": "Bearer wrong-token-value"}
            ).status_code
            == 401
        )
        allowed = client.get(
            "/internal/metrics", headers={"Authorization": "Bearer metrics-token-value"}
        )
    assert allowed.status_code == 200
    assert allowed.headers["content-type"].startswith("text/plain")


def test_metrics_describe_the_traffic_that_happened(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "metrics-token-value")
    with TestClient(_app(daily_rate_limit=1)) as client:
        client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")  # 200, cold
        client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")  # 429, over quota
        rendered = client.get(
            "/internal/metrics", headers={"Authorization": "Bearer metrics-token-value"}
        ).text

    assert 'dcf_http_requests_total{route="/v1/valuations/{ticker}",status="200"} 1' in rendered
    assert 'dcf_http_requests_total{route="/v1/valuations/{ticker}",status="429"} 1' in rendered
    assert 'dcf_cache_reads_total{outcome="provider"} 1' in rendered
    assert 'dcf_provider_calls_total{service="fmp"} 4' in rendered
    assert "dcf_quota_rejections_total 1" in rendered
    assert 'dcf_price_lookups_total{result="unavailable"} 1' in rendered
    assert "dcf_http_request_duration_seconds_count 2" in rendered
    assert "dcf_http_request_duration_seconds_sum" in rendered
    assert 'dcf_http_request_duration_seconds_bucket{le="+Inf"} 2' in rendered


def test_metrics_count_errors_by_stable_code(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "metrics-token-value")
    with TestClient(_app()) as client:
        client.get(f"/v1/valuations/NOSUCH?{VALID_QUERY}")
        rendered = client.get(
            "/internal/metrics", headers={"Authorization": "Bearer metrics-token-value"}
        ).text

    assert 'dcf_errors_total{code="ticker_not_found"} 1' in rendered


def test_metrics_exposition_is_well_formed():
    registry = MetricsRegistry()
    registry.observe_request(
        route="/v1/valuations/{ticker}",
        status=200,
        duration_seconds=0.02,
        fields={"cache": "l1", "fmp_calls": 0, "supabase_calls": 3, "price": "live"},
    )
    rendered = registry.render()

    for name in ("dcf_http_requests_total", "dcf_http_request_duration_seconds"):
        assert f"# HELP {name} " in rendered
        assert f"# TYPE {name} " in rendered
    # Buckets are cumulative and monotonic.
    counts = [
        int(line.rsplit(" ", 1)[1])
        for line in rendered.splitlines()
        if line.startswith("dcf_http_request_duration_seconds_bucket")
    ]
    assert counts == sorted(counts)
    assert counts[-1] == 1
    assert rendered.endswith("\n")


def test_operational_endpoints_stay_out_of_the_customer_contract():
    with TestClient(_app()) as client:
        paths = client.get("/openapi.json").json()["paths"]
    for internal in ("/health", "/ready", "/internal/metrics"):
        assert internal not in paths
