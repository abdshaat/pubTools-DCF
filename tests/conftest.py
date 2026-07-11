import pytest

from app.models import BaseFinancials


def make_base_financials() -> BaseFinancials:
    """Round-number base case, easy to hand-verify in a spreadsheet.

    revenue=1,000,000 | D&A, capex, delta_nwc each a clean % of revenue
    so downstream ratios (used by the engine) are exact. BaseFinancials is
    frozen/immutable, so this is safe to call directly (bypassing the
    fixture system) from hypothesis @given tests, which disallow
    function-scoped fixtures.
    """
    return BaseFinancials(
        ticker="TEST",
        source_period="FY2025",
        revenue=1_000_000.0,
        ebit=250_000.0,
        da=50_000.0,  # 5% of revenue
        capex=60_000.0,  # 6% of revenue
        delta_nwc=10_000.0,  # 1% of revenue
        net_debt=200_000.0,
        diluted_shares=100_000.0,
        current_price=20.0,
        currency="USD",
        fundamentals_as_of="2025-12-31",
        fiscal_year="2025",
        statement_period="FY",
    )


@pytest.fixture
def base_financials() -> BaseFinancials:
    return make_base_financials()
