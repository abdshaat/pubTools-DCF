"""Bounded-staleness valuation response cache (ADR-010).

Caches the full `ValuationResponse` content in Redis for a short window so a
repeat request for the same ticker + resolved assumptions skips the statement
lookup, the live price fetch, and the DCF recompute entirely.

**Read ADR-010 before changing anything here.** This module reverses ADR-008's
"the response is never cached, and no current price is cached anywhere" rule,
so it is off by default and the staleness it introduces is deliberate and
bounded:

- `VALUATION_CACHE_TTL_SECONDS` defaults to `0`, which means **disabled** — not
  a zero-second TTL but a hard short-circuit, so a deployment that leaves the
  default alone behaves exactly as it did before this module existed and never
  spends a Redis round trip finding that out.
- A non-zero TTL is the **whole** staleness bound. The cached body carries the
  price that was live when it was stored, so a hit can serve a price up to
  `ttl` seconds old. That is the trade ADR-010 records; `price_fetched_at` in
  the body tells the caller exactly how old it is.
- The TTL is re-checked locally against the stored envelope's age rather than
  trusted to Redis expiry alone, so **lowering** the setting takes effect on
  the next request instead of after the old entries drain.

Key layout:

    dcf:v1:resp:{TICKER}:{fingerprint}

`fingerprint` is a SHA-256 over the *resolved* assumptions (per-year growth
expanded, defaults applied) plus the sensitivity flag and `model_version`, so
every equivalent request form maps to one entry and a model bump can never
serve stale math.

The 2026-07-14 version of this module also carried a `generation` segment read
from `dcf:v1:gen:{TICKER}`, intended for a scheduled refresh to rotate. Nothing
ever rotated it, and reading it cost a Redis GET on **every** request — so it is
gone. With a short TTL, natural expiry *is* the invalidation mechanism: a daily
statement refresh becomes visible within `ttl` seconds. If a long TTL is ever
wanted, generation rotation comes back together with the rotator that justifies
it, not before.

`request_id` is per-request bookkeeping, not content: it is stripped before
storing and re-injected on a hit, so a cached answer still carries the id of
the request that actually received it and stays correlatable with the log line.

`computed_at` is deliberately **kept** — the 2026-07-14 version stripped and
re-stamped it, which was right when the only goal was reproducing an ETag, and
is wrong now. A hit serves math that really was computed earlier, so re-stamping
it to "now" would hide exactly the staleness ADR-010 asks us to state. Together
with `price_fetched_at` it lets a caller see the age of what they were handed
without having to know a cache exists.

Everything here is fail-open: any Redis problem means "compute normally".
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from . import MODEL_VERSION
from .models import Assumptions
from .redis_cache import (
    REDIS_KEY_PREFIX,
    RedisBackend,
    get_envelope,
    set_envelope,
)

# `0` disables the cache entirely. See the module docstring.
DEFAULT_VALUATION_CACHE_TTL_SECONDS = 0.0

# Per-request bookkeeping, excluded from the stored body and re-injected on
# read. `computed_at` is NOT here on purpose — see the module docstring.
_PER_REQUEST_FIELDS = ("request_id",)


@dataclass(frozen=True)
class CachedResponse:
    """A cache hit: the stored content plus how old it is."""

    content: dict[str, Any]
    age_seconds: float


def assumption_fingerprint(assumptions: Assumptions, *, sensitivity: bool) -> str:
    """Stable digest of the resolved request semantics.

    Built from the normalized `Assumptions` (scalar growth already expanded to
    a per-year tuple, defaults already applied), so every equivalent request
    form — reshuffled params, explicit-vs-default tax_rate, scalar vs
    pre-expanded growth — produces the same fingerprint.
    """
    canonical = json.dumps(
        {
            "wacc": assumptions.wacc,
            "terminal_growth": assumptions.terminal_growth,
            "tax_rate": assumptions.tax_rate,
            "ebit_margin": assumptions.ebit_margin,
            "projection_years": assumptions.projection_years,
            "revenue_growth": list(assumptions.resolved_revenue_growth),
            "sensitivity": sensitivity,
            "model_version": MODEL_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _response_key(ticker: str, fingerprint: str) -> str:
    return f"{REDIS_KEY_PREFIX}resp:{ticker}:{fingerprint}"


def strip_per_request_fields(content: dict[str, Any]) -> dict[str, Any]:
    """The storable view of a response body."""
    return {key: value for key, value in content.items() if key not in _PER_REQUEST_FIELDS}


async def get_cached_response(
    backend: RedisBackend | None,
    *,
    ticker: str,
    fingerprint: str,
    ttl_seconds: float,
    now: float,
) -> CachedResponse | None:
    """A cache hit, or None for a miss / disabled cache / any Redis trouble.

    Fail-open: a Redis error, a corrupt envelope, a non-dict payload, or a body
    that somehow carries per-request fields is a miss. Corrupt envelopes are
    deleted by `get_envelope`; a wrong-shaped payload is deleted here for the
    same self-healing behavior.
    """
    if backend is None or ttl_seconds <= 0:
        return None
    try:
        key = _response_key(ticker, fingerprint)
        envelope = await get_envelope(backend, key)
        if envelope is None:
            return None
        if not isinstance(envelope.data, dict) or any(
            field in envelope.data for field in _PER_REQUEST_FIELDS
        ):
            await backend.delete(key)
            return None
        age = max(0.0, now - envelope.stored_at)
        # Enforce the TTL locally too: Redis expiry was set from whatever the
        # TTL was at write time, so without this a lowered setting would not
        # bind until the old entries aged out on their own.
        if age >= ttl_seconds:
            return None
        return CachedResponse(content=envelope.data, age_seconds=age)
    except Exception:
        # Same broad fail-open as the fundamentals L2: no backend misbehavior
        # may surface on the valuation path.
        return None


async def store_response(
    backend: RedisBackend | None,
    *,
    ticker: str,
    fingerprint: str,
    content: dict[str, Any],
    ttl_seconds: float,
    stored_at: float,
) -> None:
    """Best-effort write of a successful response's content.

    200s only — callers must never cache an error. `content` must already have
    the per-request fields excluded (`strip_per_request_fields`).
    """
    if backend is None or ttl_seconds <= 0:
        return
    try:
        await set_envelope(
            backend,
            _response_key(ticker, fingerprint),
            content,
            ttl_seconds=max(1, int(ttl_seconds)),
            stored_at=stored_at,
        )
    except Exception:
        return
