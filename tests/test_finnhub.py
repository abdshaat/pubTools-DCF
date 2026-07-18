"""Finnhub client + quote normalization (ADR-008 Slice 1).

All transport behavior is driven through httpx.MockTransport — no network,
no real API key. Route wiring is deliberately absent in this slice, so these
tests cover the client and normalizer only.
"""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.exceptions import NormalizationError, ProviderAuthError, ProviderError, TickerNotFoundError
from app.normalization import normalize_finnhub_quote
from app.providers.finnhub import FinnhubClient, FinnhubConfig

QUOTE = {
    "c": 190.25,
    "d": 1.1,
    "dp": 0.6,
    "h": 191.0,
    "l": 188.4,
    "o": 189.0,
    "pc": 189.15,
    "t": 1752696000,
}


def _client(handler) -> FinnhubClient:
    return FinnhubClient(api_key="test-token", transport=httpx.MockTransport(handler))


async def _fetch(handler, ticker: str = "AAPL"):
    async with _client(handler) as client:
        return await client.fetch_quote(ticker)


# --- config -----------------------------------------------------------------


def test_from_env_returns_none_when_key_absent(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    assert FinnhubConfig.from_env() is None


def test_from_env_returns_none_for_blank_key(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "   ")
    assert FinnhubConfig.from_env() is None


def test_from_env_reads_the_key(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "abc123")
    config = FinnhubConfig.from_env()
    assert config is not None
    assert config.api_key == "abc123"


def test_client_requires_a_key(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    with pytest.raises(ProviderAuthError):
        FinnhubClient()


# --- fetch_quote ------------------------------------------------------------


def test_fetch_quote_returns_payload_and_utc_fetched_at():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=QUOTE)

    payload, fetched_at = asyncio.run(_fetch(handler, "aapl"))

    assert payload == QUOTE
    assert fetched_at.tzinfo is UTC
    # Ticker is uppercased and the token is sent as a query param.
    assert seen["params"] == {"symbol": "AAPL", "token": "test-token"}


def test_all_zero_quote_means_unknown_symbol():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0}
        )

    with pytest.raises(TickerNotFoundError):
        asyncio.run(_fetch(handler, "NOSUCH"))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (429, ProviderError),
        (500, ProviderError),
        (503, ProviderError),
        (418, ProviderError),
    ],
)
def test_error_statuses_are_classified(status, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with pytest.raises(expected):
        asyncio.run(_fetch(handler))


def test_transport_errors_become_provider_errors_without_leaking_the_token():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(ProviderError) as excinfo:
        asyncio.run(_fetch(handler))
    assert "test-token" not in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    [
        {"content": b"not json"},
        {"json": ["not", "a", "dict"]},
    ],
    ids=["malformed-json", "non-dict-json"],
)
def test_malformed_payloads_become_provider_errors(body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, **body)

    with pytest.raises(ProviderError):
        asyncio.run(_fetch(handler))


# --- normalize_finnhub_quote ------------------------------------------------


def test_normalize_maps_c_to_price_and_t_to_price_as_of():
    fetched_at = datetime(2026, 7, 17, 15, 30, tzinfo=UTC)
    quote = normalize_finnhub_quote("AAPL", QUOTE, fetched_at)
    assert quote.price == pytest.approx(190.25)
    assert quote.price_as_of == datetime.fromtimestamp(1752696000, tz=UTC)
    assert quote.fetched_at == fetched_at


def test_normalize_treats_zero_t_as_no_timestamp():
    quote = normalize_finnhub_quote("AAPL", {"c": 10.0, "t": 0}, datetime.now(UTC))
    assert quote.price_as_of is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"c": None},
        {"c": "not-a-number"},
        {"c": float("nan")},
        {"c": float("inf")},
        {"c": -5.0},
        {"c": 0.0},
    ],
)
def test_normalize_rejects_missing_or_invalid_price(payload):
    with pytest.raises(NormalizationError):
        normalize_finnhub_quote("AAPL", payload, datetime.now(UTC))


def test_normalize_rejects_unusable_timestamp():
    with pytest.raises(NormalizationError):
        normalize_finnhub_quote("AAPL", {"c": 10.0, "t": "yesterday"}, datetime.now(UTC))
