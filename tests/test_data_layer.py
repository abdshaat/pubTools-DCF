"""Tests for the ingestion (FMP client) and normalization layers.

All tests run against httpx.MockTransport with recorded fixture payloads —
no network, no API key. This doubles as CLAUDE.md milestone 3 (fixture-
backed data layer) while exercising the real milestone-4 client code.
"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.dcf_engine import compute_dcf
from app.exceptions import (
    NormalizationError,
    ProviderAuthError,
    ProviderError,
    TickerNotCoveredError,
    TickerNotFoundError,
    UnsupportedSectorError,
)
from app.fundamentals import FundamentalsService
from app.models import Assumptions
from app.normalization import normalize_fmp_fundamentals, normalize_fmp_quote
from app.providers.fmp import FMPClient, FMPFundamentals
from app.redis_cache import InMemoryRedisBackend

FIXTURES = Path(__file__).parent / "fixtures" / "fmp"


def load_fixture(ticker: str) -> dict:
    return json.loads((FIXTURES / f"{ticker}.json").read_text(encoding="utf-8"))


def fixture_transport(call_log: list | None = None) -> httpx.MockTransport:
    """Routes /{endpoint}?symbol=X to the matching section of X's fixture
    file; unknown tickers get FMP's real-world behavior (200 with [])."""

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        symbol = request.url.params.get("symbol", "")
        if call_log is not None:
            call_log.append((endpoint, symbol))
        fixture_path = FIXTURES / f"{symbol}.json"
        if not fixture_path.exists():
            return httpx.Response(200, json=[])
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        return httpx.Response(200, json=payload[endpoint])

    return httpx.MockTransport(handler)


def make_client(**kwargs) -> FMPClient:
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("transport", fixture_transport())
    return FMPClient(**kwargs)


def make_fundamentals(ticker: str = "AAPL", **overrides) -> FMPFundamentals:
    """Build an FMPFundamentals from fixture data, with per-section
    overrides for testing missing/odd fields."""
    raw = load_fixture(ticker)
    sections = {
        "income": raw["income-statement"][0],
        "balance": raw["balance-sheet-statement"][0],
        "cash_flow": raw["cash-flow-statement"][0],
        "profile": raw["profile"][0],
        "quote": raw["quote"][0],
    }
    for name, patch in overrides.items():
        sections[name] = {**sections[name], **patch}
    return FMPFundamentals(
        ticker=ticker,
        income=(sections["income"],),
        balance=(sections["balance"],),
        cash_flow=(sections["cash_flow"],),
        profile=sections["profile"],
        quote=sections["quote"],
    )


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_maps_fmp_fields_to_canonical_schema():
    base = normalize_fmp_fundamentals(make_fundamentals())

    assert base.ticker == "AAPL"
    assert base.source_period == "FY2025 (2025-09-27)"
    assert base.revenue == 391_035_000_000.0
    assert base.ebit == 123_216_000_000.0
    assert base.da == 11_445_000_000.0
    # FMP reports capex as a negative outflow; canonical schema is positive
    assert base.capex == 9_447_000_000.0
    # FMP changeInWorkingCapital is cash impact (+3,651M = NWC released
    # cash); canonical delta_nwc is the NWC increase, so sign flips
    assert base.delta_nwc == -3_651_000_000.0
    assert base.net_debt == 106_629_000_000.0 - 29_943_000_000.0
    assert base.diluted_shares == 15_408_095_000.0
    assert base.current_price == 245.5
    assert base.currency == "USD"
    assert base.fundamentals_as_of == "2025-09-27"
    assert base.price_as_of is None
    assert base.data_provider == "financialmodelingprep"


def test_normalize_rejects_financial_sector():
    with pytest.raises(UnsupportedSectorError) as exc:
        normalize_fmp_fundamentals(make_fundamentals("JPM"))
    assert exc.value.sector == "Financial Services"
    assert exc.value.ticker == "JPM"


def test_normalize_reports_all_missing_fields_by_name():
    broken = make_fundamentals(
        income={"revenue": None},
        cash_flow={"capitalExpenditure": None},
    )
    with pytest.raises(NormalizationError) as exc:
        normalize_fmp_fundamentals(broken)
    assert exc.value.missing == ["capex", "revenue"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "not-a-number"])
def test_normalize_rejects_non_finite_or_non_numeric_fields(value):
    with pytest.raises(NormalizationError) as exc:
        normalize_fmp_fundamentals(make_fundamentals(income={"revenue": value}))
    assert exc.value.missing == ["revenue"]


@pytest.mark.parametrize(
    ("section", "patch", "field"),
    [
        ("income", {"revenue": 0}, "revenue"),
        ("income", {"weightedAverageShsOutDil": 0}, "diluted_shares"),
        ("cash_flow", {"depreciationAndAmortization": -1}, "da"),
        ("quote", {"price": 0}, "current_price"),
    ],
)
def test_normalize_rejects_invalid_positive_domain_fields(section, patch, field):
    with pytest.raises(NormalizationError) as exc:
        normalize_fmp_fundamentals(make_fundamentals(**{section: patch}))
    assert exc.value.missing == [field]


def test_normalize_maps_optional_quote_timestamp():
    base = normalize_fmp_fundamentals(make_fundamentals(quote={"timestamp": 1_700_000_000}))
    assert base.price_as_of == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_normalize_rejects_invalid_quote_timestamp():
    with pytest.raises(NormalizationError) as exc:
        normalize_fmp_fundamentals(make_fundamentals(quote={"timestamp": "invalid"}))
    assert exc.value.missing == ["price_as_of"]


def test_normalize_requires_currency():
    with pytest.raises(NormalizationError) as exc:
        normalize_fmp_fundamentals(make_fundamentals(profile={"currency": None}))
    assert exc.value.missing == ["currency"]


def test_selects_newest_complete_annual_statement_set():
    base = make_fundamentals()
    newer_income = {**base.income[0], "date": "2026-09-26", "fiscalYear": 2026, "revenue": 999}
    newer_balance = {**base.balance[0], "date": "2026-09-26", "fiscalYear": 2026}
    newer_cash_flow = {**base.cash_flow[0], "date": "2026-09-26", "fiscalYear": 2026}
    fundamentals = replace(
        base,
        income=(base.income[0], newer_income),
        balance=(base.balance[0], newer_balance),
        cash_flow=(base.cash_flow[0], newer_cash_flow),
    )

    selected = normalize_fmp_fundamentals(fundamentals)
    assert selected.revenue == 999
    assert selected.fundamentals_as_of == "2026-09-26"
    assert selected.fiscal_year == "2026"


def test_newer_incomplete_period_is_not_mixed_into_older_complete_set():
    base = make_fundamentals()
    newer_income = {**base.income[0], "date": "2026-09-26", "fiscalYear": 2026, "revenue": 999}
    selected = normalize_fmp_fundamentals(replace(base, income=(newer_income, base.income[0])))

    assert selected.revenue == 391_035_000_000.0
    assert selected.fundamentals_as_of == "2025-09-27"
    assert any(
        "newer annual period was incomplete" in warning.lower()
        for warning in selected.data_quality_warnings
    )


def test_rejects_when_statement_dates_have_no_complete_intersection():
    base = make_fundamentals()
    mismatched = replace(
        base,
        balance=({**base.balance[0], "date": "2025-09-26"},),
        cash_flow=({**base.cash_flow[0], "date": "2025-09-25"},),
    )
    with pytest.raises(NormalizationError) as exc:
        normalize_fmp_fundamentals(mismatched)
    assert exc.value.missing == ["statement_alignment"]


def test_rejects_conflicting_fiscal_years_for_same_statement_date():
    base = make_fundamentals()
    mismatched = replace(base, balance=({**base.balance[0], "fiscalYear": 2024},))
    with pytest.raises(NormalizationError) as exc:
        normalize_fmp_fundamentals(mismatched)
    assert exc.value.missing == ["statement_alignment"]


def test_rejects_statement_currency_mismatch():
    base = make_fundamentals()
    mismatched = replace(base, cash_flow=({**base.cash_flow[0], "reportedCurrency": "EUR"},))
    with pytest.raises(NormalizationError) as exc:
        normalize_fmp_fundamentals(mismatched)
    assert exc.value.missing == ["statement_alignment", "currency"]


def test_selects_latest_accepted_restatement_for_same_period():
    base = make_fundamentals()
    original = {**base.income[0], "acceptedDate": "2025-10-30T10:00:00Z", "revenue": 100}
    restated = {**base.income[0], "acceptedDate": "2025-11-05T10:00:00Z", "revenue": 200}
    selected = normalize_fmp_fundamentals(replace(base, income=(original, restated)))

    assert selected.revenue == 200
    assert selected.accepted_at == "2025-11-05T10:00:00Z"
    assert any("restatement" in w.lower() for w in selected.data_quality_warnings)


def test_fiscal_year_offset_is_retained_from_income_statement():
    base = make_fundamentals()
    offset = replace(base, income=({**base.income[0], "fiscalYear": 2026},))
    selected = normalize_fmp_fundamentals(offset)
    assert selected.fiscal_year == "2026"
    assert selected.fundamentals_as_of == "2025-09-27"


def test_calendar_year_fallback_handles_provider_naming_drift():
    base = make_fundamentals()
    drifted = replace(
        base,
        income=({**base.income[0], "fiscalYear": None, "calendarYear": 2025},),
    )
    assert normalize_fmp_fundamentals(drifted).fiscal_year == "2025"


def test_diluted_share_class_field_is_used_instead_of_basic_shares():
    base = make_fundamentals()
    shares = replace(
        base,
        income=(
            {
                **base.income[0],
                "weightedAverageShsOut": 99,
                "weightedAverageShsOutDil": 123,
            },
        ),
    )
    assert normalize_fmp_fundamentals(shares).diluted_shares == 123


def test_missing_statement_dates_fail_safely():
    base = make_fundamentals()
    missing_dates = replace(
        base,
        income=({**base.income[0], "date": None},),
        balance=({**base.balance[0], "date": None},),
        cash_flow=({**base.cash_flow[0], "date": None},),
    )
    with pytest.raises(NormalizationError) as exc:
        normalize_fmp_fundamentals(missing_dates)
    assert exc.value.missing == ["statement_alignment"]


def test_normalized_output_feeds_straight_into_dcf_engine():
    base = normalize_fmp_fundamentals(make_fundamentals())
    assumptions = Assumptions(
        wacc=0.09,
        terminal_growth=0.025,
        tax_rate=0.16,
        ebit_margin=0.30,
        projection_years=5,
        revenue_growth=0.05,
    )
    valuation = compute_dcf(base, assumptions)
    assert valuation.intrinsic_value_per_share > 0
    assert len(valuation.projections) == 5


# ---------------------------------------------------------------------------
# FMP client (transport behavior)
# ---------------------------------------------------------------------------


def test_fetch_fundamentals_returns_all_five_sections():
    async def scenario():
        async with make_client() as client:
            return await client.fetch_fundamentals("aapl")  # case-insensitive

    result = asyncio.run(scenario())
    assert result.ticker == "AAPL"
    assert result.income[0]["revenue"] == 391_035_000_000
    assert result.quote["price"] == 245.5


def test_statement_fetch_requests_multiple_candidate_periods():
    observed_limits = []

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint in {
            "income-statement",
            "balance-sheet-statement",
            "cash-flow-statement",
        }:
            observed_limits.append(request.url.params.get("limit"))
        return httpx.Response(200, json=load_fixture("AAPL")[endpoint])

    async def scenario():
        async with make_client(transport=httpx.MockTransport(handler)) as client:
            await client.fetch_fundamentals("AAPL")

    asyncio.run(scenario())
    assert observed_limits == ["5", "5", "5"]


def test_provider_rejects_malformed_statement_record_list():
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        payload = [123] if endpoint == "income-statement" else load_fixture("AAPL")[endpoint]
        return httpx.Response(200, json=payload)

    async def scenario():
        async with make_client(transport=httpx.MockTransport(handler)) as client:
            await client.fetch_fundamentals("AAPL")

    with pytest.raises(ProviderError, match="malformed income-statement records"):
        asyncio.run(scenario())


def test_provider_accepts_single_statement_and_profile_objects():
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        payload = load_fixture("AAPL")[endpoint]
        if endpoint != "quote":
            payload = payload[0]
        return httpx.Response(200, json=payload)

    async def scenario():
        async with make_client(transport=httpx.MockTransport(handler)) as client:
            return await client.fetch_fundamentals("AAPL")

    result = asyncio.run(scenario())
    assert len(result.income) == 1
    assert result.profile["currency"] == "USD"


@pytest.mark.parametrize("payload", [[], "malformed"])
def test_independent_quote_fetch_rejects_empty_or_malformed_payload(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def scenario():
        async with make_client(transport=httpx.MockTransport(handler)) as client:
            await client.fetch_quote("AAPL")

    expected = TickerNotFoundError if payload == [] else ProviderError
    with pytest.raises(expected):
        asyncio.run(scenario())


def test_unknown_ticker_raises_ticker_not_found():
    async def scenario():
        async with make_client() as client:
            await client.fetch_fundamentals("ZZZZZZ")

    with pytest.raises(TickerNotFoundError) as exc:
        asyncio.run(scenario())
    assert exc.value.ticker == "ZZZZZZ"


def test_provider_402_raises_ticker_not_covered():
    # Live behavior (verified 2026-07-11): FMP's free tier returns
    # 402 Payment Required for symbols outside plan coverage (and for
    # symbols that don't exist). It is a distinct "not covered" condition,
    # not a plain not-found, and must never surface as a 500.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="Premium Query Parameter: ...")

    async def scenario():
        async with make_client(transport=httpx.MockTransport(handler)) as client:
            await client.fetch_fundamentals("ZZZQQQ")

    with pytest.raises(TickerNotCoveredError) as exc:
        asyncio.run(scenario())
    assert exc.value.ticker == "ZZZQQQ"


def test_provider_402_is_not_retried():
    # Retrying a 402 can't change the answer and wastes the daily call
    # budget — the first endpoint must fail immediately.
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(402, text="not available under your current subscription")

    async def scenario():
        async with make_client(transport=httpx.MockTransport(handler), max_retries=3) as client:
            await client.fetch_fundamentals("ZZZQQQ")

    with pytest.raises(TickerNotCoveredError):
        asyncio.run(scenario())
    assert len(attempts) == 5


def test_missing_api_key_fails_fast(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    with pytest.raises(ProviderAuthError):
        FMPClient(api_key=None)


def test_rejected_api_key_raises_auth_error_without_retry():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"error": "invalid key"})

    async def scenario():
        async with make_client(transport=httpx.MockTransport(handler)) as client:
            await client.fetch_fundamentals("AAPL")

    with pytest.raises(ProviderAuthError):
        asyncio.run(scenario())
    assert len(attempts) == 5  # concurrent endpoints fail once each, without retry


def test_retries_on_429_with_backoff_then_succeeds():
    attempts = []
    sleeps = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if len(attempts) <= 2:
            return httpx.Response(429, headers={"Retry-After": "7"})
        endpoint = request.url.path.rsplit("/", 1)[-1]
        payload = load_fixture("AAPL")
        return httpx.Response(200, json=payload[endpoint])

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario():
        async with make_client(transport=httpx.MockTransport(handler), sleep=fake_sleep) as client:
            return await client.fetch_fundamentals("AAPL")

    result = asyncio.run(scenario())
    assert result.income[0]["revenue"] == 391_035_000_000
    assert sleeps == [2.0, 2.0]  # capped Retry-After on both 429s


def test_invalid_retry_after_uses_bounded_jittered_backoff():
    sleeps = []
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "eventually"})
        endpoint = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=load_fixture("AAPL")[endpoint])

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario():
        async with make_client(
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
            jitter=lambda: 0.5,
        ) as client:
            await client.fetch_fundamentals("AAPL")

    asyncio.run(scenario())
    assert sleeps == [0.55]


def test_provider_classifies_malformed_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not-json")

    async def scenario():
        async with make_client(transport=httpx.MockTransport(handler)) as client:
            await client.fetch_fundamentals("AAPL")

    with pytest.raises(ProviderError, match="malformed JSON"):
        asyncio.run(scenario())


def test_raw_sink_failure_is_classified_as_provider_error():
    def sink(ticker: str, endpoint: str, payload: object) -> None:
        raise OSError("disk is full")

    async def scenario():
        async with make_client(raw_sink=sink) as client:
            await client.fetch_fundamentals("AAPL")

    with pytest.raises(ProviderError, match="raw provider payload sink failed"):
        asyncio.run(scenario())


def test_fetch_fundamentals_runs_endpoint_calls_concurrently_with_limit():
    active = 0
    max_active = 0
    call_log = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        endpoint = request.url.path.rsplit("/", 1)[-1]
        call_log.append(endpoint)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=load_fixture("AAPL")[endpoint])

    async def scenario():
        async with make_client(
            transport=httpx.MockTransport(handler),
            provider_concurrency=2,
        ) as client:
            await client.fetch_fundamentals("AAPL")

    asyncio.run(scenario())
    assert set(call_log) == {
        "income-statement",
        "balance-sheet-statement",
        "cash-flow-statement",
        "profile",
        "quote",
    }
    assert max_active == 2


def test_exhausted_retries_raise_provider_error():
    async def fake_sleep(seconds: float) -> None:
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def scenario():
        async with make_client(
            transport=httpx.MockTransport(handler), sleep=fake_sleep, max_retries=2
        ) as client:
            await client.fetch_fundamentals("AAPL")

    with pytest.raises(ProviderError, match="HTTP 503"):
        asyncio.run(scenario())


def test_transport_timeout_is_retried_then_classified():
    sleeps = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow provider", request=request)

    async def scenario():
        async with make_client(
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
            max_retries=1,
            jitter=lambda: 0,
        ) as client:
            await client.fetch_fundamentals("AAPL")

    with pytest.raises(ProviderError, match="transport error"):
        asyncio.run(scenario())
    assert sleeps == [0.5] * 5


def test_unsupported_http_status_is_classified_without_retry():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(418)

    async def scenario():
        async with make_client(transport=httpx.MockTransport(handler), max_retries=3) as client:
            await client.fetch_fundamentals("AAPL")

    with pytest.raises(ProviderError, match="unsupported HTTP 418"):
        asyncio.run(scenario())
    assert len(attempts) == 5


def test_raw_sink_receives_every_payload():
    stored = []

    async def scenario():
        client = make_client(
            transport=fixture_transport(),
            raw_sink=lambda ticker, endpoint, payload: stored.append((ticker, endpoint)),
        )
        async with client:
            await client.fetch_fundamentals("AAPL")

    asyncio.run(scenario())
    assert ("AAPL", "income-statement") in stored
    assert len(stored) == 5


# ---------------------------------------------------------------------------
# FundamentalsService (cache)
# ---------------------------------------------------------------------------


def test_cache_serves_second_request_without_provider_call():
    call_log = []

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = FundamentalsService(client)
            first = await service.get_base_financials("AAPL")
            calls_after_first = len(call_log)
            second = await service.get_base_financials("aapl")  # same ticker
            return first, second, calls_after_first

    first, second, calls_after_first = asyncio.run(scenario())
    assert first == second
    assert calls_after_first == 5
    assert len(call_log) == 5  # no additional provider traffic


def test_same_ticker_cold_burst_uses_one_provider_load():
    call_log = []

    async def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        call_log.append(endpoint)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=load_fixture("AAPL")[endpoint])

    async def scenario():
        async with make_client(transport=httpx.MockTransport(handler)) as client:
            service = FundamentalsService(client)
            return await asyncio.gather(*(service.get_base_financials("AAPL") for _ in range(10)))

    results = asyncio.run(scenario())
    assert {result.ticker for result in results} == {"AAPL"}
    assert len(call_log) == 5


def test_two_service_instances_share_redis_and_one_provider_load():
    call_log = []

    async def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        symbol = request.url.params.get("symbol", "")
        call_log.append((endpoint, symbol))
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=load_fixture("AAPL")[endpoint])

    async def scenario():
        redis = InMemoryRedisBackend()
        async with (
            make_client(transport=httpx.MockTransport(handler)) as first_client,
            make_client(transport=httpx.MockTransport(handler)) as second_client,
        ):
            first = FundamentalsService(
                first_client,
                redis=redis,
                redis_poll_seconds=1,
                redis_poll_interval_seconds=0.005,
            )
            second = FundamentalsService(
                second_client,
                redis=redis,
                redis_poll_seconds=1,
                redis_poll_interval_seconds=0.005,
            )
            first_task = asyncio.create_task(first.get_base_financials("AAPL"))
            await asyncio.sleep(0)
            second_task = asyncio.create_task(second.get_base_financials("AAPL"))
            return await asyncio.gather(first_task, second_task)

    first, second = asyncio.run(scenario())
    assert first == second
    assert len(call_log) == 5


def test_corrupt_distributed_fundamentals_entry_is_replaced_not_surfaced():
    call_log = []

    async def scenario():
        redis = InMemoryRedisBackend()
        await redis.set("dcf:v1:fund:AAPL", '{"v":1,"t":1,"d":{"bad":true}}', ex=3600)
        async with make_client(transport=fixture_transport(call_log)) as client:
            result = await FundamentalsService(client, redis=redis).get_base_financials("AAPL")
        return result, await redis.get("dcf:v1:fund:AAPL")

    result, stored = asyncio.run(scenario())
    assert result.ticker == "AAPL"
    assert len(call_log) == 5
    assert stored is not None and '"ticker":"AAPL"' in stored


def test_redis_outage_fails_open_to_provider_fetch():
    class UnavailableRedis(InMemoryRedisBackend):
        async def get(self, key: str) -> str | None:
            raise OSError("redis unavailable")

        async def set(self, key: str, value: str, **kwargs) -> bool:
            raise OSError("redis unavailable")

    call_log = []

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            return await FundamentalsService(client, redis=UnavailableRedis()).get_base_financials(
                "AAPL"
            )

    result = asyncio.run(scenario())
    assert result.ticker == "AAPL"
    assert len(call_log) == 5


def test_crashed_distributed_lock_holder_never_blocks_provider_fallback():
    call_log = []

    async def scenario():
        redis = InMemoryRedisBackend()
        assert await redis.set("dcf:v1:lock:fund:AAPL", "crashed", px=45_000, nx=True)
        async with make_client(transport=fixture_transport(call_log)) as client:
            return await FundamentalsService(
                client,
                redis=redis,
                redis_poll_seconds=0,
            ).get_base_financials("AAPL")

    result = asyncio.run(scenario())
    assert result.ticker == "AAPL"
    assert len(call_log) == 5


def test_negative_result_is_shared_between_service_instances():
    call_log = []

    async def scenario():
        redis = InMemoryRedisBackend()
        async with make_client(transport=fixture_transport(call_log)) as first_client:
            first = FundamentalsService(first_client, redis=redis)
            with pytest.raises(TickerNotFoundError):
                await first.get_base_financials("UNKNOWN")
        calls_after_first = len(call_log)
        async with make_client(transport=fixture_transport(call_log)) as second_client:
            second = FundamentalsService(second_client, redis=redis)
            with pytest.raises(TickerNotFoundError):
                await second.get_base_financials("UNKNOWN")
        return calls_after_first

    calls_after_first = asyncio.run(scenario())
    assert calls_after_first == 5
    assert len(call_log) == calls_after_first


def test_distributed_stale_statement_is_used_only_after_refresh_failure():
    clock = {"t": 0.0}
    initial_calls = []
    refresh_calls = []

    def failing_handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        refresh_calls.append(endpoint)
        if endpoint == "income-statement":
            return httpx.Response(503)
        return httpx.Response(200, json=load_fixture("AAPL")[endpoint])

    async def no_sleep(seconds: float) -> None:
        pass

    async def scenario():
        redis = InMemoryRedisBackend(now=lambda: clock["t"])
        async with make_client(transport=fixture_transport(initial_calls)) as first_client:
            first = FundamentalsService(
                first_client,
                ttl_seconds=60,
                quote_ttl_seconds=3600,
                max_statement_staleness_seconds=3600,
                now=lambda: clock["t"],
                wall_now=lambda: clock["t"],
                redis=redis,
            )
            await first.get_base_financials("AAPL")

        clock["t"] = 61
        async with make_client(
            transport=httpx.MockTransport(failing_handler), max_retries=0, sleep=no_sleep
        ) as second_client:
            second = FundamentalsService(
                second_client,
                ttl_seconds=60,
                quote_ttl_seconds=3600,
                max_statement_staleness_seconds=3600,
                now=lambda: clock["t"],
                wall_now=lambda: clock["t"],
                redis=redis,
            )
            return await second.get_base_financials("AAPL")

    result = asyncio.run(scenario())
    assert len(initial_calls) == 5
    assert set(refresh_calls) == {
        "income-statement",
        "balance-sheet-statement",
        "cash-flow-statement",
    }
    assert any("bounded stale statement" in warning for warning in result.data_quality_warnings)


def test_waiter_cancellation_does_not_cancel_shared_provider_load():
    call_log = []

    async def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        call_log.append(endpoint)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=load_fixture("AAPL")[endpoint])

    async def scenario():
        async with make_client(transport=httpx.MockTransport(handler)) as client:
            service = FundamentalsService(client)
            first = asyncio.create_task(service.get_base_financials("AAPL"))
            await asyncio.sleep(0)
            second = asyncio.create_task(service.get_base_financials("AAPL"))
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            result = await second
            await asyncio.sleep(0)
            return result, service

    result, service = asyncio.run(scenario())
    assert result.ticker == "AAPL"
    assert len(call_log) == 5
    assert service._inflight == {}


def test_cache_expires_after_ttl():
    call_log = []
    clock = {"t": 0.0}

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = FundamentalsService(
                client,
                ttl_seconds=3600,
                quote_ttl_seconds=3600,
                now=lambda: clock["t"],
            )
            await service.get_base_financials("AAPL")
            clock["t"] = 3599.0
            await service.get_base_financials("AAPL")  # still cached
            calls_before_expiry = len(call_log)
            clock["t"] = 3601.0
            await service.get_base_financials("AAPL")  # refetch
            return calls_before_expiry

    calls_before_expiry = asyncio.run(scenario())
    assert calls_before_expiry == 5
    # Statement refresh reuses the independently fresh profile, then refreshes
    # the quote: 3 statement calls + 1 quote call instead of another full 5.
    assert len(call_log) == 9


def test_profile_refreshes_independently_without_refetching_statements():
    call_log = []
    clock = {"t": 0.0}

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = FundamentalsService(
                client,
                ttl_seconds=3600,
                profile_ttl_seconds=100,
                quote_ttl_seconds=3600,
                now=lambda: clock["t"],
            )
            await service.get_base_financials("AAPL")
            clock["t"] = 101.0
            return await service.get_base_financials("AAPL")

    refreshed = asyncio.run(scenario())
    assert refreshed.currency == "USD"
    assert len(call_log) == 6
    assert [endpoint for endpoint, _ in call_log].count("profile") == 2
    assert [endpoint for endpoint, _ in call_log].count("income-statement") == 1


def test_quote_refreshes_independently_without_refetching_statements():
    call_log = []
    clock = {"t": 0.0}

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = FundamentalsService(
                client,
                ttl_seconds=3600,
                quote_ttl_seconds=60,
                now=lambda: clock["t"],
            )
            await service.get_base_financials("AAPL")
            clock["t"] = 61.0
            return await service.get_base_financials("AAPL")

    refreshed = asyncio.run(scenario())
    assert refreshed.current_price == 245.5
    assert len(call_log) == 6
    assert [endpoint for endpoint, _ in call_log].count("quote") == 2
    assert [endpoint for endpoint, _ in call_log].count("income-statement") == 1


def test_quote_refresh_failure_uses_bounded_stale_quote_with_warning():
    call_log = []
    clock = {"t": 0.0}
    fail_quote = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        call_log.append((endpoint, request.url.params.get("symbol", "")))
        if endpoint == "quote" and fail_quote["value"]:
            return httpx.Response(503)
        return httpx.Response(200, json=load_fixture("AAPL")[endpoint])

    async def no_sleep(seconds: float) -> None:
        pass

    async def scenario():
        async with make_client(
            transport=httpx.MockTransport(handler), max_retries=0, sleep=no_sleep
        ) as client:
            service = FundamentalsService(
                client,
                ttl_seconds=3600,
                quote_ttl_seconds=60,
                max_quote_staleness_seconds=900,
                now=lambda: clock["t"],
            )
            await service.get_base_financials("AAPL")
            clock["t"] = 61.0
            fail_quote["value"] = True
            return await service.get_base_financials("AAPL")

    stale = asyncio.run(scenario())
    assert stale.current_price == 245.5
    assert any("bounded stale quote" in warning for warning in stale.data_quality_warnings)


def test_quote_refresh_failure_beyond_staleness_limit_raises():
    clock = {"t": 0.0}
    fail_quote = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "quote" and fail_quote["value"]:
            return httpx.Response(503)
        return httpx.Response(200, json=load_fixture("AAPL")[endpoint])

    async def no_sleep(seconds: float) -> None:
        pass

    async def scenario():
        async with make_client(
            transport=httpx.MockTransport(handler), max_retries=0, sleep=no_sleep
        ) as client:
            service = FundamentalsService(
                client,
                quote_ttl_seconds=60,
                max_quote_staleness_seconds=900,
                now=lambda: clock["t"],
            )
            await service.get_base_financials("AAPL")
            clock["t"] = 901.0
            fail_quote["value"] = True
            await service.get_base_financials("AAPL")

    with pytest.raises(ProviderError):
        asyncio.run(scenario())


def test_statement_refresh_failure_uses_bounded_stale_fundamentals_with_warning():
    clock = {"t": 0.0}
    fail_statements = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "income-statement" and fail_statements["value"]:
            return httpx.Response(503)
        return httpx.Response(200, json=load_fixture("AAPL")[endpoint])

    async def no_sleep(seconds: float) -> None:
        pass

    async def scenario():
        async with make_client(
            transport=httpx.MockTransport(handler), max_retries=0, sleep=no_sleep
        ) as client:
            service = FundamentalsService(
                client,
                ttl_seconds=60,
                quote_ttl_seconds=3600,
                max_statement_staleness_seconds=3600,
                now=lambda: clock["t"],
            )
            await service.get_base_financials("AAPL")
            clock["t"] = 61.0
            fail_statements["value"] = True
            return await service.get_base_financials("AAPL")

    stale = asyncio.run(scenario())
    assert stale.ticker == "AAPL"
    assert any("stale statement snapshot" in warning for warning in stale.data_quality_warnings)


def test_statement_refresh_failure_beyond_staleness_limit_raises():
    clock = {"t": 0.0}
    fail_statements = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "income-statement" and fail_statements["value"]:
            return httpx.Response(503)
        return httpx.Response(200, json=load_fixture("AAPL")[endpoint])

    async def no_sleep(seconds: float) -> None:
        pass

    async def scenario():
        async with make_client(
            transport=httpx.MockTransport(handler), max_retries=0, sleep=no_sleep
        ) as client:
            service = FundamentalsService(
                client,
                ttl_seconds=60,
                max_statement_staleness_seconds=3600,
                now=lambda: clock["t"],
            )
            await service.get_base_financials("AAPL")
            clock["t"] = 3601.0
            fail_statements["value"] = True
            await service.get_base_financials("AAPL")

    with pytest.raises(ProviderError):
        asyncio.run(scenario())


def test_invalidate_clears_statement_quote_and_negative_caches():
    service = FundamentalsService(make_client())
    service._cache["AAPL"] = (0.0, normalize_fmp_fundamentals(make_fundamentals()))
    service._quote_cache["AAPL"] = (
        0.0,
        normalize_fmp_quote("AAPL", {"price": 1}, datetime.now(UTC)),
    )
    raw = make_fundamentals()
    service._raw_cache["AAPL"] = (0.0, raw)
    service._profile_cache["AAPL"] = (0.0, raw.profile)
    service._negative["BAD"] = (0.0, TickerNotFoundError("BAD"))

    service.invalidate("aapl")
    assert "AAPL" not in service._cache
    assert "AAPL" not in service._quote_cache
    assert "AAPL" not in service._raw_cache
    assert "AAPL" not in service._profile_cache
    assert "BAD" in service._negative

    service.invalidate()
    assert not service._cache
    assert not service._quote_cache
    assert not service._raw_cache
    assert not service._profile_cache
    assert not service._negative


# ---------------------------------------------------------------------------
# FundamentalsService negative caching (protects the daily provider budget)
# ---------------------------------------------------------------------------


def _always_402_transport(call_log: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(402, text="not available under your current subscription")

    return httpx.MockTransport(handler)


def test_not_covered_ticker_is_negative_cached():
    call_log: list = []

    async def scenario():
        async with make_client(transport=_always_402_transport(call_log)) as client:
            service = FundamentalsService(client)
            for _ in range(3):
                with pytest.raises(TickerNotCoveredError):
                    await service.get_base_financials("ZZZQQQ")

    asyncio.run(scenario())
    # Three requests, but only the first reached the provider (one endpoint,
    # one logical load); the rest are served from the negative cache.
    assert len(call_log) == 5


def test_unsupported_sector_is_negative_cached():
    call_log: list = []

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = FundamentalsService(client)
            with pytest.raises(UnsupportedSectorError):
                await service.get_base_financials("JPM")
            calls_after_first = len(call_log)
            with pytest.raises(UnsupportedSectorError):
                await service.get_base_financials("JPM")
            return calls_after_first

    calls_after_first = asyncio.run(scenario())
    assert calls_after_first == 5
    # A bank fetched once then rejected on sector: the repeat costs 0 calls
    # instead of another 5.
    assert len(call_log) == 5


def test_transient_error_is_not_negative_cached():
    call_log: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append(1)
        return httpx.Response(503)

    async def no_sleep(seconds: float) -> None:
        pass

    async def scenario():
        async with make_client(
            transport=httpx.MockTransport(handler), sleep=no_sleep, max_retries=0
        ) as client:
            service = FundamentalsService(client)
            with pytest.raises(ProviderError):
                await service.get_base_financials("AAPL")
            with pytest.raises(ProviderError):
                await service.get_base_financials("AAPL")

    asyncio.run(scenario())
    # Both logical loads hit the provider — a 503 might clear up, so it must stay
    # retryable and must not be cached.
    assert len(call_log) == 10


def test_negative_cache_expires_after_ttl():
    call_log: list = []
    clock = {"t": 0.0}

    async def scenario():
        async with make_client(transport=_always_402_transport(call_log)) as client:
            service = FundamentalsService(client, ttl_seconds=3600, now=lambda: clock["t"])
            with pytest.raises(TickerNotCoveredError):
                await service.get_base_financials("ZZZQQQ")
            clock["t"] = 3599.0
            with pytest.raises(TickerNotCoveredError):
                await service.get_base_financials("ZZZQQQ")  # still cached
            calls_before_expiry = len(call_log)
            clock["t"] = 3601.0
            with pytest.raises(TickerNotCoveredError):
                await service.get_base_financials("ZZZQQQ")  # re-checked
            return calls_before_expiry

    calls_before_expiry = asyncio.run(scenario())
    assert calls_before_expiry == 5
    assert len(call_log) == 10


def test_negative_cache_has_independent_ttl():
    call_log: list = []
    clock = {"t": 0.0}

    async def scenario():
        async with make_client(transport=_always_402_transport(call_log)) as client:
            service = FundamentalsService(
                client,
                ttl_seconds=3600,
                negative_ttl_seconds=10,
                now=lambda: clock["t"],
            )
            with pytest.raises(TickerNotCoveredError):
                await service.get_base_financials("ZZZQQQ")
            clock["t"] = 11.0
            with pytest.raises(TickerNotCoveredError):
                await service.get_base_financials("ZZZQQQ")

    asyncio.run(scenario())
    assert len(call_log) == 10
