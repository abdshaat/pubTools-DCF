"""Unit tests for app/http_cache.py: the pure ETag + If-None-Match logic.

Route-level 304/caching-header behavior is tested in tests/test_api.py.
"""

import dataclasses
from datetime import UTC, datetime

import pytest

from app.dcf_engine import compute_dcf
from app.http_cache import compute_etag, if_none_match_satisfied
from app.models import Assumptions
from app.schemas import build_valuation_response
from tests.conftest import make_base_financials

_ASSUMPTIONS = Assumptions(
    wacc=0.09,
    terminal_growth=0.025,
    tax_rate=0.21,
    ebit_margin=0.25,
    projection_years=5,
    revenue_growth=0.05,
)


def _response(base=None, assumptions=_ASSUMPTIONS, request_id="req", computed_at=None):
    base = base or make_base_financials()
    computed_at = computed_at or datetime(2026, 7, 12, tzinfo=UTC)
    valuation = compute_dcf(base, assumptions)
    return build_valuation_response(
        base, assumptions, valuation, None, request_id=request_id, computed_at=computed_at
    )


def test_etag_is_quoted_sha256():
    etag = compute_etag(_response())
    assert etag.startswith('"') and etag.endswith('"')
    assert len(etag) == 64 + 2  # 64 hex chars plus the surrounding quotes


def test_etag_ignores_request_id_and_computed_at():
    a = compute_etag(_response(request_id="req-a", computed_at=datetime(2026, 1, 1, tzinfo=UTC)))
    b = compute_etag(_response(request_id="req-b", computed_at=datetime(2030, 9, 9, tzinfo=UTC)))
    assert a == b


def test_etag_changes_when_assumptions_change():
    base_etag = compute_etag(_response())
    other = dataclasses.replace(_ASSUMPTIONS, wacc=0.10)
    assert compute_etag(_response(assumptions=other)) != base_etag


def test_etag_changes_when_current_price_changes():
    base_etag = compute_etag(_response())
    moved = dataclasses.replace(make_base_financials(), current_price=25.0)
    assert compute_etag(_response(base=moved)) != base_etag


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, False),
        ("", False),
        ('"abc"', True),
        ('"nope"', False),
        ("*", True),
        ('W/"abc"', True),  # weak validator prefix is stripped for comparison
        ('"x", "abc", "y"', True),  # comma-separated candidate list
        ('"x", "y"', False),
    ],
)
def test_if_none_match_satisfied(header, expected):
    assert if_none_match_satisfied(header, '"abc"') is expected
