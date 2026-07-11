"""End-to-end API tests: HTTP request -> JSON response over fixture data.

Uses FastAPI's TestClient with the fixture-backed FMP transport, so the
full stack (routing, validation, fetch, normalization, engine, response
shaping, error mapping) is exercised without network or API key.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app import MODEL_VERSION
from app.api import _default_raw_sink, create_app
from app.auth import APIKeyAuthenticator
from app.providers.fmp import FileRawSink, FMPClient
from tests.test_data_layer import fixture_transport, load_fixture

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
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert datetime.fromisoformat(body["computed_at"]).tzinfo is not None
    assert body["data_version"].startswith("sha256:")
    assert len(body["data_version"]) == len("sha256:") + 64
    assert body["data_provider"] == "financialmodelingprep"
    assert body["currency"] == "USD"
    assert body["monetary_unit"] == "raw_currency_units"
    assert body["fundamentals_as_of"] == "2025-09-27"
    assert body["price_as_of"] is None
    assert datetime.fromisoformat(body["price_fetched_at"]).tzinfo is not None
    assert body["fiscal_year"] == "2025"
    assert body["statement_period"] == "FY"
    assert body["filing_date"] is None
    assert body["accepted_at"] is None
    assert body["statement_selection"] == "latest_complete_annual"
    assert "not investment advice" in body["disclaimer"]
    assert body["ticker"] == "AAPL"
    assert body["base_financials"]["source_period"] == "FY2025 (2025-09-27)"
    assert body["base_financials"]["revenue"] == 391_035_000_000.0
    assert body["current_price"] == 245.5
    assert body["warnings"] == ["Statement currency was absent; profile currency was used."]
    UUID(response.headers["X-Request-ID"])
    assert len(body["projections"]) == 5
    first = body["projections"][0]
    assert {
        "year",
        "revenue_growth",
        "revenue",
        "ebit_margin",
        "ebit",
        "cash_taxes",
        "nopat",
        "da",
        "capex",
        "delta_nwc",
        "fcf",
        "discount_period",
        "discount_factor",
        "pv_fcf",
    } <= set(first)
    assert first["cash_taxes"] == pytest.approx(first["ebit"] * 0.21)
    assert first["nopat"] == pytest.approx(first["ebit"] - first["cash_taxes"])
    assert first["fcf"] == pytest.approx(
        first["nopat"] + first["da"] - first["capex"] - first["delta_nwc"]
    )
    assert first["discount_period"] == 1.0
    assert first["pv_fcf"] == pytest.approx(first["fcf"] * first["discount_factor"])
    assert body["enterprise_value"] == pytest.approx(
        sum(p["pv_fcf"] for p in body["projections"]) + body["pv_terminal_value"]
    )
    assert body["equity_value"] == pytest.approx(
        body["enterprise_value"] - body["base_financials"]["net_debt"]
    )
    assert body["intrinsic_value_per_share"] == pytest.approx(
        body["equity_value"] / body["base_financials"]["diluted_shares"]
    )
    assert body["intrinsic_value_per_share"] > 0


def test_scalar_revenue_growth_is_echoed_resolved_per_year(client: TestClient):
    response = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
    assert response.json()["assumptions"]["revenue_growth"] == [0.05] * 5


def test_data_version_is_stable_for_same_normalized_snapshot(client: TestClient):
    first = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}").json()
    second = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}").json()
    assert first["data_version"] == second["data_version"]
    assert first["request_id"] != second["request_id"]


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
    assert response.json()["error"]["code"] == "ticker_not_found"


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
    assert response.json()["error"]["code"] == "ticker_unavailable"


def test_financial_sector_returns_422_with_explanation(client: TestClient):
    response = client.get(f"/v1/valuations/JPM?{VALID_QUERY}")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["field"] == "ticker"
    assert "Financial Services" in detail[0]["message"]
    assert response.json()["error"]["code"] == "unsupported_sector"


def test_terminal_growth_at_or_above_wacc_returns_422(client: TestClient):
    response = client.get(
        "/v1/valuations/AAPL?wacc=0.05&terminal_growth=0.05&ebit_margin=0.30&revenue_growth=0.05"
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["field"] == "terminal_growth"
    error = response.json()["error"]
    assert error["version"] == "1"
    assert error["code"] == "invalid_assumptions"
    assert error["fields"][0]["code"] == "invalid_value"
    assert error["request_id"] == response.headers["X-Request-ID"]
    UUID(error["request_id"])


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("wacc", "nan"),
        ("terminal_growth", "-inf"),
        ("ebit_margin", "inf"),
        ("tax_rate", "nan"),
        ("revenue_growth", "nan"),
    ],
)
def test_non_finite_query_values_return_field_level_422(
    client: TestClient, parameter: str, value: str
):
    query = {
        "wacc": "0.09",
        "terminal_growth": "0.025",
        "ebit_margin": "0.30",
        "tax_rate": "0.21",
        "revenue_growth": "0.05",
    }
    query[parameter] = value
    response = client.get("/v1/valuations/AAPL", params=query)
    assert response.status_code == 422
    assert response.json()["detail"] == [{"field": parameter, "message": "must be a finite number"}]


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("wacc", "0.51"),
        ("terminal_growth", "-0.11"),
        ("ebit_margin", "1.01"),
        ("tax_rate", "1.01"),
        ("revenue_growth", "0.51"),
    ],
)
def test_out_of_range_query_values_return_field_level_422(
    client: TestClient, parameter: str, value: str
):
    query = {
        "wacc": "0.09",
        "terminal_growth": "0.025",
        "ebit_margin": "0.30",
        "tax_rate": "0.21",
        "revenue_growth": "0.05",
    }
    query[parameter] = value
    response = client.get("/v1/valuations/AAPL", params=query)
    assert response.status_code == 422
    assert response.json()["detail"][0]["field"] == parameter


def test_negative_valuation_returns_warnings_without_clipping(client: TestClient):
    response = client.get(
        "/v1/valuations/AAPL",
        params={
            "wacc": "0.09",
            "terminal_growth": "0.025",
            "ebit_margin": "-1.0",
            "revenue_growth": "0.05",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intrinsic_value_per_share"] < 0
    assert body["warnings"]
    assert sum("without clipping" in warning for warning in body["warnings"]) == 2


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
    error = response.json()["error"]
    assert error["code"] == "request_validation_failed"
    assert {field["field"] for field in error["fields"]} == {
        "terminal_growth",
        "ebit_margin",
        "revenue_growth",
    }


def test_openapi_documents_error_models(client: TestClient):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    operation = response.json()["paths"]["/v1/valuations/{ticker}"]["get"]
    expected = {"400", "401", "403", "404", "422", "429", "500", "502", "503"}
    assert expected.issubset(operation["responses"])
    for status in expected:
        schema = operation["responses"][status]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/ErrorResponse")


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
    assert response.json()["error"]["code"] == "provider_unavailable"


def test_provider_auth_failure_returns_enveloped_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid key"})

    fmp = FMPClient(api_key="test-key", transport=httpx.MockTransport(handler))
    with TestClient(create_app(fmp_client=fmp)) as test_client:
        response = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "provider_auth_misconfigured"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_normalization_failure_returns_enveloped_502():
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        payload = load_fixture("AAPL")[endpoint]
        if endpoint == "income-statement":
            payload = [{**payload[0], "revenue": None}]
        return httpx.Response(200, json=payload)

    fmp = FMPClient(api_key="test-key", transport=httpx.MockTransport(handler))
    with TestClient(create_app(fmp_client=fmp)) as test_client:
        response = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "normalization_failed"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


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


def test_auth_is_not_required_until_configured():
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    with TestClient(create_app(fmp_client=fmp)) as test_client:
        response = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
    assert response.status_code == 200


def test_authenticated_valuation_request_succeeds():
    key = "dcf_live_testsecret"
    auth = APIKeyAuthenticator(
        [APIKeyAuthenticator.record_from_secret(key_id="customer-1", prefix="live", secret=key)],
        required=True,
    )
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    with TestClient(create_app(fmp_client=fmp, authenticator=auth)) as test_client:
        response = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}", headers={"X-API-Key": key})

    assert response.status_code == 200
    assert response.json()["ticker"] == "AAPL"


@pytest.mark.parametrize("header_value", [None, "", "not-a-key", "dcf_unknown_secret"])
def test_missing_malformed_or_unknown_api_key_returns_401(header_value: str | None):
    key = "dcf_live_testsecret"
    auth = APIKeyAuthenticator(
        [APIKeyAuthenticator.record_from_secret(key_id="customer-1", prefix="live", secret=key)],
        required=True,
    )
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    headers = {"X-API-Key": header_value} if header_value is not None else {}
    with TestClient(
        create_app(fmp_client=fmp, authenticator=auth, daily_rate_limit=1)
    ) as test_client:
        unauthorized = test_client.get(
            f"/v1/valuations/AAPL?{VALID_QUERY}",
            headers=headers,
        )
        authorized = test_client.get(
            f"/v1/valuations/AAPL?{VALID_QUERY}",
            headers={"X-API-Key": key},
        )

    assert unauthorized.status_code == 401
    assert unauthorized.headers["WWW-Authenticate"] == "ApiKey"
    assert unauthorized.json()["error"]["code"] == "invalid_api_key"
    assert key not in unauthorized.text
    assert authorized.status_code == 200
    assert authorized.headers["X-RateLimit-Remaining"] == "0"


@pytest.mark.parametrize(
    "record_kwargs",
    [
        {"revoked": True},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    ],
)
def test_revoked_or_expired_api_key_returns_401(record_kwargs: dict):
    key = "dcf_live_testsecret"
    auth = APIKeyAuthenticator(
        [
            APIKeyAuthenticator.record_from_secret(
                key_id="customer-1",
                prefix="live",
                secret=key,
                **record_kwargs,
            )
        ],
        required=True,
    )
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    with TestClient(create_app(fmp_client=fmp, authenticator=auth)) as test_client:
        response = test_client.get(
            f"/v1/valuations/AAPL?{VALID_QUERY}",
            headers={"X-API-Key": key},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert key not in response.text


def test_insufficient_scope_returns_403():
    key = "dcf_live_testsecret"
    auth = APIKeyAuthenticator(
        [
            APIKeyAuthenticator.record_from_secret(
                key_id="customer-1",
                prefix="live",
                secret=key,
                scopes={"usage:read"},
            )
        ],
        required=True,
    )
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    with TestClient(create_app(fmp_client=fmp, authenticator=auth)) as test_client:
        response = test_client.get(
            f"/v1/valuations/AAPL?{VALID_QUERY}",
            headers={"X-API-Key": key},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"
    assert key not in response.text


def test_public_endpoints_do_not_require_api_key_when_auth_is_enabled():
    key = "dcf_live_testsecret"
    auth = APIKeyAuthenticator(
        [APIKeyAuthenticator.record_from_secret(key_id="customer-1", prefix="live", secret=key)],
        required=True,
    )
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    with TestClient(create_app(fmp_client=fmp, authenticator=auth)) as test_client:
        assert test_client.get("/").status_code == 200
        assert test_client.get("/health").status_code == 200
        assert test_client.get("/openapi.json").status_code == 200


def test_valuation_requests_are_limited_per_day():
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    with TestClient(create_app(fmp_client=fmp, daily_rate_limit=2)) as test_client:
        first = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        second = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        blocked = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "2"
    assert first.headers["X-RateLimit-Remaining"] == "1"
    assert second.status_code == 200
    assert second.headers["X-RateLimit-Remaining"] == "0"
    assert blocked.status_code == 429
    assert blocked.headers["X-RateLimit-Limit"] == "2"
    assert blocked.headers["X-RateLimit-Remaining"] == "0"
    assert int(blocked.headers["Retry-After"]) > 0
    assert blocked.json()["error"]["code"] == "rate_limit_exceeded"
    assert blocked.json()["error"]["request_id"] == blocked.headers["X-Request-ID"]


def test_website_and_health_do_not_consume_valuation_limit():
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    with TestClient(create_app(fmp_client=fmp, daily_rate_limit=1)) as test_client:
        assert test_client.get("/").status_code == 200
        assert test_client.get("/health").status_code == 200
        allowed = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        blocked = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert allowed.status_code == 200
    assert allowed.headers["X-RateLimit-Remaining"] == "0"
    assert blocked.status_code == 429


def test_invalid_valuation_requests_count_against_daily_limit():
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    with TestClient(create_app(fmp_client=fmp, daily_rate_limit=1)) as test_client:
        invalid = test_client.get("/v1/valuations/AAPL?wacc=0.09")
        blocked = test_client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert invalid.status_code == 422
    assert invalid.headers["X-RateLimit-Remaining"] == "0"
    assert blocked.status_code == 429


def test_health_endpoint():
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    with TestClient(create_app(fmp_client=fmp)) as test_client:
        response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json()["model_version"] == MODEL_VERSION


def test_root_serves_customer_landing_page():
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    with TestClient(create_app(fmp_client=fmp)) as test_client:
        response = test_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "geolocation=()" in response.headers["Permissions-Policy"]
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "connect-src 'self'" in csp
    assert "Run valuation" in response.text
    assert "/v1/valuations/" in response.text
