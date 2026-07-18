"""Live smoke test against the real FMP API (needs FMP_API_KEY set).

Usage:
    python scripts/smoke_fetch.py AAPL

Fetches + normalizes one ticker, runs a DCF with placeholder assumptions,
and prints the result. Raw provider responses are saved under data/raw/
for auditing.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.dcf_engine import compute_dcf
from app.fundamentals import FundamentalsService
from app.models import Assumptions
from app.providers.fmp import FileRawSink, FMPClient


async def main(ticker: str) -> None:
    raw_sink = FileRawSink(Path(__file__).parent.parent / "data" / "raw")
    async with FMPClient(raw_sink=raw_sink) as client:
        service = FundamentalsService(client)
        base = await service.get_base_financials(ticker)

    print(f"--- {base.ticker} base financials ({base.source_period}) ---")
    for field in (
        "revenue",
        "ebit",
        "da",
        "capex",
        "delta_nwc",
        "net_debt",
        "diluted_shares",
    ):
        print(f"  {field:>16}: {getattr(base, field):,.0f}")

    assumptions = Assumptions(
        wacc=0.09,
        terminal_growth=0.025,
        tax_rate=0.21,
        ebit_margin=base.ebit / base.revenue,  # hold base-year margin
        projection_years=5,
        revenue_growth=0.05,
    )
    valuation = compute_dcf(base, assumptions)

    print("--- valuation (placeholder assumptions) ---")
    print(f"  intrinsic value/share: {valuation.intrinsic_value_per_share:,.2f}")
    print("  (market price/upside are live-from-Finnhub API-layer concerns; ADR-008)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "AAPL"))
