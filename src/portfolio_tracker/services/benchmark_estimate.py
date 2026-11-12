"""Benchmark comparison estimate: lookback returns vs any benchmark, with a band.

The Canonical portfolio return (`performance.modified_dietz` v2) fails closed
whenever a boundary is modeled or a source gap exists. This service answers the
same question as a separately labeled estimate instead: it reuses the retained
value history (`weekly_values.build_daily_value_points`), the canonical external
flow ledger, and stored benchmark total-return closes, and it reports the
canonical result next to the estimate so a consumer never confuses the two.

Uncertainty is reported as an explicit low/high band built from named
scenarios, never as a statistical confidence interval:

* every observed ending basis available on the ending date;
* every basis available on the opening date;
* for a modeled opening, the opening value scaled by
  ``±model_error_per_180_days`` for each 180 days between the opening and the
  earliest observed full-book value.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_tracker.models import Benchmark, InvestmentTransaction
from portfolio_tracker.services.active_items import valued_account_ids
from portfolio_tracker.services.external_flow_ledger import (
    ExternalFlowEntry,
    build_external_flow_ledger,
)
from portfolio_tracker.services.performance import compute_performance_series
from portfolio_tracker.services.weekly_values import (
    DailyValuePoint,
    ValueBasis,
    build_daily_value_points,
)

METHODOLOGY = "benchmark_comparison.estimate"
METHODOLOGY_VERSION = "1"

# Calibrated 2026-11-12: walking the transaction ledger back 180 days from an
# observed full book reproduced later observed totals within 1.63%.
DEFAULT_MODEL_ERROR_PER_180_DAYS = Decimal("0.0163")
DEFAULT_LOOKBACK_DAYS: tuple[int, ...] = (180, 365, 730)
DEFAULT_BENCHMARKS: tuple[str, ...] = ("VOO", "QQQ")

_BOUNDARY_TOLERANCE_DAYS = 6
_PRICE_LOOKBACK_DAYS = 7
_OBSERVED_BASES: tuple[ValueBasis, ...] = (
    "whole_account",
    "marked_positions",
    "legacy_cached_snapshot",
)
_BASIS_PREFERENCE: tuple[ValueBasis, ...] = (*_OBSERVED_BASES, "legacy_cached_model")
_SHARE_TRANSFER_SUBTYPES = ("external_asset_transfer_in", "external_asset_transfer_out")
_QUANTITY_TOLERANCE = Decimal("0.000001")

Tier = Literal["canonical_available", "estimate", "unavailable"]
ReturnBasis = Literal["total_return_adjusted", "raw_price_fallback"]


class Band(BaseModel):
    central: Decimal
    low: Decimal
    high: Decimal


class BoundaryValue(BaseModel):
    as_of: date
    basis: ValueBasis
    value: Decimal


class BenchmarkComparison(BaseModel):
    ticker: str
    available: bool
    return_basis: ReturnBasis | None = None
    return_pct: Band | None
    gap_pp: Band | None
    dollar_gap: Band | None


class MatchedShareTransfer(BaseModel):
    date: date
    ticker: str | None
    flow_id: str
    withheld_amount: Decimal


class CanonicalSummary(BaseModel):
    calculation_status: str
    reason_codes: list[str]
    portfolio_return_pct: Decimal | None


class LookbackComparison(BaseModel):
    lookback_days: int
    tier: Tier
    unavailable_reason: str | None
    start_date: date | None
    end_date: date | None
    opening: list[BoundaryValue]
    ending: list[BoundaryValue]
    opening_is_modeled: bool
    opening_error_fraction: Decimal
    net_external_cashflow_in: Decimal | None
    modified_dietz_denominator: Decimal | None
    portfolio_return_pct: Band | None
    benchmarks: list[BenchmarkComparison]
    matched_internal_share_transfers: list[MatchedShareTransfer]
    unresolved_flow_issue_count: int
    canonical: CanonicalSummary | None
    assumptions: list[str]


class BenchmarkEstimateResult(BaseModel):
    methodology: Literal["benchmark_comparison.estimate"] = METHODOLOGY
    methodology_version: Literal["1"] = METHODOLOGY_VERSION
    as_of: date | None
    benchmarks: list[str]
    model_error_per_180_days: Decimal
    windows: list[LookbackComparison]


@dataclass(frozen=True)
class _Scenario:
    opening: Decimal
    ending: Decimal


def _full_book(point: DailyValuePoint, holding_accounts: frozenset[int]) -> bool:
    if point.basis != "marked_positions":
        return True
    return holding_accounts <= frozenset(point.covered_account_ids or ())


def _boundary(
    points: Sequence[DailyValuePoint],
    target: date,
    holding_accounts: frozenset[int],
    bases: Iterable[ValueBasis],
) -> list[DailyValuePoint]:
    """Latest point per basis on or within six days before ``target``."""
    floor = target - timedelta(days=_BOUNDARY_TOLERANCE_DAYS)
    chosen: dict[ValueBasis, DailyValuePoint] = {}
    for point in points:
        if not floor <= point.as_of <= target or point.basis not in bases:
            continue
        if not _full_book(point, holding_accounts):
            continue
        current = chosen.get(point.basis)
        if current is None or current.as_of < point.as_of:
            chosen[point.basis] = point
    if not chosen:
        return []
    latest = max(point.as_of for point in chosen.values())
    return sorted(
        (point for point in chosen.values() if point.as_of == latest),
        key=lambda point: _BASIS_PREFERENCE.index(point.basis),
    )


def _benchmark_closes(
    session: Session, tickers: Sequence[str], start: date, end: date
) -> dict[str, dict[ReturnBasis, dict[date, Decimal]]]:
    """Total-return and raw closes kept apart so one window never mixes the two."""
    rows = session.execute(
        select(Benchmark.symbol, Benchmark.date, Benchmark.total_return_close, Benchmark.close)
        .where(Benchmark.symbol.in_(tickers))
        .where(Benchmark.date >= start - timedelta(days=_PRICE_LOOKBACK_DAYS))
        .where(Benchmark.date <= end)
    ).all()
    out: dict[str, dict[ReturnBasis, dict[date, Decimal]]] = {
        ticker: {"total_return_adjusted": {}, "raw_price_fallback": {}} for ticker in tickers
    }
    for symbol, close_date, total_return_close, close in rows:
        if total_return_close is not None and Decimal(total_return_close) > 0:
            out[symbol]["total_return_adjusted"][close_date] = Decimal(total_return_close)
        if close is not None and Decimal(close) > 0:
            out[symbol]["raw_price_fallback"][close_date] = Decimal(close)
    return out


def _close_on_or_before(closes: dict[date, Decimal], target: date) -> Decimal | None:
    for offset in range(_PRICE_LOOKBACK_DAYS + 1):
        close = closes.get(target - timedelta(days=offset))
        if close is not None:
            return close
    return None


def _matched_share_transfers(
    session: Session, entries: Sequence[ExternalFlowEntry], account_ids: frozenset[int]
) -> dict[str, MatchedShareTransfer]:
    """Derived share-transfer rows whose opposite leg sits in another valued account.

    The canonical ledger prices a share transfer as external when its opposite
    leg is withheld (for example an unresolved provenance decision). When both
    legs are present in the account universe with equal quantity, the shares
    stayed inside the portfolio, so the estimate treats the move as internal.
    """
    matched: dict[str, MatchedShareTransfer] = {}
    for entry in entries:
        if entry.subtype != "unmatched_share_transfer" or not entry.valuation_price:
            continue
        net_quantity = entry.signed_external_amount / entry.valuation_price
        opposite_subtype = (
            "external_asset_transfer_in" if net_quantity < 0 else "external_asset_transfer_out"
        )
        opposite_accounts = account_ids - frozenset(entry.account_ids)
        if not opposite_accounts or not entry.security_ids:
            continue
        opposite_quantity = session.scalar(
            select(func.coalesce(func.sum(InvestmentTransaction.quantity), 0))
            .where(InvestmentTransaction.date == entry.date)
            .where(InvestmentTransaction.account_id.in_(opposite_accounts))
            .where(InvestmentTransaction.security_id.in_(entry.security_ids))
            .where(InvestmentTransaction.subtype == opposite_subtype)
        )
        if opposite_quantity is None:
            continue
        if abs(abs(Decimal(opposite_quantity)) - abs(net_quantity)) <= _QUANTITY_TOLERANCE * max(
            Decimal(1), abs(net_quantity)
        ):
            matched[entry.flow_id] = MatchedShareTransfer(
                date=entry.date,
                ticker=entry.ticker,
                flow_id=entry.flow_id,
                withheld_amount=entry.signed_external_amount,
            )
    return matched


def _modified_dietz(
    opening: Decimal,
    ending: Decimal,
    start: date,
    end: date,
    flows: Sequence[tuple[date, Decimal]],
) -> tuple[Decimal, Decimal, Decimal]:
    days = Decimal((end - start).days)
    net = sum((amount for _, amount in flows), Decimal(0))
    weighted = sum(
        (amount * Decimal((end - flow_date).days) / days for flow_date, amount in flows),
        Decimal(0),
    )
    denominator = opening + weighted
    return (ending - opening - net) / denominator, net, denominator


def _band(values: Sequence[Decimal], central: Decimal) -> Band:
    return Band(central=central, low=min(values), high=max(values))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))


def _compare_window(
    session: Session,
    lookback_days: int,
    end_point: list[DailyValuePoint],
    points: Sequence[DailyValuePoint],
    holding_accounts: frozenset[int],
    account_ids: frozenset[int],
    tickers: Sequence[str],
    model_error_per_180_days: Decimal,
    earliest_observed: date | None,
) -> LookbackComparison:
    end = end_point[0].as_of
    opening_points = _boundary(
        points, end - timedelta(days=lookback_days), holding_accounts, _BASIS_PREFERENCE
    )
    empty = LookbackComparison(
        lookback_days=lookback_days,
        tier="unavailable",
        unavailable_reason="no_retained_value_near_opening_date",
        start_date=None,
        end_date=end,
        opening=[],
        ending=[BoundaryValue(as_of=p.as_of, basis=p.basis, value=p.value) for p in end_point],
        opening_is_modeled=False,
        opening_error_fraction=Decimal(0),
        net_external_cashflow_in=None,
        modified_dietz_denominator=None,
        portfolio_return_pct=None,
        benchmarks=[],
        matched_internal_share_transfers=[],
        unresolved_flow_issue_count=0,
        canonical=None,
        assumptions=[],
    )
    if not opening_points:
        return empty
    start = opening_points[0].as_of
    opening_is_modeled = all(p.basis == "legacy_cached_model" for p in opening_points)
    error = Decimal(0)
    if opening_is_modeled and earliest_observed is not None and earliest_observed > start:
        error = model_error_per_180_days * Decimal((earliest_observed - start).days) / Decimal(180)

    ledger = build_external_flow_ledger(session, start, end, account_ids=account_ids)
    matched = _matched_share_transfers(session, ledger.entries, account_ids)
    flows = [
        (entry.date, entry.signed_external_amount)
        for entry in ledger.entries
        if entry.classification != "internal"
        and entry.flow_id not in matched
        and start < entry.date <= end
    ]

    scenarios = [
        _Scenario(opening=o.value * (1 + sign * error), ending=e.value)
        for o in opening_points
        for e in end_point
        for sign in ((Decimal(-1), Decimal(0), Decimal(1)) if error else (Decimal(0),))
    ]
    central_scenario = _Scenario(opening=opening_points[0].value, ending=end_point[0].value)
    results = [
        _modified_dietz(s.opening, s.ending, start, end, flows)
        for s in [central_scenario, *scenarios]
    ]
    if any(scenario_denominator <= 0 for _, _, scenario_denominator in results):
        return empty.model_copy(
            update={
                "unavailable_reason": "non_positive_modified_dietz_denominator",
                "start_date": start,
            }
        )
    central_return, net, denominator = results[0]
    portfolio_band = _band(
        [_quantize(r * 100) for r, _, _ in results], _quantize(central_return * 100)
    )

    closes = _benchmark_closes(session, tickers, start, end)
    comparisons: list[BenchmarkComparison] = []
    for ticker in tickers:
        needed = [start, end, *(flow_date for flow_date, _ in flows)]
        resolved: dict[date, Decimal] = {}
        return_basis: ReturnBasis | None = None
        for basis in ("total_return_adjusted", "raw_price_fallback"):
            series = closes.get(ticker, {}).get(basis, {})
            prices = {day: _close_on_or_before(series, day) for day in needed}
            if all(price is not None for price in prices.values()):
                resolved = {day: price for day, price in prices.items() if price is not None}
                return_basis = basis
                break
        if return_basis is None:
            comparisons.append(
                BenchmarkComparison(
                    ticker=ticker, available=False, return_pct=None, gap_pp=None, dollar_gap=None
                )
            )
            continue
        bench_returns: list[Decimal] = []
        gaps: list[Decimal] = []
        dollars: list[Decimal] = []
        for scenario in [central_scenario, *scenarios]:
            shares = scenario.opening / resolved[start] + sum(
                (amount / resolved[flow_date] for flow_date, amount in flows), Decimal(0)
            )
            bench_end = shares * resolved[end]
            port_return, scenario_net, scenario_denominator = _modified_dietz(
                scenario.opening, scenario.ending, start, end, flows
            )
            bench_return = (bench_end - scenario.opening - scenario_net) / scenario_denominator
            bench_returns.append(_quantize(bench_return * 100))
            gaps.append(_quantize((port_return - bench_return) * 100))
            dollars.append(_quantize(scenario.ending - bench_end))
        comparisons.append(
            BenchmarkComparison(
                ticker=ticker,
                available=True,
                return_basis=return_basis,
                return_pct=_band(bench_returns, bench_returns[0]),
                gap_pp=_band(gaps, gaps[0]),
                dollar_gap=_band(dollars, dollars[0]),
            )
        )

    canonical_series = compute_performance_series(session, start, end)
    canonical_return = None
    if canonical_series.calculation_status == "available" and canonical_series.points:
        canonical_return = canonical_series.points[-1].portfolio_return_pct
    canonical = CanonicalSummary(
        calculation_status=canonical_series.calculation_status,
        reason_codes=list(canonical_series.calculation_reason_codes),
        portfolio_return_pct=canonical_return,
    )

    assumptions = [
        "Share transfers whose opposite leg is in another valued account are internal.",
        "Accounts without holdings evidence contribute flows but no value of their own.",
    ]
    unresolved_issue_count = sum(1 for issue in ledger.issues if start < issue.date <= end)
    if unresolved_issue_count:
        assumptions.append(
            f"{unresolved_issue_count} flow-ledger issue(s) in the window are excluded; "
            "the band does not cover them."
        )
    if any(c.return_basis == "raw_price_fallback" for c in comparisons):
        assumptions.append("A raw_price_fallback benchmark excludes dividends.")
    if opening_is_modeled:
        assumptions.append(
            "Opening value is the retained transaction-walk model; its band widens by "
            f"{model_error_per_180_days * 100}% per 180 days before the earliest observed book."
        )
    return LookbackComparison(
        lookback_days=lookback_days,
        tier="canonical_available" if canonical.calculation_status == "available" else "estimate",
        unavailable_reason=None,
        start_date=start,
        end_date=end,
        opening=[
            BoundaryValue(as_of=p.as_of, basis=p.basis, value=p.value) for p in opening_points
        ],
        ending=[BoundaryValue(as_of=p.as_of, basis=p.basis, value=p.value) for p in end_point],
        opening_is_modeled=opening_is_modeled,
        opening_error_fraction=_quantize(error),
        net_external_cashflow_in=net,
        modified_dietz_denominator=denominator,
        portfolio_return_pct=portfolio_band,
        benchmarks=comparisons,
        matched_internal_share_transfers=list(matched.values()),
        unresolved_flow_issue_count=unresolved_issue_count,
        canonical=canonical,
        assumptions=assumptions,
    )


def build_benchmark_estimate(
    session: Session,
    *,
    end_date: date | None = None,
    lookback_days: Sequence[int] = DEFAULT_LOOKBACK_DAYS,
    benchmarks: Sequence[str] = DEFAULT_BENCHMARKS,
    model_error_per_180_days: Decimal = DEFAULT_MODEL_ERROR_PER_180_DAYS,
) -> BenchmarkEstimateResult:
    """Compare the portfolio with each benchmark over each lookback window."""
    tickers = [ticker.strip().upper() for ticker in benchmarks if ticker.strip()]
    resolved_end = end_date or date.today()
    history_start = resolved_end - timedelta(
        days=max(lookback_days, default=0) + _BOUNDARY_TOLERANCE_DAYS
    )
    account_ids, points = build_daily_value_points(session, history_start, resolved_end)
    holding_accounts = valued_account_ids(session) & account_ids
    observed = [
        p
        for p in points
        if p.basis in _OBSERVED_BASES
        and p.basis != "legacy_cached_snapshot"
        and _full_book(p, holding_accounts)
    ]
    end_point: list[DailyValuePoint] = []
    if observed:
        latest = max(p.as_of for p in observed)
        end_point = _boundary(points, latest, holding_accounts, _OBSERVED_BASES)
        end_point = [p for p in end_point if p.as_of == latest]
    earliest_observed = min(
        (p.as_of for p in points if p.basis in _OBSERVED_BASES and _full_book(p, holding_accounts)),
        default=None,
    )
    windows: list[LookbackComparison] = []
    for days in lookback_days:
        if not end_point:
            windows.append(
                LookbackComparison(
                    lookback_days=days,
                    tier="unavailable",
                    unavailable_reason="no_observed_ending_value",
                    start_date=None,
                    end_date=None,
                    opening=[],
                    ending=[],
                    opening_is_modeled=False,
                    opening_error_fraction=Decimal(0),
                    net_external_cashflow_in=None,
                    modified_dietz_denominator=None,
                    portfolio_return_pct=None,
                    benchmarks=[],
                    matched_internal_share_transfers=[],
                    unresolved_flow_issue_count=0,
                    canonical=None,
                    assumptions=[],
                )
            )
            continue
        windows.append(
            _compare_window(
                session,
                days,
                end_point,
                points,
                holding_accounts,
                account_ids,
                tickers,
                model_error_per_180_days,
                earliest_observed,
            )
        )
    return BenchmarkEstimateResult(
        as_of=end_point[0].as_of if end_point else None,
        benchmarks=tickers,
        model_error_per_180_days=model_error_per_180_days,
        windows=windows,
    )
