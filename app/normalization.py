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

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from datetime import date as Date
from math import isfinite
from typing import Any

from .exceptions import NormalizationError, UnsupportedSectorError
from .models import BaseFinancials
from .providers.fmp import FMPFundamentals

# v1 scope gate: standard FCF DCF doesn't apply to banks/insurers.
FINANCIAL_SECTORS = {"Financial Services", "Financial", "Banking", "Insurance"}

_MISSING = object()


@dataclass(frozen=True)
class _SelectedStatements:
    income: dict[str, Any]
    balance: dict[str, Any]
    cash_flow: dict[str, Any]
    statement_date: str
    fiscal_year: str
    period: str
    filing_date: str | None
    accepted_at: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedQuote:
    price: float
    price_as_of: datetime | None
    fetched_at: datetime


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


def _as_iso_date(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return Date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _fiscal_year(record: dict[str, Any]) -> str | None:
    value = _pick(record, "fiscalYear", "calendarYear")
    return None if value is _MISSING else str(value)


def _filing_rank(record: dict[str, Any], position: int) -> tuple[str, str, int]:
    accepted = str(record.get("acceptedDate") or "")
    filing = str(record.get("filingDate") or record.get("fillingDate") or "")
    # Earlier provider positions win only as the final tie-breaker.
    return accepted, filing, -position


def _select_latest_complete_statements(f: FMPFundamentals) -> _SelectedStatements:
    endpoint_records = {
        "income": f.income,
        "balance": f.balance,
        "cash_flow": f.cash_flow,
    }
    indexed: dict[str, dict[tuple[str, str], list[tuple[int, dict[str, Any]]]]] = {}
    all_annual_dates: set[str] = set()

    for name, records in endpoint_records.items():
        by_identity: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
        for position, record in enumerate(records):
            period = str(record.get("period") or "").upper()
            statement_date = _as_iso_date(record.get("date"))
            if period != "FY" or statement_date is None:
                continue
            identity = (period, statement_date)
            by_identity.setdefault(identity, []).append((position, record))
            all_annual_dates.add(statement_date)
        indexed[name] = by_identity

    compatible = set(indexed["income"]) & set(indexed["balance"]) & set(indexed["cash_flow"])
    if not compatible:
        raise NormalizationError(f.ticker, ["statement_alignment"])

    profile_currency = str(f.profile.get("currency") or "").upper()
    valid: list[_SelectedStatements] = []
    currency_rejected = False
    for period, statement_date in compatible:
        chosen: dict[str, dict[str, Any]] = {}
        duplicate_selected = False
        for name in endpoint_records:
            candidates = indexed[name][(period, statement_date)]
            position, record = max(candidates, key=lambda item: _filing_rank(item[1], item[0]))
            chosen[name] = record
            duplicate_selected = duplicate_selected or len(candidates) > 1

        supplied_years = {year for record in chosen.values() if (year := _fiscal_year(record))}
        if len(supplied_years) > 1:
            continue
        fiscal_year = next(iter(supplied_years), statement_date[:4])

        supplied_currencies = {
            str(currency).upper()
            for record in chosen.values()
            if (currency := record.get("reportedCurrency"))
        }
        if len(supplied_currencies) > 1:
            currency_rejected = True
            continue
        if supplied_currencies and profile_currency not in supplied_currencies:
            currency_rejected = True
            continue

        accepted_values = [str(r["acceptedDate"]) for r in chosen.values() if r.get("acceptedDate")]
        filing_values = [
            str(value)
            for record in chosen.values()
            if (value := record.get("filingDate") or record.get("fillingDate"))
        ]
        warnings: list[str] = []
        if not supplied_years:
            warnings.append("Fiscal year was derived from the matched statement date.")
        if not supplied_currencies:
            warnings.append("Statement currency was absent; profile currency was used.")
        if duplicate_selected:
            warnings.append("A newer filing/restatement was selected for the matched period.")

        valid.append(
            _SelectedStatements(
                income=chosen["income"],
                balance=chosen["balance"],
                cash_flow=chosen["cash_flow"],
                statement_date=statement_date,
                fiscal_year=fiscal_year,
                period=period,
                filing_date=max(filing_values, default=None),
                accepted_at=max(accepted_values, default=None),
                warnings=tuple(warnings),
            )
        )

    if not valid:
        fields = ["statement_alignment"]
        if currency_rejected:
            fields.append("currency")
        raise NormalizationError(f.ticker, fields)

    selected = max(
        valid,
        key=lambda item: (
            item.statement_date,
            item.fiscal_year,
            item.accepted_at or "",
            item.filing_date or "",
        ),
    )
    if any(statement_date > selected.statement_date for statement_date in all_annual_dates):
        selected = replace(
            selected,
            warnings=selected.warnings
            + ("A newer annual period was incomplete and was not mixed into the valuation.",),
        )
    return selected


def normalize_finnhub_quote(
    ticker: str, quote: dict[str, Any], fetched_at: datetime
) -> NormalizedQuote:
    """Finnhub /quote: `c` is the current price, `t` unix seconds (0 = absent)."""
    try:
        price = float(quote["c"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise NormalizationError(ticker, ["current_price"]) from None
    if not isfinite(price) or price <= 0:
        raise NormalizationError(ticker, ["current_price"])

    price_as_of = None
    quote_timestamp = quote.get("t")
    if quote_timestamp is not None and quote_timestamp != 0:
        try:
            price_as_of = datetime.fromtimestamp(float(quote_timestamp), tz=UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            raise NormalizationError(ticker, ["price_as_of"]) from None
    return NormalizedQuote(price=price, price_as_of=price_as_of, fetched_at=fetched_at)


def normalize_fmp_fundamentals(f: FMPFundamentals) -> BaseFinancials:
    sector = f.profile.get("sector")
    if sector in FINANCIAL_SECTORS:
        raise UnsupportedSectorError(f.ticker, sector)

    selected = _select_latest_complete_statements(f)

    fields = {
        "revenue": _pick(selected.income, "revenue"),
        "ebit": _pick(selected.income, "operatingIncome"),
        "diluted_shares": _pick(selected.income, "weightedAverageShsOutDil"),
        "da": _pick(selected.cash_flow, "depreciationAndAmortization"),
        "capex": _pick(selected.cash_flow, "capitalExpenditure"),
        "change_in_working_capital": _pick(selected.cash_flow, "changeInWorkingCapital"),
        "total_debt": _pick(selected.balance, "totalDebt"),
        "cash": _pick(selected.balance, "cashAndCashEquivalents"),
    }

    currency = _pick(f.profile, "currency")
    if currency is _MISSING or not str(currency).strip():
        raise NormalizationError(f.ticker, ["currency"])

    missing = sorted(name for name, value in fields.items() if value is _MISSING)
    if missing:
        raise NormalizationError(f.ticker, missing)

    converted: dict[str, float] = {}
    invalid: list[str] = []
    for name, value in fields.items():
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            invalid.append(name)
            continue
        if not isfinite(number):
            invalid.append(name)
            continue
        converted[name] = number

    if invalid:
        raise NormalizationError(f.ticker, sorted(invalid))

    if converted["revenue"] <= 0:
        invalid.append("revenue")
    if converted["diluted_shares"] <= 0:
        invalid.append("diluted_shares")
    if converted["da"] < 0:
        invalid.append("da")
    net_debt = converted["total_debt"] - converted["cash"]
    if not isfinite(net_debt):
        invalid.append("net_debt")
    if invalid:
        raise NormalizationError(f.ticker, sorted(invalid))

    source_period = f"{selected.period}{selected.fiscal_year} ({selected.statement_date})"

    return BaseFinancials(
        ticker=f.ticker,
        source_period=source_period,
        revenue=converted["revenue"],
        ebit=converted["ebit"],
        da=converted["da"],
        capex=abs(converted["capex"]),
        delta_nwc=-converted["change_in_working_capital"],
        net_debt=net_debt,
        diluted_shares=converted["diluted_shares"],
        currency=str(currency).upper(),
        fundamentals_as_of=selected.statement_date,
        fiscal_year=selected.fiscal_year,
        statement_period=selected.period,
        filing_date=selected.filing_date,
        accepted_at=selected.accepted_at,
        data_quality_warnings=selected.warnings,
    )
