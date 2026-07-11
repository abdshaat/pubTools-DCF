"""Pydantic schemas for the API layer.

These define the wire format only. Internal layers keep using the frozen
dataclasses in app.models; converting at the boundary keeps the engine
free of FastAPI/pydantic dependencies.
"""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
    revenue_growth: float
    revenue: float
    ebit_margin: float
    ebit: float
    cash_taxes: float
    nopat: float
    da: float
    capex: float
    delta_nwc: float
    fcf: float
    discount_period: float
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
    request_id: str
    computed_at: datetime
    model_version: str
    data_version: str
    data_provider: str
    currency: str | None
    monetary_unit: str
    fundamentals_as_of: str | None
    price_as_of: datetime | None
    price_fetched_at: datetime | None
    fiscal_year: str | None
    statement_period: str | None
    filing_date: str | None
    accepted_at: str | None
    statement_selection: str
    disclaimer: str
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
    warnings: list[str]
    sensitivity: SensitivityOut | None = None


class FieldError(BaseModel):
    field: str
    message: str


class ErrorField(BaseModel):
    field: str
    code: str
    message: str


class ErrorBody(BaseModel):
    version: str = "1"
    code: str
    message: str
    request_id: str
    fields: list[ErrorField] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Backward-compatible errors keep `detail`; new clients use `error`."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "detail": [{"field": "wacc", "message": "must be between 0.001 and 0.5"}],
                "error": {
                    "version": "1",
                    "code": "invalid_assumptions",
                    "message": "DCF assumptions are invalid.",
                    "request_id": "8bd53754-b11d-4d6f-9634-a47480f6b97d",
                    "fields": [
                        {
                            "field": "wacc",
                            "code": "invalid_value",
                            "message": "must be between 0.001 and 0.5",
                        }
                    ],
                },
            }
        }
    )

    detail: str | list[FieldError] | list[dict[str, Any]]
    error: ErrorBody


def build_valuation_response(
    base: BaseFinancials,
    assumptions: Assumptions,
    valuation: Valuation,
    sensitivity: SensitivityGrid | None = None,
    request_id: str = "internal",
    computed_at: datetime | None = None,
) -> ValuationResponse:
    snapshot = json.dumps(asdict(base), sort_keys=True, separators=(",", ":"), default=str)
    data_version = f"sha256:{hashlib.sha256(snapshot.encode()).hexdigest()}"
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
        request_id=request_id,
        computed_at=computed_at or datetime.now(UTC),
        model_version=MODEL_VERSION,
        data_version=data_version,
        data_provider=base.data_provider,
        currency=base.currency,
        monetary_unit="raw_currency_units",
        fundamentals_as_of=base.fundamentals_as_of,
        price_as_of=base.price_as_of,
        price_fetched_at=base.price_fetched_at,
        fiscal_year=base.fiscal_year,
        statement_period=base.statement_period,
        filing_date=base.filing_date,
        accepted_at=base.accepted_at,
        statement_selection=base.statement_selection,
        disclaimer="Model estimate based on supplied assumptions; not investment advice.",
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
                revenue_growth=p.revenue_growth,
                revenue=p.revenue,
                ebit_margin=p.ebit_margin,
                ebit=p.ebit,
                cash_taxes=p.cash_taxes,
                nopat=p.nopat,
                da=p.da,
                capex=p.capex,
                delta_nwc=p.delta_nwc,
                fcf=p.fcf,
                discount_period=p.discount_period,
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
        warnings=[*base.data_quality_warnings, *valuation.warnings],
    )
