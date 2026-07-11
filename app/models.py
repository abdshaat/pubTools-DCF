"""Canonical data structures for the DCF engine.

All monetary fields are raw dollars (not thousands/millions) per the
unit-convention rule in CLAUDE.md.
"""

from dataclasses import dataclass
from typing import Optional, Sequence, Union


@dataclass(frozen=True)
class BaseFinancials:
    """Normalized base-year financials for one ticker, as produced by the
    normalization layer. This is the last actual (non-projected) period.
    """

    ticker: str
    source_period: str  # e.g. "FY2025Q4" / "TTM 2026-03-31"
    revenue: float
    ebit: float
    da: float  # depreciation & amortization
    capex: float
    delta_nwc: float  # change in net working capital, base year
    net_debt: float
    diluted_shares: float
    current_price: float


@dataclass(frozen=True)
class Assumptions:
    """Customer-supplied DCF assumptions. `revenue_growth` may be passed as
    a single float (broadcast across every projection year) or a sequence
    with one value per projection year; it is normalized to a tuple of
    length `projection_years` in __post_init__.
    """

    wacc: float
    terminal_growth: float
    tax_rate: float
    ebit_margin: float
    projection_years: int
    revenue_growth: Union[float, Sequence[float]]

    def __post_init__(self) -> None:
        if isinstance(self.revenue_growth, (int, float)):
            resolved = tuple(float(self.revenue_growth) for _ in range(self.projection_years))
        else:
            resolved = tuple(float(g) for g in self.revenue_growth)
        object.__setattr__(self, "revenue_growth", resolved)


@dataclass(frozen=True)
class YearProjection:
    year: int
    revenue: float
    ebit: float
    fcf: float
    discount_factor: float
    pv_fcf: float


@dataclass(frozen=True)
class SensitivityGrid:
    """Intrinsic value per share across WACC (rows) x terminal growth
    (columns) around the caller's assumptions. Cells where the combination
    is invalid (terminal growth >= WACC, or WACC <= 0) are None.
    """

    wacc_values: tuple[float, ...]
    terminal_growth_values: tuple[float, ...]
    per_share_values: tuple[tuple[Optional[float], ...], ...]


@dataclass(frozen=True)
class Valuation:
    projections: tuple[YearProjection, ...]
    terminal_value: float
    pv_terminal_value: float
    enterprise_value: float
    equity_value: float
    intrinsic_value_per_share: float
    current_price: float
    upside_pct: float
