import pytest

from app.models import BaseFinancials


@pytest.fixture(autouse=True)
def _no_ambient_service_env(monkeypatch):
    """Tests must not depend on the developer's external-service credentials.

    Without this, real SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY values in .env
    silently flip create_app() into real-auth mode for every test that doesn't
    explicitly configure Supabase, breaking assumptions baked into those tests.
    """
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("CRON_SECRET", raising=False)


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
        currency="USD",
        fundamentals_as_of="2025-12-31",
        fiscal_year="2025",
        statement_period="FY",
    )


@pytest.fixture
def base_financials() -> BaseFinancials:
    return make_base_financials()
