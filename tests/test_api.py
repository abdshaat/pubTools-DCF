"""End-to-end API tests: HTTP request -> JSON response over fixture data.

Uses FastAPI's TestClient with the fixture-backed FMP transport, so the
full stack (routing, validation, fetch, normalization, engine, response
shaping, error mapping) is exercised without network or API key.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import MODEL_VERSION
from app.api import _default_raw_sink, create_app
from app.providers.fmp import FileRawSink, FMPClient
from tests.test_data_layer import fixture_transport

VALID_QUERY = (
    "wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05&projection_years=5"
)


def test_default_raw_sink_is_disabled_on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    assert _default_raw_sink() is None


def test_default_raw_sink_is_available_for_local_development(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    assert isinstance(_default_raw_sink(), FileRawSink)


@pytest.fixture
def client() -> TestClient:
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    app = create_app(fmp_client=fmp)
    with TestClient(app) as test_client:
        yield test_client


def test_valuation_happy_path_is_auditable(client: TestClient):
    response = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert response.status_code == 200
    body = response.json()

    # spec: response must let the caller audit the math end to end
    assert body["model_version"] == MODEL_VERSION
    assert body["ticker"] == "AAPL"
    assert body["base_financials"]["source_period"] == "FY2025 (2025-09-27)"
    assert body["base_financials"]["revenue"] == 391_035_000_000.0
    assert body["current_price"] == 245.5
    assert len(body["projections"]) == 5
    assert {"year", "revenue", "ebit", "fcf", "discount_factor", "pv_fcf"} <= set(
        body["projections"][0]
    )
    assert body["enterprise_value"] == pytest.approx(
        sum(p["pv_fcf"] for p in body["projections"]) + body["pv_terminal_value"]
    )
    assert body["intrinsic_value_per_share"] > 0


def test_scalar_revenue_growth_is_echoed_resolved_per_year(client: TestClient):
    response = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
    assert response.json()["assumptions"]["revenue_growth"] == [0.05] * 5


def test_per_year_revenue_growth_list_accepted(client: TestClient):
    response = client.get(
        "/v1/valuations/AAPL?wacc=0.09&terminal_growth=0.025&ebit_margin=0.30"
        "&revenue_growth=0.08,0.07,0.06,0.05,0.04&projection_years=5"
    )
    assert response.status_code == 200
    assert response.json()["assumptions"]["revenue_growth"] == [
        0.08,
        0.07,
        0.06,
        0.05,
        0.04,
    ]


def test_ticker_is_case_insensitive(client: TestClient):
    response = client.get(f"/v1/valuations/aapl?{VALID_QUERY}")
    assert response.status_code == 200
    assert response.json()["ticker"] == "AAPL"


def test_unknown_ticker_returns_404(client: TestClient):
    response = client.get(f"/v1/valuations/ZZZZZZ?{VALID_QUERY}")
    assert response.status_code == 404
    assert "ZZZZZZ" in response.json()["detail"]


def test_ticker_outside_data_plan_returns_404_with_explanation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="not available under your current subscription")

    fmp = FMPClient(api_key="test-key", transport=httpx.MockTransport(handler))
    with TestClient(create_app(fmp_client=fmp)) as tc:
        response = tc.get(f"/v1/valuations/SMALLCAP?{VALID_QUERY}")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "SMALLCAP" in detail
    # message must not tell the customer to upgrade a plan they don't own
    assert "subscription" not in detail.lower()
    assert "upgrade" not in detail.lower()


def test_financial_sector_returns_422_with_explanation(client: TestClient):
    response = client.get(f"/v1/valuations/JPM?{VALID_QUERY}")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["field"] == "ticker"
    assert "Financial Services" in detail[0]["message"]


def test_terminal_growth_at_or_above_wacc_returns_422(client: TestClient):
    response = client.get(
        "/v1/valuations/AAPL?wacc=0.05&terminal_growth=0.05&ebit_margin=0.30&revenue_growth=0.05"
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["field"] == "terminal_growth"


def test_malformed_revenue_growth_returns_422(client: TestClient):
    response = client.get(
        "/v1/valuations/AAPL?wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=fast"
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["field"] == "revenue_growth"


def test_growth_list_length_mismatch_returns_422(client: TestClient):
    response = client.get(
        "/v1/valuations/AAPL?wacc=0.09&terminal_growth=0.025&ebit_margin=0.30"
        "&revenue_growth=0.08,0.07&projection_years=5"
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["field"] == "revenue_growth"


def test_projection_years_out_of_range_returns_422(client: TestClient):
    response = client.get(
        "/v1/valuations/AAPL?wacc=0.09&terminal_growth=0.025&ebit_margin=0.30"
        "&revenue_growth=0.05&projection_years=20"
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["field"] == "projection_years"


def test_missing_required_param_returns_422(client: TestClient):
    response = client.get("/v1/valuations/AAPL?wacc=0.09")
    assert response.status_code == 422  # FastAPI's own required-param error


def test_provider_outage_returns_503():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def no_sleep(seconds: float) -> None:
        pass

    fmp = FMPClient(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        sleep=no_sleep,
        max_retries=1,
    )
    with TestClient(create_app(fmp_client=fmp)) as test_client:
        response = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert response.status_code == 503
    assert "provider" in response.json()["detail"]


def test_second_request_served_from_cache():
    call_log: list = []
    fmp = FMPClient(api_key="test-key", transport=fixture_transport(call_log))
    with TestClient(create_app(fmp_client=fmp)) as test_client:
        first = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        second = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert first.status_code == second.status_code == 200
    assert len(call_log) == 5  # one fetch cycle, second request cache-served


def test_sensitivity_grid_included_by_default(client: TestClient):
    response = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
    body = response.json()

    grid = body["sensitivity"]
    assert grid["wacc_values"] == [0.08, 0.09, 0.1]
    assert grid["terminal_growth_values"] == [0.02, 0.025, 0.03]
    assert len(grid["intrinsic_value_per_share"]) == 3
    assert all(len(row) == 3 for row in grid["intrinsic_value_per_share"])
    # center cell is the point estimate
    assert grid["intrinsic_value_per_share"][1][1] == pytest.approx(
        body["intrinsic_value_per_share"]
    )


def test_sensitivity_grid_can_be_disabled(client: TestClient):
    response = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}&sensitivity=false")
    assert response.status_code == 200
    assert response.json()["sensitivity"] is None


def test_health_endpoint():
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    with TestClient(create_app(fmp_client=fmp)) as test_client:
        response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json()["model_version"] == MODEL_VERSION
