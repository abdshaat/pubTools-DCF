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
from math import isfinite

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


# Public v1 assumption bounds. These are model-safety limits, not recommended
# investment assumptions. Keep the API descriptions and customer docs aligned.
MIN_WACC = 0.001
MAX_WACC = 0.50
MIN_TERMINAL_GROWTH = -0.10
MAX_TERMINAL_GROWTH = 0.10
MIN_TAX_RATE = 0.0
MAX_TAX_RATE = 1.0
MIN_EBIT_MARGIN = -1.0
MAX_EBIT_MARGIN = 1.0
MIN_REVENUE_GROWTH = -0.50
MAX_REVENUE_GROWTH = 0.50


def _require_finite(field: str, value: float) -> None:
    if not isfinite(value):
        raise DCFValidationError(field, "must be a finite number")


def _validate_base(base: BaseFinancials) -> None:
    numeric_fields = {
        "revenue": base.revenue,
        "ebit": base.ebit,
        "da": base.da,
        "capex": base.capex,
        "delta_nwc": base.delta_nwc,
        "net_debt": base.net_debt,
        "diluted_shares": base.diluted_shares,
    }
    for field, value in numeric_fields.items():
        _require_finite(field, value)

    if base.revenue <= 0:
        raise DCFValidationError("revenue", "base-year revenue must be positive")
    if base.da < 0:
        raise DCFValidationError("da", "must not be negative")
    if base.capex < 0:
        raise DCFValidationError("capex", "must not be negative")
    if base.diluted_shares <= 0:
        raise DCFValidationError("diluted_shares", "must be positive")


def _validate(base: BaseFinancials, assumptions: Assumptions) -> None:
    _validate_base(base)

    if not (3 <= assumptions.projection_years <= 15):
        raise DCFValidationError("projection_years", "must be between 3 and 15")

    if len(assumptions.resolved_revenue_growth) != assumptions.projection_years:
        raise DCFValidationError(
            "revenue_growth",
            "must be a single value or one value per projection year",
        )

    assumption_fields = {
        "wacc": assumptions.wacc,
        "terminal_growth": assumptions.terminal_growth,
        "tax_rate": assumptions.tax_rate,
        "ebit_margin": assumptions.ebit_margin,
    }
    for field, value in assumption_fields.items():
        _require_finite(field, value)

    if not (MIN_WACC <= assumptions.wacc <= MAX_WACC):
        raise DCFValidationError("wacc", f"must be between {MIN_WACC} and {MAX_WACC}")

    if not (MIN_TERMINAL_GROWTH <= assumptions.terminal_growth <= MAX_TERMINAL_GROWTH):
        raise DCFValidationError(
            "terminal_growth",
            f"must be between {MIN_TERMINAL_GROWTH} and {MAX_TERMINAL_GROWTH}",
        )

    if assumptions.terminal_growth >= assumptions.wacc:
        raise DCFValidationError(
            "terminal_growth", "must be less than wacc (Gordon growth formula)"
        )

    if not (MIN_TAX_RATE <= assumptions.tax_rate <= MAX_TAX_RATE):
        raise DCFValidationError("tax_rate", f"must be between {MIN_TAX_RATE} and {MAX_TAX_RATE}")

    if not (MIN_EBIT_MARGIN <= assumptions.ebit_margin <= MAX_EBIT_MARGIN):
        raise DCFValidationError(
            "ebit_margin", f"must be between {MIN_EBIT_MARGIN} and {MAX_EBIT_MARGIN}"
        )

    for g in assumptions.resolved_revenue_growth:
        _require_finite("revenue_growth", g)
        if not (MIN_REVENUE_GROWTH <= g <= MAX_REVENUE_GROWTH):
            raise DCFValidationError("revenue_growth", "each value must be within +/-50%")


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

    for year, growth in enumerate(assumptions.resolved_revenue_growth, start=1):
        revenue = prev_revenue * (1 + growth)
        ebit = revenue * assumptions.ebit_margin
        da = revenue * da_ratio
        capex = revenue * capex_ratio
        delta_nwc = revenue * nwc_ratio

        cash_taxes = ebit * assumptions.tax_rate
        nopat = ebit - cash_taxes
        fcf = nopat + da - capex - delta_nwc
        discount_period = float(year)
        discount_factor = 1.0 / ((1.0 + assumptions.wacc) ** year)
        pv_fcf = fcf * discount_factor

        for value in (
            revenue,
            ebit,
            cash_taxes,
            nopat,
            da,
            capex,
            delta_nwc,
            fcf,
            discount_period,
            discount_factor,
            pv_fcf,
        ):
            _require_finite("calculation", value)

        projections.append(
            YearProjection(
                year=year,
                revenue_growth=growth,
                revenue=revenue,
                ebit_margin=assumptions.ebit_margin,
                ebit=ebit,
                cash_taxes=cash_taxes,
                nopat=nopat,
                da=da,
                capex=capex,
                delta_nwc=delta_nwc,
                fcf=fcf,
                discount_period=discount_period,
                discount_factor=discount_factor,
                pv_fcf=pv_fcf,
            )
        )

        pv_fcf_total += pv_fcf
        prev_revenue = revenue
        last_fcf = fcf
        last_discount_factor = discount_factor

    terminal_value = (
        last_fcf
        * (1 + assumptions.terminal_growth)
        / (assumptions.wacc - assumptions.terminal_growth)
    )
    pv_terminal_value = terminal_value * last_discount_factor

    enterprise_value = pv_fcf_total + pv_terminal_value
    equity_value = enterprise_value - base.net_debt
    intrinsic_value_per_share = equity_value / base.diluted_shares

    for value in (
        terminal_value,
        pv_terminal_value,
        enterprise_value,
        equity_value,
        intrinsic_value_per_share,
    ):
        _require_finite("calculation", value)

    warnings: list[str] = []
    if enterprise_value < 0:
        warnings.append(
            "Projected enterprise value is negative under the supplied assumptions; "
            "the result is returned without clipping."
        )
    if equity_value < 0:
        warnings.append(
            "Projected equity value is negative under the supplied assumptions; "
            "the result is returned without clipping."
        )

    return Valuation(
        projections=tuple(projections),
        terminal_value=terminal_value,
        pv_terminal_value=pv_terminal_value,
        enterprise_value=enterprise_value,
        equity_value=equity_value,
        intrinsic_value_per_share=intrinsic_value_per_share,
        warnings=tuple(warnings),
    )


# Grid offsets per CLAUDE.md: WACC +/-1% x terminal growth +/-0.5%.
WACC_OFFSETS = (-0.01, 0.0, 0.01)
TERMINAL_GROWTH_OFFSETS = (-0.005, 0.0, 0.005)


def compute_sensitivity_grid(base: BaseFinancials, assumptions: Assumptions) -> SensitivityGrid:
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

    rows: list[tuple[float | None, ...]] = []
    for wacc in wacc_values:
        row: list[float | None] = []
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
