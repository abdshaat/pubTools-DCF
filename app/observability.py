"""Structured logging and per-request telemetry.

Phase 11 Slice 2. Until now the service had no logging at all: a production
incident could only be diagnosed from a response body, and the round-trip
budget in the implementation plan could only be measured by a local probe.

Three pieces:

1. **Telemetry** — a per-request dictionary held in a context variable, so code
   far below the route (the provider client, the fundamentals cache, the
   Supabase client) can annotate the request that caused it without threading a
   logger through every signature. Same reasoning as `request_context`.
2. **Outbound counters** — an httpx event hook every external client installs,
   so "how many round trips did this request make" is answered by the code that
   makes them, not by a comment in a design document.
3. **One access log line per request**, JSON in production and readable
   locally, carrying the request id, route template, status, duration, and
   whatever the layers below recorded.

Redaction is not optional here. Logs travel to places code review does not
reach, so values are scrubbed centrally: API keys, bearer tokens, cookies,
magic-link codes, credential-bearing URLs, and email addresses. The route
*template* is logged rather than the path, because the path contains the
ticker the customer asked about.
"""

import json
import logging
import re
import sys
import time
import uuid
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx

from .raw_store import REDACTED, redact_url
from .request_context import current_request_id

logger = logging.getLogger("app.access")

# One random id per process, so log lines and metrics can be attributed to the
# instance that produced them. Serverless makes this load-bearing: metrics are
# instance-local, and "two instances shared one cached statement fetch" is only
# observable if two lines can be told apart. Not a secret — it identifies a
# process, not a deployment or a customer.
INSTANCE_ID = uuid.uuid4().hex[:8]

_TELEMETRY: ContextVar[dict[str, Any] | None] = ContextVar("pubtools_telemetry", default=None)

# Fields the access log always carries, in a stable order so lines diff cleanly.
_BASE_FIELDS = (
    "ts",
    "level",
    "event",
    "instance",
    "request_id",
    "method",
    "route",
    "status",
    "duration_ms",
)

_API_KEY_PATTERN = re.compile(r"\bdcf_[a-z]+_[A-Za-z0-9_\-]{6,}")
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|apikey|token)\s+[A-Za-z0-9._\-]{6,}")
_EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_URL_PATTERN = re.compile(r"https?://[^\s\"']+")
# Substring hints are for names that are secret however they are qualified
# (`session_token`, `supabase_service_role_key_present`, …).
_SECRET_KEY_SUBSTRINGS = (
    "secret",
    "password",
    "pepper",
    "authorization",
    "cookie",
    "verifier",
    "apikey",
    "api_key",
    "token",
)
# Exact names only. `code` and `key` are secret on their own (a magic-link code,
# an API key) but not as qualifiers: `error_code` and `status_code` are the
# fields the logs exist to show, and redacting them once made a live 422
# unreadable — found by live verification 2026-07-25.
_SECRET_KEY_EXACT = frozenset({"code", "key", "email", "state", "otp"})


def scrub(value: Any) -> Any:
    """Remove credentials and personal data from anything about to be logged."""
    if isinstance(value, str):
        text = _URL_PATTERN.sub(lambda match: redact_url(match.group(0)), value)
        text = _API_KEY_PATTERN.sub(REDACTED, text)
        text = _BEARER_PATTERN.sub(lambda match: f"{match.group(1)} {REDACTED}", text)
        return _EMAIL_PATTERN.sub(REDACTED, text)
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_secret_key(str(key)) else scrub(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SECRET_KEY_EXACT:
        return True
    return any(hint in lowered for hint in _SECRET_KEY_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Per-request telemetry
# ---------------------------------------------------------------------------


@contextmanager
def telemetry_scope() -> Iterator[dict[str, Any]]:
    """Collect telemetry for one request (or one scheduled run)."""
    fields: dict[str, Any] = {}
    token = _TELEMETRY.set(fields)
    try:
        yield fields
    finally:
        _TELEMETRY.reset(token)


def record(**fields: Any) -> None:
    """Attach facts to the current request's log line. No-op outside one."""
    current = _TELEMETRY.get()
    if current is not None:
        current.update(fields)


def increment(name: str, amount: int = 1) -> None:
    """Count something that can happen more than once per request."""
    current = _TELEMETRY.get()
    if current is not None:
        current[name] = int(current.get(name, 0)) + amount


def add_duration(name: str, milliseconds: float) -> None:
    """Accumulate time spent in one phase of the current request."""
    current = _TELEMETRY.get()
    if current is not None:
        current[name] = round(float(current.get(name, 0.0)) + milliseconds, 2)


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Time one phase of a request into its log line.

    This is the tracing this service actually needs: `duration_ms` says a
    request was slow, `t_statements_ms` / `t_price_ms` / `t_quota_ms` say which
    dependency made it slow — the question an operator asks next. Spans are
    additive per request, so a retry loop shows as accumulated time under one
    name rather than as several fragments.

    Deliberately dependency-free. Exporting to OpenTelemetry would add a
    runtime dependency and a collector endpoint to a project that keeps its
    runtime deps to httpx and FastAPI, so that is an owner decision, not a
    side effect of wanting timings (Phase 11 Slice 4).
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        add_duration(f"t_{name}_ms", (time.perf_counter() - started) * 1000.0)


def snapshot() -> dict[str, Any]:
    current = _TELEMETRY.get()
    return dict(current) if current is not None else {}


def outbound_counter(service: str) -> dict[str, list[Any]]:
    """httpx `event_hooks` that count one request's calls to `service`.

    Retries count separately, on purpose: three attempts against FMP cost three
    round trips, and a budget that hid that would be lying.
    """

    async def _count(response: httpx.Response) -> None:
        increment(f"{service}_calls")

    return {"response": [_count]}


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per line, scrubbed, with telemetry fields inlined."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(scrub(extra))
        request_id = payload.get("request_id") or current_request_id()
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["error"] = scrub(self.formatException(record.exc_info).splitlines()[-1])
        ordered = {key: payload[key] for key in _BASE_FIELDS if key in payload}
        ordered.update({key: value for key, value in payload.items() if key not in ordered})
        return json.dumps(ordered, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable equivalent for local development."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:<7} {scrub(record.getMessage())}"
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict) and extra:
            rendered = " ".join(f"{key}={value}" for key, value in scrub(extra).items())
            return f"{base} {rendered}"
        return base


def configure_logging(*, level: str = "INFO", log_format: str = "text") -> None:
    """Install exactly one handler on this app's logger tree.

    Idempotent: `create_app` runs per test in this suite, and duplicate
    handlers would multiply every line.
    """
    root = logging.getLogger("app")
    formatter: logging.Formatter = JsonFormatter() if log_format == "json" else TextFormatter()
    for handler in list(root.handlers):
        if getattr(handler, "_pubtools_managed", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler._pubtools_managed = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    # The app's own tree only; never reconfigure the root logger, which belongs
    # to whoever embeds this app.
    root.propagate = False


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

# Seconds. Chosen around the measured baseline (warm ~1.5 ms in-process, cold
# dominated by provider round trips) so the interesting range is not one bucket.
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_METRIC_HELP = {
    "dcf_http_requests_total": ("counter", "Requests by route template and status code."),
    "dcf_http_request_duration_seconds": ("histogram", "Request duration in seconds."),
    "dcf_cache_reads_total": ("counter", "Statement reads by the layer that answered."),
    "dcf_provider_calls_total": ("counter", "Outbound calls by external service."),
    "dcf_quota_rejections_total": ("counter", "Requests refused by the daily quota."),
    "dcf_errors_total": ("counter", "Error responses by stable error code."),
    "dcf_price_lookups_total": ("counter", "Live price lookups by outcome."),
    "dcf_instance_info": ("gauge", "Always 1; labels identify the reporting instance."),
}


class MetricsRegistry:
    """Process-local counters rendered in Prometheus text format.

    Instance-local on purpose. Each Vercel function instance keeps its own
    counters, so a scrape describes the instance that answered it, not the
    fleet — aggregation belongs to whatever collects the scrapes. Storing them
    in Redis would make a monitoring concern into a request-path dependency,
    which ADR-004 keeps Redis out of.
    """

    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self._duration_buckets: dict[float, int] = dict.fromkeys(_DURATION_BUCKETS, 0)
        self._duration_sum = 0.0
        self._duration_count = 0

    def increment(self, name: str, amount: int = 1, **labels: str) -> None:
        key = (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))
        self._counters[key] = self._counters.get(key, 0) + amount

    def observe_duration(self, seconds: float) -> None:
        self._duration_sum += seconds
        self._duration_count += 1
        for bucket in _DURATION_BUCKETS:
            if seconds <= bucket:
                self._duration_buckets[bucket] += 1

    def observe_request(
        self, *, route: str, status: int, duration_seconds: float, fields: Mapping[str, Any]
    ) -> None:
        """Fold one finished request into the counters.

        Reads the same telemetry the access log emits, so a metric and its log
        line can never disagree about what happened.
        """
        self.increment("dcf_http_requests_total", route=route, status=str(status))
        self.observe_duration(duration_seconds)

        cache = fields.get("cache")
        if isinstance(cache, str):
            self.increment("dcf_cache_reads_total", outcome=cache)
        for field, value in fields.items():
            if field.endswith("_calls") and isinstance(value, int):
                self.increment(
                    "dcf_provider_calls_total", amount=value, service=field[: -len("_calls")]
                )
        if fields.get("quota") == "exceeded":
            self.increment("dcf_quota_rejections_total")
        error_code = fields.get("error_code")
        if isinstance(error_code, str):
            self.increment("dcf_errors_total", code=error_code)
        price = fields.get("price")
        if isinstance(price, str):
            self.increment("dcf_price_lookups_total", result=price)

    def render(self) -> str:
        """Prometheus text exposition (version 0.0.4)."""
        lines: list[str] = [
            f"# HELP dcf_instance_info {_METRIC_HELP['dcf_instance_info'][1]}",
            "# TYPE dcf_instance_info gauge",
            f'dcf_instance_info{{instance="{INSTANCE_ID}"}} 1',
        ]
        by_name: dict[str, list[tuple[tuple[tuple[str, str], ...], int]]] = {}
        for (name, labels), value in self._counters.items():
            by_name.setdefault(name, []).append((labels, value))

        for name in sorted(by_name):
            kind, help_text = _METRIC_HELP.get(name, ("counter", ""))
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            for labels, value in sorted(by_name[name]):
                lines.append(f"{name}{_render_labels(labels)} {value}")

        name = "dcf_http_request_duration_seconds"
        kind, help_text = _METRIC_HELP[name]
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        # Buckets are stored cumulatively (every observation increments each
        # bucket it fits in), which is exactly Prometheus' `le` semantics.
        for bucket in _DURATION_BUCKETS:
            lines.append(f'{name}_bucket{{le="{bucket}"}} {self._duration_buckets[bucket]}')
        lines.append(f'{name}_bucket{{le="+Inf"}} {self._duration_count}')
        lines.append(f"{name}_sum {self._duration_sum:.6f}")
        lines.append(f"{name}_count {self._duration_count}")
        return "\n".join(lines) + "\n"


def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    rendered = ",".join(f'{key}="{_escape(value)}"' for key, value in labels)
    return "{" + rendered + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def log_request(
    *,
    method: str,
    route: str,
    status: int,
    duration_ms: float,
    extra: MutableMapping[str, Any] | None = None,
) -> None:
    """Emit the single access line for a finished request."""
    fields: dict[str, Any] = {
        "event": "http_request",
        "instance": INSTANCE_ID,
        "request_id": current_request_id(),
        "method": method,
        "route": route,
        "status": status,
        "duration_ms": round(duration_ms, 2),
    }
    fields.update(snapshot())
    if extra:
        fields.update(extra)
    level = logging.ERROR if status >= 500 else logging.INFO
    logger.log(level, "%s %s -> %s", method, route, status, extra={"fields": fields})
