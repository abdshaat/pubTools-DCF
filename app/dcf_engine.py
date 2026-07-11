"""Pure, deterministic DCF engine.

compute_dcf() takes base financials + assumptions and returns a Valuation.
No I/O, no framework dependencies, no side effects — safe to unit test in
isolation and safe to call from any transport layer (FastAPI, CLI, batch job).

Simplifying assumptions used "for now" (flagged for the later correctness
discussion, per CLAUDE.md):
  - D&A, capex, and delta-NWC are each held at a constant ratio to revenue,
    using the base-year ratio observed in BaseFinancials. No independent
    per-line-item assumptions yet.
  - End-of-year discounting (no mid-year convention).
  - ebit_margin is a single value applied to every projection year (no
    per-year margin ramp).
"""

from dataclasses import replace
from typing import Optional

from .models import (
    Assumptions,
    BaseFinancials,
    SensitivityGrid,
    Valuation,
    YearProjection,
)


class DCFValidationError(ValueError):
    """Raised when assumptions fail validation. `field` identifies which
    input was invalid, so the API layer can map this to a 422 per-field
    error message.
    """

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def _validate(base: BaseFinancials, assumptions: Assumptions) -> None:
    if not (3 <= assumptions.projection_years <= 15):
        raise DCFValidationError("projection_years", "must be between 3 and 15")

    if len(assumptions.revenue_growth) != assumptions.projection_years:
        raise DCFValidationError(
            "revenue_growth",
            "must be a single value or one value per projection year",
        )

    if assumptions.wacc <= 0:
        raise DCFValidationError("wacc", "must be positive")

    if assumptions.terminal_growth >= assumptions.wacc:
        raise DCFValidationError(
            "terminal_growth", "must be less than wacc (Gordon growth formula)"
        )

    if assumptions.tax_rate < 0:
        raise DCFValidationError("tax_rate", "must not be negative")

    for g in assumptions.revenue_growth:
        if abs(g) > 0.5:
            raise DCFValidationError("revenue_growth", "each value must be within +/-50%")

    if base.revenue <= 0:
        raise DCFValidationError("revenue", "base-year revenue must be positive")

    if base.diluted_shares <= 0:
        raise DCFValidationError("diluted_shares", "must be positive")


def compute_dcf(base: BaseFinancials, assumptions: Assumptions) -> Valuation:
    _validate(base, assumptions)

    da_ratio = base.da / base.revenue
    capex_ratio = base.capex / base.revenue
    nwc_ratio = base.delta_nwc / base.revenue

    projections: list[YearProjection] = []
    prev_revenue = base.revenue
    pv_fcf_total = 0.0
    last_fcf = 0.0
    last_discount_factor = 1.0

    for year, growth in enumerate(assumptions.revenue_growth, start=1):
        revenue = prev_revenue * (1 + growth)
        ebit = revenue * assumptions.ebit_margin
        da = revenue * da_ratio
        capex = revenue * capex_ratio
        delta_nwc = revenue * nwc_ratio

        fcf = ebit * (1 - assumptions.tax_rate) + da - capex - delta_nwc
        discount_factor = 1.0 / ((1.0 + assumptions.wacc) ** year)
        pv_fcf = fcf * discount_factor

        projections.append(
            YearProjection(
                year=year,
                revenue=revenue,
                ebit=ebit,
                fcf=fcf,
                discount_factor=discount_factor,
                pv_fcf=pv_fcf,
            )
        )

        pv_fcf_total += pv_fcf
        prev_revenue = revenue
        last_fcf = fcf
        last_discount_factor = discount_factor

    terminal_value = (
        last_fcf * (1 + assumptions.terminal_growth)
        / (assumptions.wacc - assumptions.terminal_growth)
    )
    pv_terminal_value = terminal_value * last_discount_factor

    enterprise_value = pv_fcf_total + pv_terminal_value
    equity_value = enterprise_value - base.net_debt
    intrinsic_value_per_share = equity_value / base.diluted_shares
    upside_pct = (
        (intrinsic_value_per_share - base.current_price) / base.current_price * 100
        if base.current_price
        else 0.0
    )

    return Valuation(
        projections=tuple(projections),
        terminal_value=terminal_value,
        pv_terminal_value=pv_terminal_value,
        enterprise_value=enterprise_value,
        equity_value=equity_value,
        intrinsic_value_per_share=intrinsic_value_per_share,
        current_price=base.current_price,
        upside_pct=upside_pct,
    )


# Grid offsets per CLAUDE.md: WACC +/-1% x terminal growth +/-0.5%.
WACC_OFFSETS = (-0.01, 0.0, 0.01)
TERMINAL_GROWTH_OFFSETS = (-0.005, 0.0, 0.005)


def compute_sensitivity_grid(
    base: BaseFinancials, assumptions: Assumptions
) -> SensitivityGrid:
    """3x3 grid of intrinsic value per share around the caller's WACC and
    terminal growth. Pure like compute_dcf. The center cell always equals
    the point estimate; combinations that would break the Gordon formula
    (g >= WACC, or WACC <= 0) come back as None rather than erroring, so
    one bad corner doesn't cost the caller the whole grid.
    """
    # round() strips float-add artifacts (0.09 - 0.01 = 0.07999...) so the
    # echoed axis values are clean
    wacc_values = tuple(round(assumptions.wacc + o, 10) for o in WACC_OFFSETS)
    growth_values = tuple(
        round(assumptions.terminal_growth + o, 10) for o in TERMINAL_GROWTH_OFFSETS
    )

    rows: list[tuple[Optional[float], ...]] = []
    for wacc in wacc_values:
        row: list[Optional[float]] = []
        for growth in growth_values:
            if wacc <= 0 or growth >= wacc:
                row.append(None)
            else:
                shifted = replace(assumptions, wacc=wacc, terminal_growth=growth)
                row.append(compute_dcf(base, shifted).intrinsic_value_per_share)
        rows.append(tuple(row))

    return SensitivityGrid(
        wacc_values=wacc_values,
        terminal_growth_values=growth_values,
        per_share_values=tuple(rows),
    )
