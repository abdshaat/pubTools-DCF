"""Tests for structured logging and per-request telemetry (Phase 11 Slice 2).

Two things must hold for these logs to be worth having: an operator can explain
a request from one line without reproducing it, and nothing secret or personal
ever reaches that line.
"""

import asyncio
import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.auth import APIKeyAuthenticator
from app.observability import (
    JsonFormatter,
    ScrubbingFilter,
    TextFormatter,
    configure_logging,
    increment,
    log_request,
    record,
    scrub,
    snapshot,
    stage,
    telemetry_scope,
)
from app.providers.fmp import FMPClient
from app.raw_store import REDACTED
from tests.test_data_layer import fixture_transport

VALID_QUERY = (
    "wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05&projection_years=5"
)


class CapturedLogs:
    """Collects records off the app's logger tree and renders them as JSON."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []

    @property
    def lines(self) -> list[dict]:
        formatter = JsonFormatter()
        return [json.loads(formatter.format(record)) for record in self.records]

    @property
    def access_lines(self) -> list[dict]:
        return [line for line in self.lines if line.get("event") == "http_request"]


@pytest.fixture
def logs():
    captured = CapturedLogs()

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.records.append(record)

    logger = logging.getLogger("app")
    handler = _Handler()
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield captured
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "auth failed for dcf_live_9f8a7b6c5d4e3f2a1b",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6",
        "GET https://financialmodelingprep.com/stable/profile?symbol=AAPL&apikey=s3cretvalue",
        "magic link requested by investor@example.com",
    ],
)
def test_scrub_removes_credentials_and_personal_data(value):
    scrubbed = scrub(value)
    for secret in ("dcf_live_9f8a7b6c5d4e3f2a1b", "eyJhbGciOiJIUzI1NiIsInR5cCI6", "s3cretvalue"):
        assert secret not in scrubbed
    assert "investor@example.com" not in scrubbed


def test_scrub_masks_values_under_secret_looking_keys():
    scrubbed = scrub(
        {
            "api_key": "dcf_live_abcdef123456",
            "session_token": "abc.def.ghi",
            "email": "someone@example.com",
            "ticker": "AAPL",
            "duration_ms": 4.2,
        }
    )
    assert scrubbed["api_key"] == REDACTED
    assert scrubbed["session_token"] == REDACTED
    assert scrubbed["email"] == REDACTED
    # Non-secret operational fields survive, or the log says nothing useful.
    assert scrubbed["ticker"] == "AAPL"
    assert scrubbed["duration_ms"] == 4.2


def test_diagnostic_fields_are_not_mistaken_for_secrets():
    """`code` is a magic-link code; `error_code` is why a request failed.

    Redacting the second one made a live 422 unreadable during Slice 4
    verification — the logs exist to answer exactly that question.
    """
    scrubbed = scrub(
        {
            "code": "magic-link-code",
            "error_code": "invalid_email",
            "status_code": 422,
            "model_version": "0.2.0",
            "key": "dcf_live_abcdef123456",
            "cache_key": "dcf:v1:fund:AAPL",
        }
    )
    assert scrubbed["code"] == REDACTED
    assert scrubbed["key"] == REDACTED
    assert scrubbed["error_code"] == "invalid_email"
    assert scrubbed["status_code"] == 422
    assert scrubbed["model_version"] == "0.2.0"
    # A cache key names a ticker, not a credential, but "key" as a qualifier is
    # ambiguous enough that the value still goes through the string scrubber.
    assert "dcf:v1:fund:AAPL" in str(scrubbed["cache_key"])


def test_scrub_recurses_into_nested_structures():
    scrubbed = scrub({"outer": [{"password": "hunter2"}, "mail me at a@b.co"]})
    assert scrubbed["outer"][0]["password"] == REDACTED
    assert "a@b.co" not in scrubbed["outer"][1]


# ---------------------------------------------------------------------------
# Formatters and configuration
# ---------------------------------------------------------------------------


def test_json_formatter_emits_one_parseable_object():
    record_ = logging.LogRecord("app.access", logging.INFO, __file__, 1, "hello", None, None)
    record_.fields = {"event": "http_request", "status": 200, "ticker": "AAPL"}  # type: ignore[attr-defined]
    line = json.loads(JsonFormatter().format(record_))
    assert line["level"] == "INFO"
    assert line["event"] == "http_request"
    assert line["status"] == 200
    assert line["ticker"] == "AAPL"
    assert line["ts"]


def test_text_formatter_renders_fields_for_humans():
    record_ = logging.LogRecord("app.access", logging.INFO, __file__, 1, "GET / -> 200", None, None)
    record_.fields = {"status": 200, "duration_ms": 1.5}  # type: ignore[attr-defined]
    rendered = TextFormatter().format(record_)
    assert "GET / -> 200" in rendered
    assert "status=200" in rendered
    assert "duration_ms=1.5" in rendered


def test_configure_logging_does_not_stack_handlers():
    logger = logging.getLogger("app")
    for _ in range(5):
        configure_logging(level="INFO", log_format="json")
    managed = [h for h in logger.handlers if getattr(h, "_pubtools_managed", False)]
    assert len(managed) == 1


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_telemetry_is_scoped_to_one_request():
    with telemetry_scope():
        record(ticker="AAPL")
        increment("fmp_calls", 4)
        assert snapshot() == {"ticker": "AAPL", "fmp_calls": 4}
    assert snapshot() == {}


def test_telemetry_outside_a_request_is_a_no_op():
    record(ticker="AAPL")  # must not raise: scripts and the cron path have no scope
    increment("fmp_calls")
    assert snapshot() == {}


def test_concurrent_requests_do_not_share_telemetry():
    async def scenario():
        async def one(name: str, calls: int) -> dict:
            with telemetry_scope():
                record(ticker=name)
                await asyncio.sleep(0)
                increment("fmp_calls", calls)
                return snapshot()

        return await asyncio.gather(one("AAPL", 4), one("MSFT", 1))

    first, second = asyncio.run(scenario())
    assert first == {"ticker": "AAPL", "fmp_calls": 4}
    assert second == {"ticker": "MSFT", "fmp_calls": 1}


def test_log_request_carries_whatever_the_layers_recorded(logs):
    with telemetry_scope():
        record(cache="l1", ticker="AAPL")
        increment("supabase_calls", 3)
        log_request(method="GET", route="/v1/valuations/{ticker}", status=200, duration_ms=12.345)

    line = logs.access_lines[0]
    assert line["method"] == "GET"
    assert line["route"] == "/v1/valuations/{ticker}"
    assert line["status"] == 200
    assert line["duration_ms"] == 12.35
    assert line["cache"] == "l1"
    assert line["supabase_calls"] == 3


def test_server_errors_log_at_error_level(logs):
    with telemetry_scope():
        log_request(method="GET", route="/v1/valuations/{ticker}", status=503, duration_ms=1.0)
    assert logs.records[0].levelname == "ERROR"


# ---------------------------------------------------------------------------
# End to end through the app
# ---------------------------------------------------------------------------


def _app(**kwargs):
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    kwargs.setdefault("authenticator", APIKeyAuthenticator(required=False))
    return create_app(fmp_client=fmp, **kwargs)


def test_one_access_line_per_request_with_the_matching_request_id(logs):
    with TestClient(_app()) as client:
        response = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert response.status_code == 200
    assert len(logs.access_lines) == 1
    line = logs.access_lines[0]
    assert line["request_id"] == response.headers["X-Request-ID"]
    assert line["route"] == "/v1/valuations/{ticker}"
    assert line["status"] == 200
    assert line["duration_ms"] >= 0
    assert line["ticker"] == "AAPL"
    assert line["model_version"]


def test_the_log_records_where_the_data_came_from(logs):
    with TestClient(_app()) as client:
        client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    cold, warm = logs.access_lines
    # Cold: four FMP endpoint calls, answered by the provider.
    assert cold["cache"] == "provider"
    assert cold["fmp_calls"] == 4
    # Warm: the statement cache answered, so no provider round trip at all.
    assert warm["cache"] == "l1"
    assert "fmp_calls" not in warm


def test_the_raw_path_is_not_logged_but_the_ticker_is(logs):
    with TestClient(_app()) as client:
        client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    rendered = json.dumps(logs.access_lines[0])
    assert "/v1/valuations/AAPL" not in rendered
    assert "wacc=0.09" not in rendered
    assert logs.access_lines[0]["ticker"] == "AAPL"


def test_rejected_requests_are_logged_too(logs):
    """The access log wraps the auth/quota gate, not the other way round."""
    auth = APIKeyAuthenticator([], required=True)
    with TestClient(_app(authenticator=auth)) as client:
        response = client.get(
            f"/v1/valuations/AAPL?{VALID_QUERY}", headers={"X-API-Key": "dcf_live_notarealkey"}
        )

    assert response.status_code == 401
    line = logs.access_lines[0]
    assert line["status"] == 401
    assert line["request_id"] == response.headers["X-Request-ID"]
    # Rejections happen before routing, so the route has to be resolved by
    # hand; without that they all collapse into "unmatched" and no one can tell
    # which endpoint is being refused.
    assert line["route"] == "/v1/valuations/{ticker}"
    # The presented key must not survive anywhere in the line.
    assert "dcf_live_notarealkey" not in json.dumps(line)


def test_quota_rejections_are_visible_in_the_log(logs):
    with TestClient(_app(daily_rate_limit=1)) as client:
        client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        second = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert second.status_code == 429
    assert logs.access_lines[-1]["quota"] == "exceeded"
    assert logs.access_lines[0]["quota"] == "allowed"


def test_unmatched_routes_log_a_bounded_route_label(logs):
    with TestClient(_app()) as client:
        client.get("/definitely-not-a-route")
    assert logs.access_lines[0]["route"] == "unmatched"
    assert logs.access_lines[0]["status"] == 404


def test_domain_errors_still_produce_one_line(logs):
    with TestClient(_app()) as client:
        response = client.get(f"/v1/valuations/NOSUCH?{VALID_QUERY}")
    assert response.status_code == 404
    assert len(logs.access_lines) == 1
    assert logs.access_lines[0]["status"] == 404


# ---------------------------------------------------------------------------
# Stage timings (the dependency-free half of tracing)
# ---------------------------------------------------------------------------


def test_stages_accumulate_into_the_request_line():
    with telemetry_scope():
        with stage("statements"):
            time.sleep(0.01)
        with stage("price"):
            pass
        fields = snapshot()

    assert fields["t_statements_ms"] >= 9
    assert fields["t_price_ms"] >= 0
    # A retry loop shows as one accumulated span, not several fragments.
    with telemetry_scope():
        for _ in range(3):
            with stage("provider"):
                time.sleep(0.002)
        assert snapshot()["t_provider_ms"] >= 5


def test_a_failing_stage_is_still_timed():
    with telemetry_scope():
        with pytest.raises(RuntimeError), stage("price"):
            raise RuntimeError("provider exploded")
        assert "t_price_ms" in snapshot()


def test_stages_outside_a_request_are_a_no_op():
    with stage("statements"):
        pass
    assert snapshot() == {}


def test_a_valuation_reports_where_its_time_went(logs):
    with TestClient(_app()) as client:
        client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    line = logs.access_lines[0]
    # Which dependency made a request slow is the question asked right after
    # "was it slow", so both live on the same line.
    assert line["t_statements_ms"] >= 0
    assert line["t_compute_ms"] >= 0
    assert line["duration_ms"] >= line["t_statements_ms"]


# ---------------------------------------------------------------------------
# Loggers this app makes emit but does not own
# ---------------------------------------------------------------------------
#
# `httpx` logs one `HTTP Request: <method> <url> "<status>"` line per outbound
# call, and FMP puts its key in the query string. Those records never reach the
# handler `configure_logging` installs, because that one sits on the `app` tree
# — so before 2026-07-26 the key was written to production logs verbatim. These
# tests pin the wiring, not just the scrubber: the leak was never in `scrub()`.


@pytest.fixture
def httpx_logs():
    """Capture `httpx`'s own records the way a foreign handler would render them."""
    configure_logging(level="INFO", log_format="json")
    foreign = logging.getLogger("httpx")
    rendered: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rendered.append(record.getMessage())

    handler = _Handler()
    previous_level = foreign.level
    foreign.setLevel(logging.INFO)
    foreign.addHandler(handler)
    try:
        yield rendered
    finally:
        foreign.removeHandler(handler)
        foreign.setLevel(previous_level)


def test_the_httpx_request_log_cannot_leak_the_provider_key(httpx_logs):
    secret = "fmp-live-key-9d41c0"
    client = FMPClient(api_key=secret, transport=fixture_transport())
    asyncio.run(client.fetch_fundamentals("AAPL"))
    asyncio.run(client.aclose())

    assert httpx_logs, "httpx should have logged its outbound calls"
    for line in httpx_logs:
        assert secret not in line
        assert f"apikey={REDACTED}" in line
        # The rest of the line has to survive, or the fix trades a leak for
        # blindness — this is the log that found the leak.
        assert "symbol=AAPL" in line
        assert '"HTTP/1.1 200 OK"' in line


def test_a_live_price_url_cannot_leak_the_finnhub_token(httpx_logs):
    logging.getLogger("httpx").info(
        'HTTP Request: %s %s "%s %d %s"',
        "GET",
        httpx.URL("https://finnhub.io/api/v1/quote?symbol=AAPL&token=fh-secret-value"),
        "HTTP/1.1",
        401,
        "Unauthorized",
    )

    line = httpx_logs[0]
    assert "fh-secret-value" not in line
    assert f"token={REDACTED}" in line
    # 401 is the fact that mattered in production on 2026-07-26; an argument
    # scrubbed into a string would have broken the `%d` and lost the whole line.
    assert "401 Unauthorized" in line


def test_scrubbing_a_record_leaves_non_string_arguments_alone():
    record_ = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%s took %d ms",
        args=("GET", 42),
        exc_info=None,
    )
    assert ScrubbingFilter().filter(record_) is True
    assert record_.args == ("GET", 42)
    assert record_.getMessage() == "GET took 42 ms"


def test_configure_logging_does_not_stack_foreign_filters():
    for _ in range(5):
        configure_logging(level="INFO", log_format="json")
    for name in ("httpx", "httpcore"):
        managed = [
            f for f in logging.getLogger(name).filters if getattr(f, "_pubtools_managed", False)
        ]
        assert len(managed) == 1, name
