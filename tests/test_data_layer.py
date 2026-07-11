"""Tests for the ingestion (FMP client) and normalization layers.

All tests run against httpx.MockTransport with recorded fixture payloads —
no network, no API key. This doubles as CLAUDE.md milestone 3 (fixture-
backed data layer) while exercising the real milestone-4 client code.
"""

import asyncio
import json
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
from app.normalization import normalize_fmp_fundamentals
from app.providers.fmp import FMPClient, FMPFundamentals

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
    return FMPFundamentals(ticker=ticker, **sections)


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


def test_normalized_output_feeds_straight_into_dcf_engine():
    base = normalize_fmp_fundamentals(make_fundamentals())
    assumptions = Assumptions(
        wacc=0.09, terminal_growth=0.025, tax_rate=0.16,
        ebit_margin=0.30, projection_years=5, revenue_growth=0.05,
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
    assert result.income["revenue"] == 391_035_000_000
    assert result.quote["price"] == 245.5


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
        async with make_client(
            transport=httpx.MockTransport(handler), max_retries=3
        ) as client:
            await client.fetch_fundamentals("ZZZQQQ")

    with pytest.raises(TickerNotCoveredError):
        asyncio.run(scenario())
    assert len(attempts) == 1


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
    assert len(attempts) == 1  # auth errors must not be retried


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
        async with make_client(
            transport=httpx.MockTransport(handler), sleep=fake_sleep
        ) as client:
            return await client.fetch_fundamentals("AAPL")

    result = asyncio.run(scenario())
    assert result.income["revenue"] == 391_035_000_000
    assert sleeps == [7.0, 7.0]  # honored Retry-After on both 429s


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


def test_raw_sink_receives_every_payload():
    stored = []

    async def scenario():
        client = make_client(
            transport=fixture_transport(),
            raw_sink=lambda ticker, endpoint, payload: stored.append(
                (ticker, endpoint)
            ),
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


def test_cache_expires_after_ttl():
    call_log = []
    clock = {"t": 0.0}

    async def scenario():
        async with make_client(transport=fixture_transport(call_log)) as client:
            service = FundamentalsService(
                client, ttl_seconds=3600, now=lambda: clock["t"]
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
    assert len(call_log) == 10


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
    # which 402s immediately); the rest are served from the negative cache.
    assert len(call_log) == 1


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
    # Both requests hit the provider — a 503 might clear up, so it must stay
    # retryable and must not be cached.
    assert len(call_log) == 2


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
    assert calls_before_expiry == 1
    assert len(call_log) == 2
