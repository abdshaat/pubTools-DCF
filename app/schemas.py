"""Pydantic schemas for the API layer.

These define the wire format only. Internal layers keep using the frozen
dataclasses in app.models; converting at the boundary keeps the engine
free of FastAPI/pydantic dependencies.
"""

from pydantic import BaseModel

from . import MODEL_VERSION
from .models import Assumptions, BaseFinancials, SensitivityGrid, Valuation


class BaseFinancialsOut(BaseModel):
    ticker: str
    source_period: str
    revenue: float
    ebit: float
    da: float
    capex: float
    delta_nwc: float
    net_debt: float
    diluted_shares: float
    current_price: float


class ResolvedAssumptionsOut(BaseModel):
    """Assumptions echoed back fully resolved — e.g. a scalar revenue_growth
    is expanded to one value per projection year, so the caller sees exactly
    what the engine used."""

    wacc: float
    terminal_growth: float
    tax_rate: float
    ebit_margin: float
    projection_years: int
    revenue_growth: list[float]


class YearProjectionOut(BaseModel):
    year: int
    revenue: float
    ebit: float
    fcf: float
    discount_factor: float
    pv_fcf: float


class SensitivityOut(BaseModel):
    """Intrinsic value per share across WACC (rows) x terminal growth
    (columns). null cells mark combinations where the Gordon formula is
    undefined (terminal growth >= WACC)."""

    wacc_values: list[float]
    terminal_growth_values: list[float]
    intrinsic_value_per_share: list[list[float | None]]


class ValuationResponse(BaseModel):
    model_version: str
    ticker: str
    base_financials: BaseFinancialsOut
    assumptions: ResolvedAssumptionsOut
    projections: list[YearProjectionOut]
    terminal_value: float
    pv_terminal_value: float
    enterprise_value: float
    equity_value: float
    intrinsic_value_per_share: float
    current_price: float
    upside_pct: float
    sensitivity: SensitivityOut | None = None


class FieldError(BaseModel):
    field: str
    message: str


def build_valuation_response(
    base: BaseFinancials,
    assumptions: Assumptions,
    valuation: Valuation,
    sensitivity: SensitivityGrid | None = None,
) -> ValuationResponse:
    return ValuationResponse(
        sensitivity=(
            SensitivityOut(
                wacc_values=list(sensitivity.wacc_values),
                terminal_growth_values=list(sensitivity.terminal_growth_values),
                intrinsic_value_per_share=[list(row) for row in sensitivity.per_share_values],
            )
            if sensitivity is not None
            else None
        ),
        model_version=MODEL_VERSION,
        ticker=base.ticker,
        base_financials=BaseFinancialsOut(
            ticker=base.ticker,
            source_period=base.source_period,
            revenue=base.revenue,
            ebit=base.ebit,
            da=base.da,
            capex=base.capex,
            delta_nwc=base.delta_nwc,
            net_debt=base.net_debt,
            diluted_shares=base.diluted_shares,
            current_price=base.current_price,
        ),
        assumptions=ResolvedAssumptionsOut(
            wacc=assumptions.wacc,
            terminal_growth=assumptions.terminal_growth,
            tax_rate=assumptions.tax_rate,
            ebit_margin=assumptions.ebit_margin,
            projection_years=assumptions.projection_years,
            revenue_growth=list(assumptions.resolved_revenue_growth),
        ),
        projections=[
            YearProjectionOut(
                year=p.year,
                revenue=p.revenue,
                ebit=p.ebit,
                fcf=p.fcf,
                discount_factor=p.discount_factor,
                pv_fcf=p.pv_fcf,
            )
            for p in valuation.projections
        ],
        terminal_value=valuation.terminal_value,
        pv_terminal_value=valuation.pv_terminal_value,
        enterprise_value=valuation.enterprise_value,
        equity_value=valuation.equity_value,
        intrinsic_value_per_share=valuation.intrinsic_value_per_share,
        current_price=valuation.current_price,
        upside_pct=valuation.upside_pct,
    )
