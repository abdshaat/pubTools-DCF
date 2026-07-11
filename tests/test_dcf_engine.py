import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.dcf_engine import (
    DCFValidationError,
    compute_dcf,
    compute_sensitivity_grid,
)
from app.models import Assumptions, BaseFinancials
from tests.conftest import make_base_financials

# ---------------------------------------------------------------------------
# Hand-computed cases (the "validate vs a spreadsheet" milestone).
# Each expected value below is computed independently of app/dcf_engine.py.
# ---------------------------------------------------------------------------


def test_matches_hand_computed_3_year_flat_growth(base_financials: BaseFinancials):
    assumptions = Assumptions(
        wacc=0.10,
        terminal_growth=0.02,
        tax_rate=0.25,
        ebit_margin=0.25,
        projection_years=3,
        revenue_growth=0.05,  # scalar broadcast to 3 years
    )

    result = compute_dcf(base_financials, assumptions)

    # Year 1: revenue = 1,000,000 * 1.05 = 1,050,000
    #   ebit  = 1,050,000 * 0.25          = 262,500
    #   da    = 1,050,000 * 0.05          =  52,500
    #   capex = 1,050,000 * 0.06          =  63,000
    #   dNWC  = 1,050,000 * 0.01          =  10,500
    #   fcf   = 262,500*0.75 + 52,500 - 63,000 - 10,500 = 175,875
    #   df    = 1 / 1.10^1 = 0.909090909...
    #   pv_fcf = 175,875 * 0.909090909... = 159,886.36...
    y1 = result.projections[0]
    assert y1.revenue == pytest.approx(1_050_000.0)
    assert y1.fcf == pytest.approx(175_875.0)
    assert y1.pv_fcf == pytest.approx(159_886.3636, rel=1e-6)

    # Year 2: revenue = 1,050,000 * 1.05 = 1,102,500
    #   ebit  = 275,625; da = 55,125; capex = 66,150; dNWC = 11,025
    #   fcf   = 275,625*0.75 + 55,125 - 66,150 - 11,025 = 184,668.75
    y2 = result.projections[1]
    assert y2.revenue == pytest.approx(1_102_500.0)
    assert y2.fcf == pytest.approx(184_668.75)

    # Year 3: revenue = 1,102,500 * 1.05 = 1,157,625
    #   ebit = 289,406.25; da = 57,881.25; capex = 69,457.5; dNWC = 11,576.25
    #   fcf  = 289,406.25*0.75 + 57,881.25 - 69,457.5 - 11,576.25 = 193,902.1875
    y3 = result.projections[2]
    assert y3.revenue == pytest.approx(1_157_625.0)
    assert y3.fcf == pytest.approx(193_902.1875)

    # Terminal value off year-3 FCF: TV = fcf3 * 1.02 / (0.10 - 0.02)
    expected_tv = 193_902.1875 * 1.02 / 0.08
    assert result.terminal_value == pytest.approx(expected_tv)

    expected_pv_tv = expected_tv / (1.10**3)
    assert result.pv_terminal_value == pytest.approx(expected_pv_tv)

    expected_pv_fcf_sum = 175_875.0 / 1.10 + 184_668.75 / 1.10**2 + 193_902.1875 / 1.10**3
    expected_ev = expected_pv_fcf_sum + expected_pv_tv
    expected_equity = expected_ev - base_financials.net_debt
    expected_per_share = expected_equity / base_financials.diluted_shares

    assert result.enterprise_value == pytest.approx(expected_ev)
    assert result.equity_value == pytest.approx(expected_equity)
    assert result.intrinsic_value_per_share == pytest.approx(expected_per_share)
    assert result.upside_pct == pytest.approx(
        (expected_per_share - base_financials.current_price) / base_financials.current_price * 100
    )


def test_matches_hand_computed_zero_growth_zero_terminal_growth(base_financials: BaseFinancials):
    # With g=0 and terminal_growth=0, every projected year is identical to
    # the base-year ratios applied to a flat revenue, making this the
    # simplest possible case to verify by hand.
    assumptions = Assumptions(
        wacc=0.10,
        terminal_growth=0.0,
        tax_rate=0.20,
        ebit_margin=0.25,
        projection_years=3,
        revenue_growth=0.0,
    )

    result = compute_dcf(base_financials, assumptions)

    # revenue flat at 1,000,000 every year
    # ebit = 250,000; fcf = 250,000*0.8 + 50,000 - 60,000 - 10,000 = 180,000
    for yp in result.projections:
        assert yp.revenue == pytest.approx(1_000_000.0)
        assert yp.fcf == pytest.approx(180_000.0)

    expected_tv = 180_000.0 * 1.0 / 0.10
    assert result.terminal_value == pytest.approx(expected_tv)


def test_per_year_revenue_growth_list_is_used_positionally(base_financials: BaseFinancials):
    assumptions = Assumptions(
        wacc=0.10,
        terminal_growth=0.02,
        tax_rate=0.25,
        ebit_margin=0.25,
        projection_years=3,
        revenue_growth=[0.10, 0.05, 0.0],
    )

    result = compute_dcf(base_financials, assumptions)

    assert result.projections[0].revenue == pytest.approx(1_100_000.0)
    assert result.projections[1].revenue == pytest.approx(1_155_000.0)
    assert result.projections[2].revenue == pytest.approx(1_155_000.0)


# ---------------------------------------------------------------------------
# Validation rules (CLAUDE.md: return per-field errors, 422 at the API layer)
# ---------------------------------------------------------------------------


def test_terminal_growth_equal_to_wacc_rejected(base_financials: BaseFinancials):
    assumptions = Assumptions(
        wacc=0.10,
        terminal_growth=0.10,
        tax_rate=0.25,
        ebit_margin=0.25,
        projection_years=5,
        revenue_growth=0.05,
    )
    with pytest.raises(DCFValidationError) as exc:
        compute_dcf(base_financials, assumptions)
    assert exc.value.field == "terminal_growth"


def test_terminal_growth_above_wacc_rejected(base_financials: BaseFinancials):
    assumptions = Assumptions(
        wacc=0.10,
        terminal_growth=0.12,
        tax_rate=0.25,
        ebit_margin=0.25,
        projection_years=5,
        revenue_growth=0.05,
    )
    with pytest.raises(DCFValidationError) as exc:
        compute_dcf(base_financials, assumptions)
    assert exc.value.field == "terminal_growth"


@pytest.mark.parametrize("years", [1, 2, 16, 30])
def test_projection_years_out_of_range_rejected(base_financials: BaseFinancials, years: int):
    assumptions = Assumptions(
        wacc=0.10,
        terminal_growth=0.02,
        tax_rate=0.25,
        ebit_margin=0.25,
        projection_years=years,
        revenue_growth=0.05,
    )
    with pytest.raises(DCFValidationError) as exc:
        compute_dcf(base_financials, assumptions)
    assert exc.value.field == "projection_years"


@pytest.mark.parametrize("years", [3, 5, 15])
def test_projection_years_boundary_values_accepted(base_financials: BaseFinancials, years: int):
    assumptions = Assumptions(
        wacc=0.10,
        terminal_growth=0.02,
        tax_rate=0.25,
        ebit_margin=0.25,
        projection_years=years,
        revenue_growth=0.05,
    )
    result = compute_dcf(base_financials, assumptions)
    assert len(result.projections) == years


@pytest.mark.parametrize("growth", [0.51, -0.51, 5.0])
def test_revenue_growth_beyond_50pct_rejected(base_financials: BaseFinancials, growth: float):
    assumptions = Assumptions(
        wacc=0.10,
        terminal_growth=0.02,
        tax_rate=0.25,
        ebit_margin=0.25,
        projection_years=3,
        revenue_growth=growth,
    )
    with pytest.raises(DCFValidationError) as exc:
        compute_dcf(base_financials, assumptions)
    assert exc.value.field == "revenue_growth"


def test_negative_tax_rate_rejected(base_financials: BaseFinancials):
    assumptions = Assumptions(
        wacc=0.10,
        terminal_growth=0.02,
        tax_rate=-0.01,
        ebit_margin=0.25,
        projection_years=3,
        revenue_growth=0.05,
    )
    with pytest.raises(DCFValidationError) as exc:
        compute_dcf(base_financials, assumptions)
    assert exc.value.field == "tax_rate"


def test_revenue_growth_list_length_mismatch_rejected(base_financials: BaseFinancials):
    assumptions = Assumptions(
        wacc=0.10,
        terminal_growth=0.02,
        tax_rate=0.25,
        ebit_margin=0.25,
        projection_years=5,
        revenue_growth=[0.05, 0.05],
    )
    with pytest.raises(DCFValidationError) as exc:
        compute_dcf(base_financials, assumptions)
    assert exc.value.field == "revenue_growth"


# ---------------------------------------------------------------------------
# Sensitivity grid
# ---------------------------------------------------------------------------


def _grid_assumptions(wacc=0.10, terminal_growth=0.02) -> Assumptions:
    return Assumptions(
        wacc=wacc,
        terminal_growth=terminal_growth,
        tax_rate=0.25,
        ebit_margin=0.25,
        projection_years=5,
        revenue_growth=0.05,
    )


def test_grid_axes_are_clean_offsets_around_inputs(base_financials: BaseFinancials):
    grid = compute_sensitivity_grid(base_financials, _grid_assumptions())
    assert grid.wacc_values == (0.09, 0.10, 0.11)
    assert grid.terminal_growth_values == (0.015, 0.02, 0.025)


def test_grid_center_cell_equals_point_estimate(base_financials: BaseFinancials):
    assumptions = _grid_assumptions()
    grid = compute_sensitivity_grid(base_financials, assumptions)
    point = compute_dcf(base_financials, assumptions)
    assert grid.per_share_values[1][1] == pytest.approx(point.intrinsic_value_per_share)


def test_grid_cells_match_independent_recomputation(base_financials: BaseFinancials):
    grid = compute_sensitivity_grid(base_financials, _grid_assumptions())
    corner = compute_dcf(base_financials, _grid_assumptions(wacc=0.09, terminal_growth=0.025))
    assert grid.per_share_values[0][2] == pytest.approx(corner.intrinsic_value_per_share)


def test_grid_is_monotonic_in_both_axes(base_financials: BaseFinancials):
    grid = compute_sensitivity_grid(base_financials, _grid_assumptions())
    for row in grid.per_share_values:  # fixed WACC: value rises with growth
        assert row[0] < row[1] < row[2]
    for col in range(3):  # fixed growth: value falls as WACC rises
        assert (
            grid.per_share_values[0][col]
            > grid.per_share_values[1][col]
            > grid.per_share_values[2][col]
        )


def test_grid_marks_gordon_breaking_cells_none_instead_of_erroring(
    base_financials: BaseFinancials,
):
    # wacc=0.03, g=0.024 is valid, but the wacc-1% row (0.02) collides with
    # g=0.024 and g=0.029; the point estimate must still come back
    grid = compute_sensitivity_grid(
        base_financials, _grid_assumptions(wacc=0.03, terminal_growth=0.024)
    )
    low_wacc_row = grid.per_share_values[0]
    assert low_wacc_row[1] is None and low_wacc_row[2] is None
    assert low_wacc_row[0] is not None  # g=0.019 < 0.02 still computes
    assert grid.per_share_values[1][1] is not None  # center always present


# ---------------------------------------------------------------------------
# Property-based invariant tests. These guard against formula regressions
# that a fixed set of hand-computed cases could miss, and directly target
# the "output is extremely sensitive to WACC/terminal growth" risk called
# out in CLAUDE.md by asserting the sensitivity direction is always correct.
# ---------------------------------------------------------------------------

valid_wacc = st.floats(min_value=0.06, max_value=0.20, allow_nan=False)
valid_growth = st.floats(min_value=-0.3, max_value=0.3, allow_nan=False)
valid_tax = st.floats(min_value=0.0, max_value=0.5, allow_nan=False)
valid_margin = st.floats(min_value=0.05, max_value=0.5, allow_nan=False)
valid_years = st.integers(min_value=3, max_value=15)


@settings(max_examples=100)
@given(
    wacc=valid_wacc,
    terminal_growth=st.floats(min_value=-0.05, max_value=0.03, allow_nan=False),
    tax_rate=valid_tax,
    ebit_margin=valid_margin,
    years=valid_years,
    growth=valid_growth,
    wacc_bump=st.floats(min_value=0.001, max_value=0.02, allow_nan=False),
)
def test_higher_wacc_never_increases_equity_value(
    wacc, terminal_growth, tax_rate, ebit_margin, years, growth, wacc_bump
):
    assume(terminal_growth < wacc)
    assume(terminal_growth < wacc + wacc_bump)
    base_financials = make_base_financials()

    low = Assumptions(
        wacc=wacc,
        terminal_growth=terminal_growth,
        tax_rate=tax_rate,
        ebit_margin=ebit_margin,
        projection_years=years,
        revenue_growth=growth,
    )
    high = Assumptions(
        wacc=wacc + wacc_bump,
        terminal_growth=terminal_growth,
        tax_rate=tax_rate,
        ebit_margin=ebit_margin,
        projection_years=years,
        revenue_growth=growth,
    )

    result_low = compute_dcf(base_financials, low)
    result_high = compute_dcf(base_financials, high)

    assert result_high.equity_value <= result_low.equity_value + 1e-6


@settings(max_examples=100)
@given(
    wacc=st.floats(min_value=0.10, max_value=0.20, allow_nan=False),
    terminal_growth=st.floats(min_value=-0.05, max_value=0.05, allow_nan=False),
    tax_rate=valid_tax,
    ebit_margin=valid_margin,
    years=valid_years,
    growth=valid_growth,
    growth_bump=st.floats(min_value=0.001, max_value=0.02, allow_nan=False),
)
def test_higher_terminal_growth_never_decreases_equity_value(
    wacc, terminal_growth, tax_rate, ebit_margin, years, growth, growth_bump
):
    assume(terminal_growth < wacc)
    assume(terminal_growth + growth_bump < wacc)
    base_financials = make_base_financials()

    low = Assumptions(
        wacc=wacc,
        terminal_growth=terminal_growth,
        tax_rate=tax_rate,
        ebit_margin=ebit_margin,
        projection_years=years,
        revenue_growth=growth,
    )
    high = Assumptions(
        wacc=wacc,
        terminal_growth=terminal_growth + growth_bump,
        tax_rate=tax_rate,
        ebit_margin=ebit_margin,
        projection_years=years,
        revenue_growth=growth,
    )

    result_low = compute_dcf(base_financials, low)
    result_high = compute_dcf(base_financials, high)

    assert result_high.equity_value >= result_low.equity_value - 1e-6


@settings(max_examples=50)
@given(
    wacc=valid_wacc,
    terminal_growth=st.floats(min_value=-0.05, max_value=0.03, allow_nan=False),
    tax_rate=valid_tax,
    ebit_margin=valid_margin,
    years=valid_years,
    growth=valid_growth,
)
def test_enterprise_value_equals_sum_of_pv_fcf_plus_pv_terminal_value(
    wacc, terminal_growth, tax_rate, ebit_margin, years, growth
):
    assume(terminal_growth < wacc)
    base_financials = make_base_financials()

    assumptions = Assumptions(
        wacc=wacc,
        terminal_growth=terminal_growth,
        tax_rate=tax_rate,
        ebit_margin=ebit_margin,
        projection_years=years,
        revenue_growth=growth,
    )
    result = compute_dcf(base_financials, assumptions)

    pv_fcf_sum = sum(p.pv_fcf for p in result.projections)
    assert result.enterprise_value == pytest.approx(pv_fcf_sum + result.pv_terminal_value)
    assert result.equity_value == pytest.approx(result.enterprise_value - base_financials.net_debt)
    assert result.intrinsic_value_per_share == pytest.approx(
        result.equity_value / base_financials.diluted_shares
    )
