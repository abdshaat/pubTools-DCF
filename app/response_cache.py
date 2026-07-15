"""Distributed valuation response cache (Phase 8 Slice B).

Caches the full `ValuationResponse` in Redis for a short window so repeat
requests for the same ticker + resolved assumptions skip the provider fetch
AND the DCF recompute entirely — closing Phase 7's "a 304 is quota-free but
not compute-free" caveat. Metering is unaffected: a cache hit is still an
origin request and still consumes quota (that logic lives in the middleware).

Key layout (see project-docs/IMPLEMENTATION_PLAN.md Phase 8):

    dcf:v1:resp:{TICKER}:{generation}:{fingerprint}

- `fingerprint` is a SHA-256 over the *resolved* assumptions (per-year growth
  expanded, defaults applied) plus the sensitivity flag and `model_version`,
  so every equivalent request form maps to one entry and a model bump can
  never serve stale math.
- `generation` comes from `dcf:v1:gen:{TICKER}` (absent -> "0"). Slice C's
  scheduled refresh rotates it after a successful DB promotion, making every
  cached assumption-variant for the ticker unreachable at once without
  needing to enumerate keys.
- `request_id`/`computed_at` are stripped before storing and re-injected
  fresh on a hit; the ETag already excludes exactly those two fields, so a
  hit reproduces the original ETag by construction.

Everything here is fail-open: any Redis problem means "compute normally".
"""

import hashlib
import json
from typing import Any

from . import MODEL_VERSION
from .models import Assumptions
from .redis_cache import (
    REDIS_KEY_PREFIX,
    RedisBackend,
    get_envelope,
    set_envelope,
)

# Aligned with Cache-Control s-maxage; a short window bounds how long a
# just-refreshed quote/snapshot can coexist with a cached response.
RESPONSE_CACHE_TTL_SECONDS = 60

# Fields that are per-request bookkeeping, not content. Must stay in sync
# with app/http_cache.py's ETag exclusions so a hit reproduces the ETag.
_PER_REQUEST_FIELDS = ("request_id", "computed_at")

_DEFAULT_GENERATION = "0"


def assumption_fingerprint(assumptions: Assumptions, *, sensitivity: bool) -> str:
    """Stable digest of the resolved request semantics.

    Built from the normalized `Assumptions` (scalar growth already expanded
    to a per-year tuple, defaults already applied), so every equivalent
    request form — reshuffled params, explicit-vs-default tax_rate, scalar
    vs pre-expanded growth — produces the same fingerprint.
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


def _generation_key(ticker: str) -> str:
    return f"{REDIS_KEY_PREFIX}gen:{ticker}"


def _response_key(ticker: str, generation: str, fingerprint: str) -> str:
    return f"{REDIS_KEY_PREFIX}resp:{ticker}:{generation}:{fingerprint}"


async def _current_generation(backend: RedisBackend, ticker: str) -> str:
    generation = await backend.get(_generation_key(ticker))
    return generation if generation else _DEFAULT_GENERATION


async def get_cached_response(
    backend: RedisBackend | None,
    *,
    ticker: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    """The cached response content (per-request fields absent), or None.

    Fail-open: any Redis error, corrupt envelope, or non-dict payload is a
    miss. Corrupt envelopes are deleted by `get_envelope`; a payload of the
    wrong shape is deleted here for the same self-healing behavior.
    """
    if backend is None:
        return None
    try:
        generation = await _current_generation(backend, ticker)
        key = _response_key(ticker, generation, fingerprint)
        envelope = await get_envelope(backend, key)
        if envelope is None:
            return None
        if not isinstance(envelope.data, dict) or any(
            field in envelope.data for field in _PER_REQUEST_FIELDS
        ):
            await backend.delete(key)
            return None
        return envelope.data
    except Exception:
        # Same broad fail-open as the fundamentals L2 (Slice A): no backend
        # misbehavior may surface on the valuation path.
        return None


async def store_response(
    backend: RedisBackend | None,
    *,
    ticker: str,
    fingerprint: str,
    content: dict[str, Any],
    stored_at: float,
) -> None:
    """Best-effort write of a successful response's content (200s only —
    callers must never cache errors here). `content` must already have the
    per-request fields excluded."""
    if backend is None:
        return
    try:
        generation = await _current_generation(backend, ticker)
        await set_envelope(
            backend,
            _response_key(ticker, generation, fingerprint),
            content,
            ttl_seconds=RESPONSE_CACHE_TTL_SECONDS,
            stored_at=stored_at,
        )
    except Exception:
        return
