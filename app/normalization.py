"""Normalization layer: FMP payloads -> canonical BaseFinancials.

Sign conventions (the part most likely to silently break — see the
"data normalization is the hardest part" warning in CLAUDE.md):

  - FMP `capitalExpenditure` is a cash OUTFLOW, reported negative.
    BaseFinancials.capex is a positive spend figure, so we take abs().
  - FMP `changeInWorkingCapital` is the cash-flow IMPACT of NWC movement
    (negative when working capital grew and consumed cash). The DCF engine
    subtracts delta_nwc as an INCREASE in NWC, so the sign is flipped:
    delta_nwc = -changeInWorkingCapital.

All figures from FMP are raw dollars, matching the project-wide unit
convention.
"""

from typing import Any

from .exceptions import NormalizationError, UnsupportedSectorError
from .models import BaseFinancials
from .providers.fmp import FMPFundamentals

# v1 scope gate: standard FCF DCF doesn't apply to banks/insurers.
FINANCIAL_SECTORS = {"Financial Services", "Financial", "Banking", "Insurance"}

_MISSING = object()


def _pick(payload: dict, *keys: str) -> Any:
    """Return the first present, non-None key from payload, else _MISSING.

    Multiple keys cover FMP's naming drift between API generations
    (e.g. fiscalYear vs calendarYear).
    """
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return _MISSING


def normalize_fmp_fundamentals(f: FMPFundamentals) -> BaseFinancials:
    sector = f.profile.get("sector")
    if sector in FINANCIAL_SECTORS:
        raise UnsupportedSectorError(f.ticker, sector)

    fields = {
        "revenue": _pick(f.income, "revenue"),
        "ebit": _pick(f.income, "operatingIncome"),
        "diluted_shares": _pick(f.income, "weightedAverageShsOutDil"),
        "da": _pick(f.cash_flow, "depreciationAndAmortization"),
        "capex": _pick(f.cash_flow, "capitalExpenditure"),
        "change_in_working_capital": _pick(f.cash_flow, "changeInWorkingCapital"),
        "total_debt": _pick(f.balance, "totalDebt"),
        "cash": _pick(f.balance, "cashAndCashEquivalents"),
        "current_price": _pick(f.quote, "price"),
    }

    missing = sorted(name for name, value in fields.items() if value is _MISSING)
    if missing:
        raise NormalizationError(f.ticker, missing)

    period = _pick(f.income, "period")
    fiscal_year = _pick(f.income, "fiscalYear", "calendarYear")
    date = _pick(f.income, "date")
    parts = [str(p) for p in (period, fiscal_year) if p is not _MISSING]
    source_period = "".join(parts) if parts else "unknown"
    if date is not _MISSING:
        source_period += f" ({date})"

    return BaseFinancials(
        ticker=f.ticker,
        source_period=source_period,
        revenue=float(fields["revenue"]),
        ebit=float(fields["ebit"]),
        da=float(fields["da"]),
        capex=abs(float(fields["capex"])),
        delta_nwc=-float(fields["change_in_working_capital"]),
        net_debt=float(fields["total_debt"]) - float(fields["cash"]),
        diluted_shares=float(fields["diluted_shares"]),
        current_price=float(fields["current_price"]),
    )
