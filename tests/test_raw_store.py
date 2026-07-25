"""Tests for the raw provider evidence store (Phase 10 / ADR-009).

Covers the properties the audit trail is supposed to guarantee: captures never
collide or appear half-written, credentials never reach disk, a sink failure
never reaches the customer, retention actually bounds the directory, and stored
evidence renormalizes offline to exactly what the live payload produced.
"""

import asyncio
import gzip
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.normalization import normalize_fmp_fundamentals
from app.providers.fmp import FMPFundamentals
from app.raw_store import (
    CAPTURE_SCHEMA_VERSION,
    CAPTURE_SUFFIX,
    REDACTED,
    FileRawSink,
    RawCapture,
    RawCaptureError,
    capture_document,
    latest_capture,
    load_captures,
    redact_headers,
    redact_url,
    usage_report,
)
from tests.test_data_layer import load_fixture

FMP_URL = (
    "https://financialmodelingprep.com/stable/income-statement?symbol=AAPL&apikey=s3cret&limit=5"
)


def make_capture(**overrides) -> RawCapture:
    defaults = dict(
        provider="fmp",
        ticker="AAPL",
        endpoint="income-statement",
        payload=[{"fiscalYear": "2025", "revenue": 416000000000}],
        status_code=200,
        url=FMP_URL,
        request_headers={"Authorization": "Bearer live-token", "accept": "application/json"},
        response_headers={"Set-Cookie": "session=abc", "content-type": "application/json"},
        elapsed_ms=12.3456,
        attempt=1,
        request_id="req-1",
    )
    defaults.update(overrides)
    return RawCapture(**defaults)


def read_document(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        document = json.load(handle)
    assert isinstance(document, dict)
    return document


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redact_url_hides_credential_query_values():
    redacted = redact_url(FMP_URL)
    assert "s3cret" not in redacted
    assert f"apikey={REDACTED}" in redacted
    # Non-credential parameters survive, so the evidence still says what was asked.
    assert "symbol=AAPL" in redacted
    assert "limit=5" in redacted


def test_redact_url_hides_userinfo():
    assert "hunter2" not in redact_url("https://user:hunter2@example.com/quote?symbol=AAPL")


def test_redact_headers_hides_authorization_and_cookies():
    redacted = redact_headers(
        {
            "Authorization": "Bearer x",
            "X-API-Key": "k",
            "Cookie": "a=b",
            "Accept": "application/json",
        }
    )
    assert redacted == {
        "Authorization": REDACTED,
        "X-API-Key": REDACTED,
        "Cookie": REDACTED,
        "Accept": "application/json",
    }


def test_stored_capture_contains_no_credentials(tmp_path):
    path = FileRawSink(tmp_path).write(make_capture())
    raw_bytes = path.read_bytes()
    assert b"s3cret" not in raw_bytes
    assert b"live-token" not in raw_bytes
    assert b"session=abc" not in raw_bytes
    document = read_document(path)
    assert document["http"]["request_headers"]["Authorization"] == REDACTED
    assert document["http"]["response_headers"]["Set-Cookie"] == REDACTED


# ---------------------------------------------------------------------------
# Capture document
# ---------------------------------------------------------------------------


def test_capture_document_records_full_provenance():
    document = capture_document(make_capture())
    assert document["schema_version"] == CAPTURE_SCHEMA_VERSION
    assert document["provider"] == "fmp"
    assert document["ticker"] == "AAPL"
    assert document["endpoint"] == "income-statement"
    assert document["request_id"] == "req-1"
    assert document["attempt"] == 1
    assert document["http"]["status"] == 200
    assert document["http"]["elapsed_ms"] == 12.346
    assert datetime.fromisoformat(document["captured_at"]).tzinfo is not None
    assert len(document["content_sha256"]) == 64
    assert document["payload"] == make_capture().payload


def test_content_hash_is_stable_across_captures_of_the_same_payload():
    first = capture_document(make_capture())
    second = capture_document(make_capture(request_id="req-2", attempt=3))
    assert first["content_sha256"] == second["content_sha256"]
    assert first["capture_id"] != second["capture_id"]


def test_content_hash_changes_when_the_provider_body_changes():
    first = capture_document(make_capture())
    second = capture_document(make_capture(payload=[{"fiscalYear": "2025", "revenue": 1}]))
    assert first["content_sha256"] != second["content_sha256"]


def test_unserializable_payload_is_rejected_before_any_write(tmp_path):
    sink = FileRawSink(tmp_path)
    with pytest.raises(RawCaptureError):
        sink.write(make_capture(payload={"when": object()}))
    assert list(tmp_path.rglob("*")) == []


# ---------------------------------------------------------------------------
# Writing: atomic, compressed, collision-proof
# ---------------------------------------------------------------------------


def test_captures_are_gzipped_and_round_trip(tmp_path):
    path = FileRawSink(tmp_path).write(make_capture())
    assert path.name.endswith(".json.gz")
    assert path.parent == tmp_path / "AAPL" / "income-statement"
    assert path.read_bytes()[:2] == b"\x1f\x8b"  # gzip magic
    assert read_document(path)["payload"] == make_capture().payload


def test_concurrent_captures_never_overwrite_each_other(tmp_path):
    sink = FileRawSink(tmp_path)

    async def scenario():
        await asyncio.gather(*(sink(make_capture(request_id=f"req-{n}")) for n in range(25)))

    asyncio.run(scenario())
    stored = load_captures(tmp_path, "AAPL", "income-statement")
    assert len(stored) == 25
    assert len({capture.document["request_id"] for capture in stored}) == 25
    assert sink.stats().captures_written == 25
    assert sink.stats().failures == 0


def test_capture_files_sort_chronologically(tmp_path):
    sink = FileRawSink(tmp_path)
    base = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)
    for offset in (2, 0, 1):
        sink.write(
            make_capture(captured_at=base + timedelta(minutes=offset), request_id=str(offset))
        )
    stored = load_captures(tmp_path, "AAPL", "income-statement")
    assert [capture.document["request_id"] for capture in stored] == ["0", "1", "2"]
    assert stored[-1].path == latest_capture(tmp_path, "AAPL", "income-statement").path


def test_a_failed_write_leaves_no_partial_or_temporary_file(tmp_path, monkeypatch):
    sink = FileRawSink(tmp_path)
    sink.write(make_capture())  # one good capture first

    def explode(source, target):
        raise OSError("no space left on device")

    monkeypatch.setattr("app.raw_store.os.replace", explode)
    with pytest.raises(OSError):
        sink.write(make_capture(request_id="doomed"))

    directory = tmp_path / "AAPL" / "income-statement"
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        path.name for path in directory.glob("*.json.gz")
    )
    stored = load_captures(tmp_path, "AAPL", "income-statement")
    assert len(stored) == 1
    assert stored[0].document["request_id"] == "req-1"


def test_write_runs_off_the_event_loop_thread(tmp_path):
    sink = FileRawSink(tmp_path)
    threads: dict[str, int] = {}
    original = sink.write

    def spy(capture: RawCapture) -> Path:
        threads["writer"] = threading.get_ident()
        return original(capture)

    sink.write = spy  # type: ignore[method-assign]

    async def scenario():
        threads["loop"] = threading.get_ident()
        await sink(make_capture())

    asyncio.run(scenario())
    assert threads["writer"] != threads["loop"]


def test_sink_swallows_and_counts_write_failures(tmp_path, monkeypatch):
    sink = FileRawSink(tmp_path)

    def explode(source, target):
        raise OSError("disk is full")

    monkeypatch.setattr("app.raw_store.os.replace", explode)
    asyncio.run(sink(make_capture()))

    assert sink.stats().failures == 1
    assert sink.stats().captures_written == 0
    assert load_captures(tmp_path, "AAPL", "income-statement") == []


def test_unsafe_path_components_are_refused(tmp_path):
    sink = FileRawSink(tmp_path)
    with pytest.raises(RawCaptureError):
        sink.write(make_capture(ticker="../../etc"))
    with pytest.raises(RawCaptureError):
        sink.write(make_capture(endpoint="../escape"))
    asyncio.run(sink(make_capture(ticker="../../etc")))  # async path stays quiet
    assert sink.stats().failures == 1
    assert not any(tmp_path.rglob("*.json.gz"))


# ---------------------------------------------------------------------------
# Retention and cost reporting
# ---------------------------------------------------------------------------


def test_retention_deletes_captures_older_than_the_window(tmp_path):
    # Writing sink keeps everything; a separately configured sink applies the
    # policy, so the test isolates retention from the write path.
    writer = FileRawSink(tmp_path, retention_days=365)
    now = datetime.now(UTC)
    for age_days in (30, 20, 8, 6, 1):
        writer.write(make_capture(captured_at=now - timedelta(days=age_days)))

    result = FileRawSink(tmp_path, retention_days=7).prune_all()
    assert result.captures_removed == 3
    assert result.bytes_removed > 0
    remaining = load_captures(tmp_path, "AAPL", "income-statement")
    assert len(remaining) == 2
    assert all(capture.captured_at > now - timedelta(days=7) for capture in remaining)


def test_retention_caps_captures_per_endpoint_keeping_the_newest(tmp_path):
    writer = FileRawSink(tmp_path)
    now = datetime.now(UTC)
    for minute in range(10):
        writer.write(
            make_capture(
                captured_at=now - timedelta(minutes=10 - minute), request_id=f"req-{minute}"
            )
        )

    pruner = FileRawSink(tmp_path, max_captures_per_endpoint=3)
    assert pruner.prune_all().captures_removed == 7
    assert pruner.stats().captures_pruned == 7
    remaining = load_captures(tmp_path, "AAPL", "income-statement")
    assert [capture.document["request_id"] for capture in remaining] == ["req-7", "req-8", "req-9"]


def test_prune_is_throttled_between_writes(tmp_path):
    ticks = iter([0.0, 10.0, 10_000.0])
    sink = FileRawSink(
        tmp_path,
        max_captures_per_endpoint=1,
        prune_interval_seconds=900,
        clock=lambda: next(ticks),
    )
    now = datetime.now(UTC)
    sink.write(make_capture(captured_at=now - timedelta(minutes=2)))
    sink.write(make_capture(captured_at=now - timedelta(minutes=1)))
    # Second write is inside the throttle window: nothing pruned yet.
    assert len(load_captures(tmp_path, "AAPL", "income-statement")) == 2
    sink.write(make_capture(captured_at=now))
    assert len(load_captures(tmp_path, "AAPL", "income-statement")) == 1


def test_usage_report_totals_captures_and_bytes(tmp_path):
    sink = FileRawSink(tmp_path)
    sink.write(make_capture())
    sink.write(make_capture(request_id="req-2"))
    sink.write(make_capture(endpoint="profile", payload={"symbol": "AAPL"}))
    sink.write(make_capture(ticker="MSFT"))

    report = usage_report(tmp_path)
    entries = {(entry.ticker, entry.endpoint): entry for entry in report}
    assert entries[("AAPL", "income-statement")].captures == 2
    assert entries[("AAPL", "profile")].captures == 1
    assert entries[("MSFT", "income-statement")].captures == 1
    assert sum(entry.bytes for entry in report) == sink.stats().bytes_written
    assert report == sorted(report, key=lambda entry: entry.bytes, reverse=True)


def test_invalid_retention_configuration_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        FileRawSink(tmp_path, retention_days=0)
    with pytest.raises(ValueError):
        FileRawSink(tmp_path, max_captures_per_endpoint=0)
    with pytest.raises(ValueError):
        FileRawSink(tmp_path, prune_interval_seconds=-1)


# ---------------------------------------------------------------------------
# Reading back: replay and legacy evidence
# ---------------------------------------------------------------------------


def test_stored_captures_renormalize_to_the_same_base_financials(tmp_path):
    """The replay guarantee: stored evidence reproduces the served numbers."""
    fixture = load_fixture("AAPL")
    sink = FileRawSink(tmp_path)
    for endpoint in ("income-statement", "balance-sheet-statement", "cash-flow-statement"):
        sink.write(make_capture(endpoint=endpoint, payload=fixture[endpoint]))
    sink.write(make_capture(endpoint="profile", payload=fixture["profile"]))

    def records(endpoint: str) -> tuple[dict, ...]:
        payload = latest_capture(tmp_path, "AAPL", endpoint).payload
        return tuple(payload) if isinstance(payload, list) else (payload,)

    profile = latest_capture(tmp_path, "AAPL", "profile").payload
    replayed = normalize_fmp_fundamentals(
        FMPFundamentals(
            ticker="AAPL",
            income=records("income-statement"),
            balance=records("balance-sheet-statement"),
            cash_flow=records("cash-flow-statement"),
            profile=profile[0] if isinstance(profile, list) else profile,
        )
    )
    live = normalize_fmp_fundamentals(
        FMPFundamentals(
            ticker="AAPL",
            income=tuple(fixture["income-statement"]),
            balance=tuple(fixture["balance-sheet-statement"]),
            cash_flow=tuple(fixture["cash-flow-statement"]),
            profile=fixture["profile"][0],
        )
    )
    assert replayed == live


def test_legacy_flat_captures_are_still_readable(tmp_path):
    """Pre-Phase-10 files (data/raw/AAPL/profile_1750000000.json) still replay."""
    legacy_dir = tmp_path / "AAPL"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "profile_1750000000.json").write_text(
        json.dumps([{"symbol": "AAPL", "sector": "Technology"}]), encoding="utf-8"
    )

    stored = load_captures(tmp_path, "AAPL", "profile")
    assert len(stored) == 1
    assert stored[0].document["schema_version"] == 0
    assert stored[0].payload == [{"symbol": "AAPL", "sector": "Technology"}]
    assert stored[0].endpoint == "profile"


def test_new_captures_take_precedence_over_legacy_files(tmp_path):
    legacy_dir = tmp_path / "AAPL"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "profile_1750000000.json").write_text(json.dumps({"old": True}), encoding="utf-8")
    FileRawSink(tmp_path).write(make_capture(endpoint="profile", payload={"old": False}))

    assert latest_capture(tmp_path, "AAPL", "profile").payload == {"old": False}
    assert len(load_captures(tmp_path, "AAPL", "profile")) == 2


def test_latest_capture_raises_when_no_evidence_exists(tmp_path):
    with pytest.raises(LookupError):
        latest_capture(tmp_path, "AAPL", "profile")


def test_stored_capture_exposes_its_identity(tmp_path):
    path = FileRawSink(tmp_path).write(make_capture())
    stored = load_captures(tmp_path, "AAPL", "income-statement")[0]
    assert stored.path == path
    assert stored.ticker == "AAPL"
    assert stored.endpoint == "income-statement"
    assert stored.content_sha256 == read_document(path)["content_sha256"]


# ---------------------------------------------------------------------------
# Robustness: damaged evidence must degrade, never crash the tooling
# ---------------------------------------------------------------------------


def test_capture_time_falls_back_to_the_file_when_metadata_is_damaged(tmp_path):
    """A renamed file or a corrupted timestamp still sorts and prunes."""
    original = FileRawSink(tmp_path).write(make_capture())
    document = read_document(original)
    document["captured_at"] = "not-a-timestamp"
    renamed = original.parent / f"renamed{CAPTURE_SUFFIX}"
    with gzip.open(renamed, "wt", encoding="utf-8") as handle:
        json.dump(document, handle)
    original.unlink()

    stored = load_captures(tmp_path, "AAPL", "income-statement")[0]
    mtime = datetime.fromtimestamp(renamed.stat().st_mtime, UTC)
    assert stored.captured_at == mtime
    # Retention still sees it: filename-derived time falls back to the mtime too.
    assert FileRawSink(tmp_path, retention_days=1).prune_all().captures_removed == 0


def test_a_capture_that_is_not_an_object_is_reported_clearly(tmp_path):
    directory = tmp_path / "AAPL" / "profile"
    directory.mkdir(parents=True)
    path = directory / f"20260725T180000.000000Z_deadbeef_1234{CAPTURE_SUFFIX}"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(["not", "an", "object"], handle)

    with pytest.raises(RawCaptureError):
        load_captures(tmp_path, "AAPL", "profile")


def test_stray_files_and_empty_directories_are_ignored(tmp_path):
    (tmp_path / "README.txt").write_text("not a ticker", encoding="utf-8")
    (tmp_path / "AAPL").mkdir()
    (tmp_path / "AAPL" / "stray.txt").write_text("not an endpoint", encoding="utf-8")
    (tmp_path / "AAPL" / "profile").mkdir()

    sink = FileRawSink(tmp_path)
    assert usage_report(tmp_path) == []
    assert sink.prune_all().captures_removed == 0
    assert usage_report(tmp_path / "missing") == []
