"""HTTP caching primitives for the valuation endpoint (Phase 7).

The GET valuation endpoint is designed to be HTTP-cacheable. This module holds
the pure, I/O-free pieces of that: a deterministic ETag over a response's
*content*, and conditional-request (`If-None-Match`) matching.

Caching-audience decision (see project-docs/IMPLEMENTATION_PLAN.md Phase 7): valuation
responses are `Cache-Control: public`, so a shared CDN/edge cache may serve a
cached valuation to any caller of the same URL. A cache hit never reaches this
app's origin, so it never runs auth/quota -- accepted because a valuation is a
deterministic function of public financial data plus caller-supplied
assumptions, not confidential per-customer data. Consequently, per-caller
response headers (`X-RateLimit-*`) are NOT emitted on valuation responses, so a
shared cache can never serve one customer's quota state to another.
"""

import hashlib
import json

from .schemas import ValuationResponse

# max-age bounds browser reuse; s-maxage bounds CDN/edge reuse (Vercel honors
# s-maxage). ~60s roughly matches the quote-refresh cadence. Tunable later.
VALUATION_CACHE_CONTROL = "public, max-age=30, s-maxage=60, stale-while-revalidate=30"

# Response is not cacheable (errors, rate-limit rejections): never let a shared
# cache retain a per-request/transient valuation-path response.
NO_STORE = "no-store"

VALUATION_VARY = "Accept-Encoding"

# Per-request bookkeeping fields that must NOT affect the ETag: two otherwise
# identical valuations differing only in these should share one ETag.
_ETAG_EXCLUDED_FIELDS = frozenset({"request_id", "computed_at"})


def compute_etag(payload: ValuationResponse) -> str:
    """Strong ETag over the response content, excluding per-request bookkeeping.

    Because every content field (including `current_price`, `data_version`, and
    `model_version`) participates, the ETag changes automatically whenever
    anything a client can observe changes -- giving cache invalidation for
    quotes, restated financials, and model-version bumps "for free," with no
    separate purge mechanism. Returned already quoted, per RFC 9110.
    """
    content = payload.model_dump(mode="json", exclude=set(_ETAG_EXCLUDED_FIELDS))
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f'"{digest}"'


def _normalize_tag(tag: str) -> str:
    tag = tag.strip()
    # Strip the weak-validator prefix; If-None-Match uses weak comparison
    # (RFC 9110 s.13.1.2), and we only ever emit strong tags anyway.
    if tag.startswith("W/"):
        tag = tag[2:]
    return tag


def if_none_match_satisfied(header: str | None, etag: str) -> bool:
    """True when an `If-None-Match` request header means "not modified".

    Handles the `*` wildcard and a comma-separated list of candidate tags.
    """
    if not header:
        return False
    header = header.strip()
    if header == "*":
        return True
    ours = _normalize_tag(etag)
    return any(_normalize_tag(candidate) == ours for candidate in header.split(","))
