"""Raw provider evidence: capture records, redaction, and the audit sink.

Phase 10. Every normalized snapshot the API serves is derived from provider
bytes; this module keeps those bytes so a normalization bug can be replayed
against the exact payload that produced it, without spending another provider
call.

Design rules (ADR-009):

- **Capture is best effort.** Audit storage never fails a customer request:
  `FileRawSink.__call__` swallows and counts write failures, and the provider
  client guards any injected sink the same way.
- **Never on the event loop.** The async entry point hands the blocking
  compress/write/prune work to a worker thread.
- **Never overwrites.** Filenames carry a microsecond timestamp, a content
  hash, and a random suffix, and the file is published with an atomic
  `os.replace` from a temporary file in the same directory, so a partially
  written capture is never visible and two concurrent captures cannot collide.
- **No credentials.** URLs and headers are redacted before storage; the FMP key
  travels in the query string, so an unredacted URL would be a leaked secret.
- **Bounded.** Captures are gzipped and pruned by age and by count per
  ticker/endpoint, and the sink reports bytes written/reclaimed for cost
  monitoring.

Captures are evidence only. Nothing here is ever read on a customer request —
the serving path is L1 -> Redis -> Supabase snapshots -> FMP (ADR-006).
"""

import asyncio
import contextlib
import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

CAPTURE_SCHEMA_VERSION = 1
CAPTURE_SUFFIX = ".json.gz"
_TEMP_PREFIX = ".tmp-"
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S.%fZ"

DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_CAPTURES_PER_ENDPOINT = 25
DEFAULT_PRUNE_INTERVAL_SECONDS = 900.0

REDACTED = "REDACTED"

# Query parameters and headers whose values are credentials. FMP authenticates
# with `?apikey=`, so the request URL is secret-bearing by construction.
_SENSITIVE_QUERY_PARAMS = frozenset(
    {"apikey", "api_key", "key", "token", "access_token", "auth", "password", "secret", "signature"}
)
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "apikey",
        "x-auth-token",
        "x-csrf-token",
    }
)

# Ticker and endpoint become directory names. They arrive from a validated
# path parameter and a fixed endpoint table today, but the sink is the point
# where untrusted text would become a filesystem path, so it validates here
# rather than trusting its callers.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RawCaptureError(ValueError):
    """A capture could not be stored (unsafe component, unserializable body)."""


def redact_url(url: str) -> str:
    """Replace credential query values (and any userinfo) with a placeholder."""
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{REDACTED}@{netloc.rsplit('@', 1)[1]}"
    query = urlencode(
        [
            (key, REDACTED if key.lower() in _SENSITIVE_QUERY_PARAMS else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Copy headers with credential-bearing values replaced."""
    return {
        key: (REDACTED if key.lower() in _SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


@dataclass(frozen=True)
class RawCapture:
    """One provider response worth keeping, before it is written anywhere."""

    provider: str
    ticker: str
    endpoint: str
    payload: Any
    status_code: int
    url: str = ""
    request_headers: Mapping[str, str] = field(default_factory=dict)
    response_headers: Mapping[str, str] = field(default_factory=dict)
    elapsed_ms: float | None = None
    attempt: int = 1
    request_id: str | None = None
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def capture_document(capture: RawCapture) -> dict[str, Any]:
    """Build the stored envelope: metadata, content hash, redacted HTTP, body.

    The hash is taken over a canonical (sorted, separator-normalized) encoding
    of the payload alone, so the same provider body hashes identically across
    captures even though every capture is stored separately.
    """
    try:
        canonical = json.dumps(capture.payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise RawCaptureError(
            f"payload for {capture.endpoint}/{capture.ticker} is not JSON-serializable"
        ) from exc
    return {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "capture_id": uuid.uuid4().hex,
        "captured_at": capture.captured_at.astimezone(UTC).isoformat(),
        "provider": capture.provider,
        "ticker": capture.ticker.upper(),
        "endpoint": capture.endpoint,
        "request_id": capture.request_id,
        "attempt": capture.attempt,
        "content_sha256": hashlib.sha256(canonical).hexdigest(),
        "content_bytes": len(canonical),
        "http": {
            "status": capture.status_code,
            "url": redact_url(capture.url),
            "request_headers": redact_headers(capture.request_headers),
            "response_headers": redact_headers(capture.response_headers),
            "elapsed_ms": (
                None if capture.elapsed_ms is None else round(float(capture.elapsed_ms), 3)
            ),
        },
        "payload": capture.payload,
    }


@dataclass(frozen=True)
class StoredCapture:
    """A capture read back from disk."""

    path: Path
    document: dict[str, Any]

    @property
    def payload(self) -> Any:
        return self.document.get("payload")

    @property
    def ticker(self) -> str:
        return str(self.document.get("ticker", ""))

    @property
    def endpoint(self) -> str:
        return str(self.document.get("endpoint", ""))

    @property
    def content_sha256(self) -> str:
        return str(self.document.get("content_sha256", ""))

    @property
    def captured_at(self) -> datetime:
        raw = self.document.get("captured_at")
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw).astimezone(UTC)
            except ValueError:
                pass
        return datetime.fromtimestamp(self.path.stat().st_mtime, UTC)


@dataclass(frozen=True)
class RawSinkStats:
    """Cost/health counters for one sink instance (per process)."""

    captures_written: int = 0
    bytes_written: int = 0
    captures_pruned: int = 0
    bytes_pruned: int = 0
    failures: int = 0


@dataclass(frozen=True)
class PruneResult:
    captures_removed: int = 0
    bytes_removed: int = 0


@dataclass(frozen=True)
class UsageEntry:
    """Disk usage for one ticker/endpoint pair — the cost-monitoring unit."""

    ticker: str
    endpoint: str
    captures: int
    bytes: int
    oldest: datetime | None
    newest: datetime | None


def _safe_component(value: str, kind: str) -> str:
    component = value.strip()
    if not _SAFE_COMPONENT.match(component) or component in {".", ".."}:
        raise RawCaptureError(f"unsafe {kind} for raw capture: {value!r}")
    return component


def _restrict(path: Path, mode: int) -> None:
    """Best-effort owner-only permissions; POSIX enforces, Windows approximates."""
    with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
        os.chmod(path, mode)


def capture_timestamp(path: Path) -> datetime:
    """Capture time from the filename, falling back to the file's mtime."""
    stamp = path.name.split("_", 1)[0]
    try:
        return datetime.strptime(stamp, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)


class FileRawSink:
    """Local filesystem audit sink: `{root}/{TICKER}/{endpoint}/{capture}`.

    Local by design. Vercel functions are stateless and their filesystem is not
    durable, so `app.api._default_raw_sink` disables this sink there; a durable
    off-box backend is a separate slice (see Phase 10 in the implementation
    plan). Everything in this class is backend-agnostic apart from the writer,
    so that slice can reuse the record, redaction, and retention rules.
    """

    def __init__(
        self,
        root: Path,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_captures_per_endpoint: int = DEFAULT_MAX_CAPTURES_PER_ENDPOINT,
        prune_interval_seconds: float = DEFAULT_PRUNE_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        if max_captures_per_endpoint < 1:
            raise ValueError("max_captures_per_endpoint must be at least 1")
        if prune_interval_seconds < 0:
            raise ValueError("prune_interval_seconds must be non-negative")
        self._root = Path(root)
        self._retention = timedelta(days=retention_days)
        self._max_captures = max_captures_per_endpoint
        self._prune_interval = prune_interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._last_prune: dict[Path, float] = {}
        self._captures_written = 0
        self._bytes_written = 0
        self._captures_pruned = 0
        self._bytes_pruned = 0
        self._failures = 0

    async def __call__(self, capture: RawCapture) -> None:
        """Store one capture off the event loop; never raises to the caller."""
        try:
            await asyncio.to_thread(self.write, capture)
        except Exception:
            with self._lock:
                self._failures += 1
            logger.warning(
                "raw capture failed for %s/%s", capture.endpoint, capture.ticker, exc_info=True
            )

    def write(self, capture: RawCapture) -> Path:
        """Synchronously persist one capture; raises on failure."""
        ticker = _safe_component(capture.ticker.upper(), "ticker")
        endpoint = _safe_component(capture.endpoint, "endpoint")
        document = capture_document(capture)
        body = gzip.compress(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            mtime=0,
        )

        directory = self._root / ticker / endpoint
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            _restrict(directory, 0o700)
        target = directory / _capture_filename(document)
        temporary = directory / f"{_TEMP_PREFIX}{uuid.uuid4().hex}"
        try:
            with open(temporary, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            _restrict(temporary, 0o600)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

        with self._lock:
            self._captures_written += 1
            self._bytes_written += len(body)
        self._maybe_prune(directory)
        return target

    def stats(self) -> RawSinkStats:
        with self._lock:
            return RawSinkStats(
                captures_written=self._captures_written,
                bytes_written=self._bytes_written,
                captures_pruned=self._captures_pruned,
                bytes_pruned=self._bytes_pruned,
                failures=self._failures,
            )

    def _maybe_prune(self, directory: Path) -> None:
        """Prune this directory at most once per interval, per process."""
        now = self._clock()
        with self._lock:
            last = self._last_prune.get(directory)
            if last is not None and now - last < self._prune_interval:
                return
            self._last_prune[directory] = now
        try:
            result = self.prune_directory(directory)
        except OSError:  # pragma: no cover - filesystem race during cleanup
            logger.warning("raw capture prune failed for %s", directory, exc_info=True)
            return
        if result.captures_removed:
            logger.info(
                "pruned %d raw captures (%d bytes) from %s",
                result.captures_removed,
                result.bytes_removed,
                directory,
            )

    def prune_directory(self, directory: Path) -> PruneResult:
        """Delete captures past the age window, then past the count cap."""
        captures = sorted(directory.glob(f"*{CAPTURE_SUFFIX}"))
        if not captures:
            return PruneResult()
        cutoff = datetime.now(UTC) - self._retention
        expired = [path for path in captures if capture_timestamp(path) < cutoff]
        expired_paths = set(expired)
        surviving = [path for path in captures if path not in expired_paths]
        overflow = surviving[: max(len(surviving) - self._max_captures, 0)]

        removed = 0
        freed = 0
        for path in [*expired, *overflow]:
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:  # pragma: no cover - concurrent prune/delete
                continue
            removed += 1
            freed += size
        with self._lock:
            self._captures_pruned += removed
            self._bytes_pruned += freed
        return PruneResult(captures_removed=removed, bytes_removed=freed)

    def prune_all(self) -> PruneResult:
        """Apply the retention policy to every stored ticker/endpoint."""
        removed = 0
        freed = 0
        for directory in _capture_directories(self._root):
            result = self.prune_directory(directory)
            removed += result.captures_removed
            freed += result.bytes_removed
        return PruneResult(captures_removed=removed, bytes_removed=freed)


def _capture_filename(document: Mapping[str, Any]) -> str:
    """`{utc timestamp}_{content hash}_{capture id}.json.gz` — sorts by time."""
    captured_at = datetime.fromisoformat(str(document["captured_at"])).astimezone(UTC)
    stamp = captured_at.strftime(_TIMESTAMP_FORMAT)
    digest = str(document["content_sha256"])[:12]
    capture_id = str(document["capture_id"])[:8]
    return f"{stamp}_{digest}_{capture_id}{CAPTURE_SUFFIX}"


def _capture_directories(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for ticker_dir in sorted(root.iterdir()):
        if not ticker_dir.is_dir():
            continue
        for endpoint_dir in sorted(ticker_dir.iterdir()):
            if endpoint_dir.is_dir():
                yield endpoint_dir


def load_capture(path: Path) -> StoredCapture:
    """Read one capture. Legacy flat `{endpoint}_{epoch}.json` files (written
    before Phase 10) are wrapped in a schema-0 envelope so replay tooling can
    read old and new evidence with one code path."""
    if path.name.endswith(CAPTURE_SUFFIX):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            document = json.load(handle)
        if not isinstance(document, dict):
            raise RawCaptureError(f"capture at {path} is not an object")
        return StoredCapture(path=path, document=document)

    payload = json.loads(path.read_text(encoding="utf-8"))
    endpoint = path.stem.rsplit("_", 1)[0]
    return StoredCapture(
        path=path,
        document={
            "schema_version": 0,
            "captured_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            "provider": "fmp",
            "ticker": path.parent.name.upper(),
            "endpoint": endpoint,
            "payload": payload,
        },
    )


def iter_capture_paths(root: Path, ticker: str, endpoint: str) -> list[Path]:
    """Every stored capture path for one ticker/endpoint, oldest first."""
    ticker_dir = Path(root) / ticker.upper()
    if not ticker_dir.exists():
        return []
    current = sorted((ticker_dir / endpoint).glob(f"*{CAPTURE_SUFFIX}"))
    legacy = sorted(ticker_dir.glob(f"{endpoint}_*.json"))
    return [*legacy, *current]


def load_captures(root: Path, ticker: str, endpoint: str) -> list[StoredCapture]:
    return [load_capture(path) for path in iter_capture_paths(root, ticker, endpoint)]


def latest_capture(root: Path, ticker: str, endpoint: str) -> StoredCapture:
    """Newest stored capture for one ticker/endpoint.

    Raises LookupError when nothing is stored — replay must never silently
    substitute a different period or a live provider call.
    """
    paths = iter_capture_paths(root, ticker, endpoint)
    if not paths:
        raise LookupError(f"no stored capture for {endpoint}/{ticker.upper()} under {root}")
    return load_capture(paths[-1])


def usage_report(root: Path) -> list[UsageEntry]:
    """Per-ticker/endpoint capture count and byte usage, largest first."""
    entries: list[UsageEntry] = []
    for directory in _capture_directories(Path(root)):
        paths = sorted(directory.glob(f"*{CAPTURE_SUFFIX}"))
        if not paths:
            continue
        stamps = [capture_timestamp(path) for path in paths]
        entries.append(
            UsageEntry(
                ticker=directory.parent.name,
                endpoint=directory.name,
                captures=len(paths),
                bytes=sum(path.stat().st_size for path in paths),
                oldest=min(stamps),
                newest=max(stamps),
            )
        )
    entries.sort(key=lambda entry: entry.bytes, reverse=True)
    return entries
