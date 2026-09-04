"""Fail-closed split-normalized position price/trade comparison.

Methodology:
  * Treat window_start as a fresh balance sheet. For each ticker the user
    held on that date, the "starting capital" is qty_at_start × price_at_start.
    The pre-window buy history is IRRELEVANT — what matters is the value the
    user controlled at the window's start.
  * Within the window, track every buy (cash out of ticker, into SPY counter-
    factual) and every sell (cash in from ticker, out of SPY counterfactual)
    using dollar-matched conversions at each event's date.
  * V_end is qty_at_end × price_at_end (or today_value if end_date=today).
  * Actual P&L on ticker = V_end + sold − bought − V_start.
  * SPY counterfactual: imagine V_start in SPY at window_start_price plus the
    same per-event $ cashflows applied as SPY share buys/sells at SPY price
    on each event date.
  * Alpha = Actual P&L − SPY counterfactual P&L = (V_end_ticker − V_end_spy).

This handles pre-history positions cleanly: a ticker the user held going
INTO the window starts with the right V_start; the pre-window buy cost is
not part of the comparison. The pair (V_start, V_end, in-window cashflows)
is sufficient.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal, TypedDict

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Benchmark,
    CostBasisOverride,
    HoldingSnapshot,
    InvestmentTransaction,
    InvestmentTransactionType,
    Price,
    Security,
)
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.performance import (
    modified_dietz_denominators_are_positive,
    modified_dietz_series,
    transaction_quantity_delta,
)
from portfolio_tracker.services.policy import load_policy_weights
from portfolio_tracker.services.splits import load_split_factors

# Skip these from the per-ticker view — they're cash-equivalent vehicles
# whose "alpha vs SPY" doesn't carry meaning (they're effectively cash).
_CASH_EQUIV_TICKERS: frozenset[str] = frozenset(
    {"SGOV", "FDRXX", "SHV", "SPAXX", "CUR:USD", "VMFXX"}
)

# Broad-market US-equity ETFs — same set used by the performance service.
# When `exclude_broad_index=True` the alpha view skips these positions and
# does NOT include them as cashflows (they're a passive allocation that
# tracks the index by definition; nothing to alpha-evaluate).
_BROAD_INDEX_TICKERS: frozenset[str] = frozenset({"VTI", "VOO", "SPY", "IVV", "RSP"})

CalculationStatus = Literal["available", "unavailable"]

_NO_ACTIVE_ACCOUNTS = "no_active_accounts"
_NO_INVESTED_POSITION_CAPITAL = "no_invested_position_capital"
_NONPOSITIVE_DIETZ_DENOMINATOR = "nonpositive_dietz_denominator"
_PRIMARY_BENCHMARK_PRICE_UNAVAILABLE = "primary_benchmark_price_unavailable"
_POSITION_PRICE_UNAVAILABLE = "position_price_unavailable"
_SHARE_MOVEMENT_MISSING_SECURITY = "share_movement_missing_security"
_SHARE_MOVEMENT_MISSING_TICKER = "share_movement_missing_ticker"
_SHARE_MOVEMENT_UNMATCHED = "share_movement_unmatched"
_SHARE_MOVEMENT_CROSS_DATE = "share_movement_cross_date"
_SHARE_MOVEMENT_PRICE_UNAVAILABLE = "share_movement_price_unavailable"
_SHARE_MOVEMENT_UNCLASSIFIED = "share_movement_unclassified"
_TRADE_SECURITY_MISSING = "trade_security_missing"
_TRADE_TICKER_MISSING = "trade_ticker_missing"
_TRADE_NOTIONAL_UNAVAILABLE = "trade_notional_unavailable"
_MAX_ASOF_PRICE_AGE_DAYS = 14

# These broker security types are denominated in directly priced shares or
# units, so quantity × execution price is a proven notional. Derivatives,
# bonds, and unknown provider types may require a contract/face-value
# multiplier that the persisted schema does not carry; they must fail closed.
_DIRECT_PRICE_UNIT_SECURITY_TYPES: frozenset[str] = frozenset(
    {
        "cs",
        "stock",
        "equity",
        "common stock",
        "preferred stock",
        "ps",
        "ad",
        "adr",
        "et",
        "etf",
        "etn",
        "oef",
        "cef",
        "mutual fund",
        "mutualfund",
        "mf",
        "fund",
        "cryptocurrency",
        "crypto",
    }
)

_TRANSFER_CASH_SUBTYPES: frozenset[str] = frozenset(
    {"external_asset_transfer_in", "external_asset_transfer_out"}
)
_NON_TRANSFER_SHARE_SUBTYPES: frozenset[str] = frozenset(
    {
        "rei",
        "reinvestment",
        "drip",
        "optionassignment",
        "optionexpiration",
        "assignment",
        "exercise",
        "merger",
        "spin off",
        "split",
        "stock distribution",
    }
)


@dataclass(frozen=True)
class _PositionEvent:
    """One normalized event in the selected invested-position universe."""

    date: date
    ticker: str
    quantity_delta: Decimal
    capital_flow: Decimal


@dataclass(frozen=True)
class _PositionEventLedger:
    events: tuple[_PositionEvent, ...]
    calculation_reason_codes: tuple[str, ...]


class PositionAlphaRow(BaseModel):
    ticker: str
    name: str | None
    value_at_start: Decimal  # qty_start × price_start
    bought_in_window: Decimal  # sum of buy $
    sold_in_window: Decimal  # sum of sell $
    value_at_end: Decimal  # qty_end × price_end
    actual_pl: Decimal | None  # V_end + sold − bought − V_start
    spy_counterfactual_pl: Decimal | None
    qqq_counterfactual_pl: Decimal | None
    policy_counterfactual_pl: Decimal | None
    alpha: Decimal | None  # actual_pl − spy_counterfactual_pl (primary)
    alpha_vs_qqq: Decimal | None
    alpha_vs_policy: Decimal | None
    # Diagnostic — if walk-back couldn't determine qty_start (no price data,
    # no transactions), this fires and the row is approximate.
    incomplete: bool


class PositionAlphaTimePoint(BaseModel):
    """One day on the dashboard chart.

    `portfolio_value` is the aggregate position dollar value at day d.
    The benchmark `*_counterfactual_value` fields apply the same starting-
    capital + dollar-matched-cashflow methodology to each benchmark.

    `position_cashflow` is the net $ that moved INTO active positions on
    day d (buys minus sells). Used by the risk-metrics regression to
    subtract trade-driven V changes from daily returns so the regression
    captures only market-driven moves.
    """

    date: date
    portfolio_value: Decimal
    spy_counterfactual_value: Decimal | None
    qqq_counterfactual_value: Decimal | None
    policy_counterfactual_value: Decimal | None
    position_cashflow: Decimal = Decimal(0)
    portfolio_return_pct: Decimal | None = None
    spy_return_pct: Decimal | None = None
    qqq_return_pct: Decimal | None = None
    policy_return_pct: Decimal | None = None


class PositionMatchedReturns(BaseModel):
    """Invested-position price/trade returns on the position-alpha basis.

    These figures exclude cash-equivalent positions and do not add cash
    dividends, interest, or account fees. Position prices and quantities share
    a split-normalized basis; benchmark legs use non-dividend-adjusted closes.
    Callers must not present this as whole-account total return.
    """

    dietz_denominator: Decimal | None = None
    portfolio_return_pct: Decimal | None = None
    spy_return_pct: Decimal | None = None
    qqq_return_pct: Decimal | None = None
    policy_return_pct: Decimal | None = None
    alpha_vs_spy_pct: Decimal | None = None
    alpha_vs_qqq_pct: Decimal | None = None
    alpha_vs_policy_pct: Decimal | None = None


class PositionAlphaResult(BaseModel):
    methodology: Literal["position_alpha.split_normalized_price_trade_modified_dietz"]
    methodology_version: Literal["3"]
    start_date: date
    end_date: date
    calculation_status: CalculationStatus
    calculation_reason_codes: list[str]
    rows: list[PositionAlphaRow]
    total_actual_pl: Decimal | None
    total_spy_pl: Decimal | None
    total_qqq_pl: Decimal | None
    total_policy_pl: Decimal | None
    total_alpha: Decimal | None  # vs SPY (primary)
    total_alpha_vs_qqq: Decimal | None
    total_alpha_vs_policy: Decimal | None
    series: list[PositionAlphaTimePoint] = []
    v_start: Decimal = Decimal(0)
    v_end: Decimal = Decimal(0)
    has_policy: bool = False
    matched_returns: PositionMatchedReturns = Field(default_factory=PositionMatchedReturns)


class _TickerAgg(TypedDict):
    name: str | None
    buys: list[tuple[date, Decimal]]
    sells: list[tuple[date, Decimal]]
    sid: int | None


def _new_ticker_agg() -> _TickerAgg:
    return {"name": None, "buys": [], "sells": [], "sid": None}


def compute_position_alpha(
    session: Session,
    start_date: date,
    end_date: date,
    exclude_broad_index: bool = False,
) -> PositionAlphaResult:
    """Build the per-ticker windowed alpha breakdown for [start_date, end_date].

    `exclude_broad_index` drops VTI/VOO/SPY/IVV/RSP from the per-ticker rows
    (they're passive index allocation and 'alpha vs SPY' on them is ~zero
    by construction). The remaining rows give a focused view of active picks.

    The $30k SGOV reserve carve-out is implicitly handled: SGOV is in
    `_CASH_EQUIV_TICKERS` and always skipped. There's no separate reserve
    parameter — the position-alpha methodology measures positions only,
    so cash carve-outs don't shift the comparison.
    """
    accts = active_account_ids(session)
    if not accts:
        return PositionAlphaResult(
            methodology="position_alpha.split_normalized_price_trade_modified_dietz",
            methodology_version="3",
            start_date=start_date,
            end_date=end_date,
            calculation_status="unavailable",
            calculation_reason_codes=[_NO_ACTIVE_ACCOUNTS],
            rows=[],
            total_actual_pl=None,
            total_spy_pl=None,
            total_qqq_pl=None,
            total_policy_pl=None,
            total_alpha=None,
            total_alpha_vs_qqq=None,
            total_alpha_vs_policy=None,
        )

    # 1. Quantities per ticker at start_date and end_date (walk-back if needed)
    qty_at_start = _qty_per_ticker_at_date(session, start_date, accts)
    qty_at_end = _qty_per_ticker_at_date(session, end_date, accts)

    # 2. Normalize the window's transaction stream once. Both the table and
    # daily series consume this ledger, so they cannot disagree about a trade
    # amount or silently omit the same quantity-changing event in one path.
    ledger = _load_position_event_ledger(
        session,
        start_date,
        end_date,
        accts,
        exclude_broad_index=exclude_broad_index,
    )
    security_rows = session.execute(
        select(
            Security.ticker,
            Security.name,
            Security.security_id,
            Security.is_cash_equivalent,
        ).where(Security.ticker.is_not(None))
    ).all()
    security_info = {
        ticker.upper(): (name, sid) for ticker, name, sid, _ in security_rows if ticker is not None
    }
    cash_equivalent_tickers = {
        ticker.upper()
        for ticker, _, _, is_cash_equivalent in security_rows
        if ticker is not None and is_cash_equivalent
    }

    by_ticker: defaultdict[str, _TickerAgg] = defaultdict(_new_ticker_agg)
    for event in ledger.events:
        b = by_ticker[event.ticker]
        name, sid = security_info.get(event.ticker, (None, None))
        if b["name"] is None:
            b["name"] = name
        b["sid"] = sid
        if event.capital_flow > 0:
            b["buys"].append((event.date, event.capital_flow))
        elif event.capital_flow < 0:
            b["sells"].append((event.date, -event.capital_flow))

    # Make sure every ticker with a non-zero start or end qty also has an entry
    for t_up, qty in qty_at_start.items():
        if t_up in _CASH_EQUIV_TICKERS or t_up in cash_equivalent_tickers:
            continue
        if exclude_broad_index and t_up in _BROAD_INDEX_TICKERS:
            continue
        if qty != 0 and t_up not in by_ticker:
            name, sid = security_info.get(t_up, (None, None))
            by_ticker[t_up] = {"name": name, "buys": [], "sells": [], "sid": sid}
    for t_up, qty in qty_at_end.items():
        if t_up in _CASH_EQUIV_TICKERS or t_up in cash_equivalent_tickers:
            continue
        if exclude_broad_index and t_up in _BROAD_INDEX_TICKERS:
            continue
        if qty != 0 and t_up not in by_ticker:
            name, sid = security_info.get(t_up, (None, None))
            by_ticker[t_up] = {"name": name, "buys": [], "sells": [], "sid": sid}

    # 3. Price-at-start and price-at-end per ticker
    all_tickers = list(by_ticker.keys())
    if not all_tickers:
        return PositionAlphaResult(
            methodology="position_alpha.split_normalized_price_trade_modified_dietz",
            methodology_version="3",
            start_date=start_date,
            end_date=end_date,
            calculation_status="unavailable",
            calculation_reason_codes=[_NO_INVESTED_POSITION_CAPITAL],
            rows=[],
            total_actual_pl=None,
            total_spy_pl=None,
            total_qqq_pl=None,
            total_policy_pl=None,
            total_alpha=None,
            total_alpha_vs_qqq=None,
            total_alpha_vs_policy=None,
        )
    prices_start = _price_per_ticker_at_date(
        session, all_tickers, start_date, require_position_basis=True
    )
    prices_end = _price_per_ticker_at_date(
        session, all_tickers, end_date, require_position_basis=True
    )
    # The derived actual leg consumes the same proven split basis we gate:
    # split-normalized quantities × eligible yfinance split-adjusted Close.
    # Broker institution_value remains available from holdings endpoints as a
    # raw fact, but mixing that independently sourced mark into this metric
    # would break economic parity with the price-return benchmark leg.
    v_start_by_ticker: dict[str, Decimal] = {}
    v_end_by_ticker: dict[str, Decimal] = {}
    for t_up in by_ticker:
        q_start = qty_at_start.get(t_up, Decimal(0))
        q_end = qty_at_end.get(t_up, Decimal(0))
        p_start = prices_start.get(t_up)
        p_end = prices_end.get(t_up)
        v_start_by_ticker[t_up] = (
            q_start * p_start if (q_start != 0 and p_start is not None) else Decimal(0)
        )
        v_end_by_ticker[t_up] = q_end * p_end if (q_end != 0 and p_end is not None) else Decimal(0)

    price_coverage_unavailable = any(
        (qty_at_start.get(ticker, Decimal(0)) != 0 and prices_start.get(ticker) is None)
        or (qty_at_end.get(ticker, Decimal(0)) != 0 and prices_end.get(ticker) is None)
        for ticker in by_ticker
    )

    # 4. Benchmark closes (SPY, QQQ, and policy basket)
    # This endpoint intentionally measures price/trade return for both the
    # invested positions and their counterfactuals. Whole-account total return
    # (including income and fees) remains owned by the performance service.
    spy_closes = _benchmark_closes_with_lookback(
        session, "SPY", start_date, end_date, total_return=False
    )
    qqq_closes = _benchmark_closes_with_lookback(
        session, "QQQ", start_date, end_date, total_return=False
    )
    policy_weights = load_policy_weights(session)
    has_policy = bool(policy_weights)
    policy_closes_per_ticker: dict[str, dict[date, Decimal]] = {}
    if has_policy:
        for ticker in policy_weights:
            policy_closes_per_ticker[ticker] = _benchmark_closes_with_lookback(
                session, ticker, start_date, end_date, total_return=False
            )

    spy_start = _last_known_price(spy_closes, start_date)
    spy_end = _last_known_price(spy_closes, end_date)
    qqq_start = _last_known_price(qqq_closes, start_date)
    qqq_end = _last_known_price(qqq_closes, end_date)
    spy_available = _benchmark_has_positive_coverage(spy_closes, start_date, end_date)
    qqq_available = _benchmark_has_positive_coverage(qqq_closes, start_date, end_date)
    policy_available = has_policy and _policy_benchmark_available(
        policy_weights,
        policy_closes_per_ticker,
        start_date,
        end_date,
    )
    if not spy_available or spy_start is None or spy_end is None:
        return PositionAlphaResult(
            methodology="position_alpha.split_normalized_price_trade_modified_dietz",
            methodology_version="3",
            start_date=start_date,
            end_date=end_date,
            calculation_status="unavailable",
            calculation_reason_codes=sorted(
                {*ledger.calculation_reason_codes, _PRIMARY_BENCHMARK_PRICE_UNAVAILABLE}
            ),
            rows=[],
            total_actual_pl=None,
            total_spy_pl=None,
            total_qqq_pl=None,
            total_policy_pl=None,
            total_alpha=None,
            total_alpha_vs_qqq=None,
            total_alpha_vs_policy=None,
            has_policy=has_policy,
        )

    series, series_price_coverage_unavailable = _compute_alpha_series(
        session,
        start_date,
        end_date,
        accts,
        list(by_ticker.keys()),
        qty_at_start,
        prices_start,
        spy_closes,
        Decimal(str(spy_start)),
        qqq_closes,
        Decimal(str(qqq_start)) if qqq_start else None,
        policy_weights,
        policy_closes_per_ticker,
        v_start_by_ticker,
        v_end_by_ticker,
        ledger,
    )

    calculation_reason_codes = list(ledger.calculation_reason_codes)
    if price_coverage_unavailable or (
        series_price_coverage_unavailable and not calculation_reason_codes
    ):
        calculation_reason_codes.append(_POSITION_PRICE_UNAVAILABLE)
    opening_capital = sum(
        (value.quantize(Decimal("0.01")) for value in v_start_by_ticker.values()),
        Decimal(0),
    )
    daily_position_cashflow = {point.date: point.position_cashflow for point in series}
    if (
        not calculation_reason_codes
        and _matched_return_denominator(
            start_date=start_date,
            end_date=end_date,
            v_start=opening_capital,
            daily_cashflow=daily_position_cashflow,
        )
        is None
    ):
        calculation_reason_codes.append(_NO_INVESTED_POSITION_CAPITAL)
    elif not calculation_reason_codes and not modified_dietz_denominators_are_positive(
        [point.date for point in series], daily_position_cashflow, opening_capital
    ):
        calculation_reason_codes.append(_NONPOSITIVE_DIETZ_DENOMINATOR)
    calculation_reason_codes = sorted(set(calculation_reason_codes))
    calculation_available = not calculation_reason_codes

    # 5. Compute per-ticker alpha (against SPY, QQQ, POLICY)
    rows: list[PositionAlphaRow] = []
    total_actual = Decimal(0)
    total_spy = Decimal(0)
    total_qqq = Decimal(0)
    total_policy = Decimal(0)

    for t_up, b in by_ticker.items():
        q_start = qty_at_start.get(t_up, Decimal(0))
        q_end = qty_at_end.get(t_up, Decimal(0))
        p_start = prices_start.get(t_up)
        p_end = prices_end.get(t_up)
        # Split-normalized quantity × eligible split-adjusted Close, precomputed
        # once so the table and chart share the exact same derived basis.
        v_start = v_start_by_ticker[t_up]
        v_end = v_end_by_ticker[t_up]

        bought_sum = sum((a for _, a in b["buys"]), Decimal(0))
        sold_sum = sum((a for _, a in b["sells"]), Decimal(0))
        if v_start == 0 and v_end == 0 and bought_sum == 0 and sold_sum == 0:
            continue

        actual_pl = v_end + sold_sum - bought_sum - v_start

        # SPY counterfactual
        spy_pl = _counterfactual_pl(
            v_start,
            b["buys"],
            b["sells"],
            spy_closes,
            Decimal(str(spy_start)),
            Decimal(str(spy_end)),
            bought_sum,
            sold_sum,
        )
        # QQQ counterfactual (NaN-safe — fall back to 0 if no QQQ data)
        qqq_pl = Decimal(0)
        if qqq_available:
            qqq_pl = _counterfactual_pl(
                v_start,
                b["buys"],
                b["sells"],
                qqq_closes,
                Decimal(str(qqq_start)),
                Decimal(str(qqq_end)),
                bought_sum,
                sold_sum,
            )
        # POLICY counterfactual: weighted sum of per-component counterfactuals
        policy_pl = Decimal(0)
        if policy_available:
            policy_pl = _policy_counterfactual_pl(
                v_start,
                b["buys"],
                b["sells"],
                policy_weights,
                policy_closes_per_ticker,
                start_date,
                end_date,
                bought_sum,
                sold_sum,
            )

        alpha = actual_pl - spy_pl
        alpha_qqq = actual_pl - qqq_pl
        alpha_policy = actual_pl - policy_pl

        incomplete = (q_start != 0 and p_start is None) or (q_end != 0 and p_end is None)

        rows.append(
            PositionAlphaRow(
                ticker=t_up,
                name=b["name"],
                value_at_start=v_start.quantize(Decimal("0.01")),
                bought_in_window=bought_sum.quantize(Decimal("0.01")),
                sold_in_window=sold_sum.quantize(Decimal("0.01")),
                value_at_end=v_end.quantize(Decimal("0.01")),
                actual_pl=(actual_pl.quantize(Decimal("0.01")) if calculation_available else None),
                spy_counterfactual_pl=(
                    spy_pl.quantize(Decimal("0.01")) if calculation_available else None
                ),
                qqq_counterfactual_pl=(
                    qqq_pl.quantize(Decimal("0.01"))
                    if calculation_available and qqq_available
                    else None
                ),
                policy_counterfactual_pl=(
                    policy_pl.quantize(Decimal("0.01"))
                    if calculation_available and policy_available
                    else None
                ),
                alpha=(alpha.quantize(Decimal("0.01")) if calculation_available else None),
                alpha_vs_qqq=(
                    alpha_qqq.quantize(Decimal("0.01"))
                    if calculation_available and qqq_available
                    else None
                ),
                alpha_vs_policy=(
                    alpha_policy.quantize(Decimal("0.01"))
                    if calculation_available and policy_available
                    else None
                ),
                incomplete=incomplete,
            )
        )
        total_actual += actual_pl
        total_spy += spy_pl
        total_qqq += qqq_pl
        total_policy += policy_pl

    rows.sort(key=lambda r: (r.alpha is None, r.alpha or Decimal(0), r.ticker))

    agg_v_start = sum((r.value_at_start for r in rows), Decimal(0))
    agg_v_end = sum((r.value_at_end for r in rows), Decimal(0))

    if not calculation_available:
        matched_returns = PositionMatchedReturns()
        series = [
            point.model_copy(
                update={
                    "spy_counterfactual_value": None,
                    "qqq_counterfactual_value": None,
                    "policy_counterfactual_value": None,
                    "portfolio_return_pct": None,
                    "spy_return_pct": None,
                    "qqq_return_pct": None,
                    "policy_return_pct": None,
                }
            )
            for point in series
        ]
    else:
        matched_returns = _matched_return_summary(
            start_date=start_date,
            end_date=end_date,
            v_start=agg_v_start,
            daily_cashflow=daily_position_cashflow,
            actual_pl=total_actual,
            spy_pl=total_spy,
            qqq_pl=total_qqq if qqq_available else None,
            policy_pl=total_policy if policy_available else None,
        )
        series = _attach_matched_return_series(
            series,
            v_start=agg_v_start,
            include_qqq=qqq_available,
            include_policy=policy_available,
        )

    return PositionAlphaResult(
        methodology="position_alpha.split_normalized_price_trade_modified_dietz",
        methodology_version="3",
        start_date=start_date,
        end_date=end_date,
        calculation_status="available" if calculation_available else "unavailable",
        calculation_reason_codes=calculation_reason_codes,
        rows=rows,
        total_actual_pl=(total_actual.quantize(Decimal("0.01")) if calculation_available else None),
        total_spy_pl=(total_spy.quantize(Decimal("0.01")) if calculation_available else None),
        total_qqq_pl=(
            total_qqq.quantize(Decimal("0.01")) if calculation_available and qqq_available else None
        ),
        total_policy_pl=(
            total_policy.quantize(Decimal("0.01"))
            if calculation_available and policy_available
            else None
        ),
        total_alpha=(
            (total_actual - total_spy).quantize(Decimal("0.01")) if calculation_available else None
        ),
        total_alpha_vs_qqq=(
            (total_actual - total_qqq).quantize(Decimal("0.01"))
            if calculation_available and qqq_available
            else None
        ),
        total_alpha_vs_policy=(
            (total_actual - total_policy).quantize(Decimal("0.01"))
            if calculation_available and policy_available
            else None
        ),
        series=series,
        v_start=agg_v_start.quantize(Decimal("0.01")),
        v_end=agg_v_end.quantize(Decimal("0.01")),
        has_policy=has_policy,
        matched_returns=matched_returns,
    )


def _matched_return_summary(
    *,
    start_date: date,
    end_date: date,
    v_start: Decimal,
    daily_cashflow: dict[date, Decimal],
    actual_pl: Decimal,
    spy_pl: Decimal,
    qqq_pl: Decimal | None,
    policy_pl: Decimal | None,
) -> PositionMatchedReturns:
    """Return percentages whose denominator exactly matches dollar alpha.

    All legs share V_start and the same dated buys-minus-sells. Therefore the
    percentage spread is necessarily dollar alpha divided by this denominator.
    This is an invested-position price/trade measure, not whole-account total
    return: cash income, fees, and cash-equivalent positions are outside it.
    """
    denominator = _matched_return_denominator(
        start_date=start_date,
        end_date=end_date,
        v_start=v_start,
        daily_cashflow=daily_cashflow,
    )
    if denominator is None:
        return PositionMatchedReturns()

    pct_unit = Decimal("0.0001")

    def pct(pl: Decimal | None) -> Decimal | None:
        if pl is None:
            return None
        return (pl / denominator * Decimal(100)).quantize(pct_unit)

    return PositionMatchedReturns(
        dietz_denominator=denominator.quantize(Decimal("0.01")),
        portfolio_return_pct=pct(actual_pl),
        spy_return_pct=pct(spy_pl),
        qqq_return_pct=pct(qqq_pl),
        policy_return_pct=pct(policy_pl),
        alpha_vs_spy_pct=pct(actual_pl - spy_pl),
        alpha_vs_qqq_pct=pct(actual_pl - qqq_pl) if qqq_pl is not None else None,
        alpha_vs_policy_pct=pct(actual_pl - policy_pl) if policy_pl is not None else None,
    )


def _matched_return_denominator(
    *,
    start_date: date,
    end_date: date,
    v_start: Decimal,
    daily_cashflow: dict[date, Decimal],
) -> Decimal | None:
    """Return the positive Modified-Dietz capital base, if one exists."""
    period_days = (end_date - start_date).days
    if period_days <= 0:
        return None

    weighted_cashflow = Decimal(0)
    for flow_date, amount in daily_cashflow.items():
        if flow_date <= start_date or flow_date > end_date or amount == 0:
            continue
        weight = Decimal((end_date - flow_date).days) / Decimal(period_days)
        weighted_cashflow += amount * weight
    denominator = v_start + weighted_cashflow
    return denominator if denominator > 0 else None


def _trade_capital_amount(
    tx: InvestmentTransaction,
    security_type: str | None,
) -> Decimal | None:
    """Return fee-exclusive trade notional, or ``None`` when unprovable.

    Both ingest adapters persist provider ``amount`` and ``fees`` separately,
    but they do not normalize whether a provider's amount embeds the fee. The
    directly provable gross security notional is therefore quantity × execution
    price, but only for security types known to be directly priced shares or
    units. Derivatives, bonds, and unknown types can require an unavailable
    contract or face-value multiplier and always fail closed. A provider amount
    is safe only for a known direct-price-unit security when the provider
    explicitly reported zero fees.
    """
    security_type_norm = (security_type or "").strip().lower()
    if security_type_norm not in _DIRECT_PRICE_UNIT_SECURITY_TYPES:
        return None
    quantity = abs(Decimal(tx.quantity or 0))
    price = Decimal(tx.price) if tx.price is not None else None
    if quantity > 0 and price is not None and price > 0:
        return quantity * price
    fees = Decimal(tx.fees) if tx.fees is not None else None
    amount = abs(Decimal(tx.amount or 0))
    if quantity > 0 and fees == 0 and amount > 0:
        return amount
    return None


def _load_position_event_ledger(
    session: Session,
    start_date: date,
    end_date: date,
    accts: frozenset[int],
    *,
    exclude_broad_index: bool,
) -> _PositionEventLedger:
    """Normalize dated trades and conservatively assess share movements.

    BUY/SELL events carry a fee-exclusive capital flow. Only compatible
    transfer-family legs may cancel: same date and normalized ticker, exact
    opposite net quantity, and distinct source/destination accounts. REI,
    assignment, expiration, mixed event families, and every transfer residual
    are replayed for raw position values but make derived P&L unavailable.
    """
    transactions = list(
        session.execute(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.account_id.in_(accts))
            .where(InvestmentTransaction.date > start_date)
            .where(InvestmentTransaction.date <= end_date)
            .order_by(
                InvestmentTransaction.date.asc(),
                InvestmentTransaction.plaid_investment_transaction_id.asc(),
            )
        )
        .scalars()
        .all()
    )
    if not transactions:
        return _PositionEventLedger(events=(), calculation_reason_codes=())

    security_ids = frozenset(tx.security_id for tx in transactions if tx.security_id is not None)
    security_by_id = {
        sid: (ticker, is_cash_equivalent, security_type)
        for sid, ticker, is_cash_equivalent, security_type in session.execute(
            select(
                Security.security_id,
                Security.ticker,
                Security.is_cash_equivalent,
                Security.type,
            ).where(Security.security_id.in_(security_ids))
        ).all()
    }
    split_factors = load_split_factors(session, security_ids)
    events: list[_PositionEvent] = []
    reasons: set[str] = set()
    transfer_movements: dict[tuple[date, str], list[tuple[int, Decimal]]] = defaultdict(list)

    for tx in transactions:
        delta = transaction_quantity_delta(tx)
        is_trade = tx.type in {
            InvestmentTransactionType.BUY.value,
            InvestmentTransactionType.SELL.value,
        }
        if not is_trade and delta is None:
            if Decimal(tx.quantity or 0) == 0:
                continue
            # A provider introduced a quantity-bearing event whose direction
            # semantics are not in our canonical transaction convention. Keep
            # the raw signed quantity in the ledger for diagnostic replay, but
            # never publish derived returns until the event is classified.
            reasons.add(_SHARE_MOVEMENT_UNCLASSIFIED)
            delta = Decimal(tx.quantity)

        if tx.security_id is None:
            reasons.add(_TRADE_SECURITY_MISSING if is_trade else _SHARE_MOVEMENT_MISSING_SECURITY)
            continue
        security = security_by_id.get(tx.security_id)
        if security is None:
            reasons.add(_TRADE_SECURITY_MISSING if is_trade else _SHARE_MOVEMENT_MISSING_SECURITY)
            continue
        ticker, is_cash_equivalent, security_type = security
        if ticker is None or not ticker.strip():
            reasons.add(_TRADE_TICKER_MISSING if is_trade else _SHARE_MOVEMENT_MISSING_TICKER)
            continue
        ticker_norm = ticker.strip().upper()
        if is_cash_equivalent or ticker_norm in _CASH_EQUIV_TICKERS:
            continue
        if exclude_broad_index and ticker_norm in _BROAD_INDEX_TICKERS:
            continue

        quantity_delta = Decimal(delta or 0) * split_factors.factor_after(tx.security_id, tx.date)
        if is_trade:
            notional = _trade_capital_amount(tx, security_type)
            if notional is None:
                reasons.add(_TRADE_NOTIONAL_UNAVAILABLE)
                events.append(
                    _PositionEvent(
                        date=tx.date,
                        ticker=ticker_norm,
                        quantity_delta=quantity_delta,
                        capital_flow=Decimal(0),
                    )
                )
                continue
            capital_flow = notional if tx.type == InvestmentTransactionType.BUY.value else -notional
            events.append(
                _PositionEvent(
                    date=tx.date,
                    ticker=ticker_norm,
                    quantity_delta=quantity_delta,
                    capital_flow=capital_flow,
                )
            )
            continue
        subtype = (tx.subtype or "").strip().lower()
        name = (tx.name or "").strip().lower()
        has_internal_share_name = "reinvestment" in name or "drip" in name
        is_transfer_family = (
            tx.type == InvestmentTransactionType.TRANSFER.value
            and subtype not in _NON_TRANSFER_SHARE_SUBTYPES
            and not has_internal_share_name
        ) or (
            tx.type == InvestmentTransactionType.CASH.value and subtype in _TRANSFER_CASH_SUBTYPES
        )
        if not is_transfer_family:
            # REI, assignment, expiration, and any other share-changing event
            # need event-specific economics. Opposite signed quantities do not
            # prove that these heterogeneous events are an internal transfer.
            reasons.add(_SHARE_MOVEMENT_UNCLASSIFIED)
            events.append(
                _PositionEvent(
                    date=tx.date,
                    ticker=ticker_norm,
                    quantity_delta=quantity_delta,
                    capital_flow=Decimal(0),
                )
            )
            continue
        transfer_movements[(tx.date, ticker_norm)].append((tx.account_id, quantity_delta))

    residuals: dict[tuple[date, str], Decimal] = {}
    for key, legs in transfer_movements.items():
        total = sum((quantity for _, quantity in legs), Decimal(0))
        positive_accounts = {account_id for account_id, quantity in legs if quantity > 0}
        negative_accounts = {account_id for account_id, quantity in legs if quantity < 0}
        is_internal_pair = (
            total == 0
            and positive_accounts
            and negative_accounts
            and positive_accounts.isdisjoint(negative_accounts)
        )
        if is_internal_pair:
            continue
        residuals[key] = total
        if total == 0:
            # Same-account reversals or other incompatible legs are not proof
            # of an internal account-to-account transfer. Preserve each leg for
            # diagnostic replay and fail closed.
            reasons.add(_SHARE_MOVEMENT_UNMATCHED)
            movement_date, ticker = key
            events.extend(
                _PositionEvent(
                    date=movement_date,
                    ticker=ticker,
                    quantity_delta=quantity,
                    capital_flow=Decimal(0),
                )
                for _, quantity in legs
            )

    residuals_by_ticker: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    for (movement_date, ticker), quantity in residuals.items():
        residuals_by_ticker[ticker].append((movement_date, quantity))
        if quantity != 0:
            events.append(
                _PositionEvent(
                    date=movement_date,
                    ticker=ticker,
                    quantity_delta=quantity,
                    capital_flow=Decimal(0),
                )
            )
        if (
            _price_per_ticker_at_date(
                session,
                [ticker],
                movement_date,
                require_position_basis=True,
            ).get(ticker)
            is None
        ):
            reasons.add(_SHARE_MOVEMENT_PRICE_UNAVAILABLE)

    for ticker_residuals in residuals_by_ticker.values():
        has_in = any(quantity > 0 for _, quantity in ticker_residuals)
        has_out = any(quantity < 0 for _, quantity in ticker_residuals)
        if has_in and has_out and len({d for d, _ in ticker_residuals}) > 1:
            reasons.add(_SHARE_MOVEMENT_CROSS_DATE)
        if sum((quantity for _, quantity in ticker_residuals), Decimal(0)) != 0:
            reasons.add(_SHARE_MOVEMENT_UNMATCHED)
        elif has_in and has_out:
            reasons.add(_SHARE_MOVEMENT_CROSS_DATE)

    events.sort(key=lambda event: (event.date, event.ticker, event.capital_flow))
    return _PositionEventLedger(
        events=tuple(events),
        calculation_reason_codes=tuple(sorted(reasons)),
    )


def _attach_matched_return_series(
    series: list[PositionAlphaTimePoint],
    *,
    v_start: Decimal,
    include_qqq: bool,
    include_policy: bool,
) -> list[PositionAlphaTimePoint]:
    if not series:
        return series
    dates = [point.date for point in series]
    cashflows = {point.date: point.position_cashflow for point in series}

    def returns_for(field: str) -> dict[date, Decimal]:
        values = {point.date: getattr(point, field) for point in series}
        return modified_dietz_series(dates, values, cashflows, v_start)

    portfolio_returns = returns_for("portfolio_value")
    spy_returns = returns_for("spy_counterfactual_value")
    qqq_returns = returns_for("qqq_counterfactual_value") if include_qqq else {}
    policy_returns = returns_for("policy_counterfactual_value") if include_policy else {}
    pct_unit = Decimal("0.0001")

    return [
        point.model_copy(
            update={
                "portfolio_return_pct": portfolio_returns.get(point.date, Decimal(0)).quantize(
                    pct_unit
                ),
                "spy_return_pct": spy_returns.get(point.date, Decimal(0)).quantize(pct_unit),
                "qqq_return_pct": (
                    qqq_returns.get(point.date, Decimal(0)).quantize(pct_unit)
                    if include_qqq
                    else None
                ),
                "policy_return_pct": (
                    policy_returns.get(point.date, Decimal(0)).quantize(pct_unit)
                    if include_policy
                    else None
                ),
            }
        )
        for point in series
    ]


def _counterfactual_pl(
    v_start: Decimal,
    buys: list[tuple[date, Decimal]],
    sells: list[tuple[date, Decimal]],
    closes: dict[date, Decimal],
    start_price: Decimal,
    end_price: Decimal,
    bought_sum: Decimal,
    sold_sum: Decimal,
) -> Decimal:
    """Run the dollar-matched-cashflow counterfactual against one benchmark series."""
    if start_price == 0:
        return Decimal(0)
    shares = (v_start / start_price) if v_start != 0 else Decimal(0)
    for d, a in buys:
        px = _last_known_price(closes, d)
        if px and px > 0:
            shares += a / px
    for d, a in sells:
        px = _last_known_price(closes, d)
        if px and px > 0:
            shares -= a / px
    end_value = shares * end_price
    return end_value + sold_sum - bought_sum - v_start


def _policy_counterfactual_pl(
    v_start: Decimal,
    buys: list[tuple[date, Decimal]],
    sells: list[tuple[date, Decimal]],
    weights: dict[str, Decimal],
    closes_per_ticker: dict[str, dict[date, Decimal]],
    start_date: date,
    end_date: date,
    bought_sum: Decimal,
    sold_sum: Decimal,
) -> Decimal:
    """Weighted-basket counterfactual: split each $ across policy tickers.

    Missing-data handling: components with no price on a given date are
    skipped, with remaining weights renormalized to sum to 1.
    """
    # Sort each policy ticker's price series once; every lot below reuses the
    # same date-memoized index (see `_BasketIndex`).
    index = _BasketIndex(weights, closes_per_ticker)
    end_value = Decimal(0)
    # V_start lot, split across policy tickers
    end_value += _basket_value_at(v_start, index, start_date, end_date)
    # Each buy: add $ to basket on buy date
    for d, a in buys:
        end_value += _basket_value_at(a, index, d, end_date)
    # Each sell: remove $ from basket on sell date (basket "sells" same $)
    for d, a in sells:
        end_value -= _basket_value_at(a, index, d, end_date)
    return end_value + sold_sum - bought_sum - v_start


class _BasketIndex:
    """Precomputed price lookups for the policy-basket counterfactual.

    `_basket_value_at` runs once per (buy/sell lot × policy ticker) — half a
    million times over a year of history — and each call used to scan the
    ticker's entire price series twice via `_last_known_price`, which builds
    a list of every date <= target and takes `max`. That linear scan made the
    policy counterfactual quadratic and dominated `/api/v1/analytics/risk`
    (which reaches it through `compute_beta` → `compute_position_alpha`).

    Two observations collapse it:

      * each series is sorted once here, so the purchase-date lookup becomes
        an O(log n) `bisect` instead of an O(n) scan; and
      * `eval_date` is the same for every call in a run, so each ticker's
        price at that date is resolved once up front rather than re-derived
        per lot.

    Prices are as-of (last close at or before the target), identical to
    `_last_known_price` — this is a speed change, not a methodology change.
    """

    __slots__ = ("_by_date", "_closes", "_dates", "_weights")

    def __init__(
        self,
        weights: dict[str, Decimal],
        closes_per_ticker: dict[str, dict[date, Decimal]],
    ) -> None:
        self._weights = weights
        self._closes: dict[str, dict[date, Decimal]] = {}
        self._dates: dict[str, list[date]] = {}
        # target date -> {ticker: as-of price}. The callers evaluate the same
        # handful of dates over and over (every lot against every series day),
        # so memoizing by date collapses ~500k lookups to one bisect per
        # (date, ticker) actually seen.
        self._by_date: dict[date, dict[str, Decimal]] = {}
        for ticker in weights:
            series = closes_per_ticker.get(ticker) or {}
            if not series:
                continue
            self._closes[ticker] = series
            self._dates[ticker] = sorted(series)

    def prices_on(self, target: date) -> dict[str, Decimal]:
        """As-of price per policy ticker, memoized per target date.

        Omits tickers whose series has no close at or before `target` — the
        same "skip unpriceable components" behavior `_last_known_price`
        returning None produced.
        """
        cached = self._by_date.get(target)
        if cached is not None:
            return cached
        prices: dict[str, Decimal] = {}
        for ticker, ordered in self._dates.items():
            idx = bisect_right(ordered, target)
            if idx == 0:
                continue
            price_date = ordered[idx - 1]
            price = self._closes[ticker][price_date]
            if target - price_date > timedelta(days=_MAX_ASOF_PRICE_AGE_DAYS) or price <= 0:
                continue
            prices[ticker] = price
        self._by_date[target] = prices
        return prices

    def weights(self) -> dict[str, Decimal]:
        return self._weights


def _policy_benchmark_available(
    weights: dict[str, Decimal],
    closes_per_ticker: dict[str, dict[date, Decimal]],
    start_date: date,
    end_date: date,
) -> bool:
    """Whether the configured basket can price the full comparison window.

    A configured policy is not automatically a usable benchmark.  The basket
    deliberately renormalizes around individual missing components, but at
    least one positive-weight component must have a positive price at both
    endpoints.  Otherwise a zero-valued counterfactual would masquerade as a
    real zero return and overstate alpha.
    """
    if not weights:
        return False
    return any(
        weight > 0
        and _benchmark_has_positive_coverage(
            closes_per_ticker.get(ticker, {}), start_date, end_date
        )
        for ticker, weight in weights.items()
    )


def _basket_value_at(
    capital: Decimal,
    index: _BasketIndex,
    purchase_date: date,
    eval_date: date,
) -> Decimal:
    """How much would `capital` invested in the policy basket on
    `purchase_date` be worth on `eval_date`?

    Renormalizes weights against priceable tickers so total capital lands.
    """
    if capital == 0:
        return Decimal(0)
    buy_prices = index.prices_on(purchase_date)
    eval_prices = index.prices_on(eval_date)
    priceable: list[tuple[Decimal, Decimal, Decimal]] = []  # weight, p_buy, p_eval
    total_w = Decimal(0)
    for ticker, w in index.weights().items():
        p_buy = buy_prices.get(ticker)
        p_eval = eval_prices.get(ticker)
        if p_buy is None or p_buy == 0 or p_eval is None:
            continue
        priceable.append((w, p_buy, p_eval))
        total_w += w
    if not priceable or total_w == 0:
        return Decimal(0)
    value = Decimal(0)
    for w, p_buy, p_eval in priceable:
        renormed_w = w / total_w
        shares = (capital * renormed_w) / p_buy
        value += shares * p_eval
    return value


def _compute_alpha_series(
    session: Session,
    start_date: date,
    end_date: date,
    accts: frozenset[int],
    tickers: list[str],
    qty_at_start: dict[str, Decimal],
    prices_at_start: dict[str, Decimal],
    spy_closes: dict[date, Decimal],
    spy_start: Decimal,
    qqq_closes: dict[date, Decimal] | None = None,
    qqq_start: Decimal | None = None,
    policy_weights: dict[str, Decimal] | None = None,
    policy_closes_per_ticker: dict[str, dict[date, Decimal]] | None = None,
    v_start_by_ticker: dict[str, Decimal] | None = None,
    v_end_by_ticker: dict[str, Decimal] | None = None,
    ledger: _PositionEventLedger | None = None,
) -> tuple[list[PositionAlphaTimePoint], bool]:
    """Build the daily aggregate V and benchmark V series for the chart.

    Walks transactions forward, maintaining per-ticker qty (for V_portfolio)
    and per-ticker benchmark-shares accumulators (one per benchmark) using
    dollar-matched conversions at each trade's date.

    `v_start_by_ticker` / `v_end_by_ticker` are the same split-normalized
    quantity × eligible split-adjusted Close values the table reports. The
    chart endpoints are pinned to those sums, benchmark sleeves use the same
    starting value, and interior days use the same eligible price series.
    """
    if not tickers or not spy_closes:
        return ([], False)

    # Map ticker -> security_id (pick any one; positions are aggregated by ticker)
    tk_set = {t.upper() for t in tickers if t}
    sid_to_ticker: dict[int, str] = {}
    for sid, t in session.execute(
        select(Security.security_id, Security.ticker).where(func.upper(Security.ticker).in_(tk_set))
    ).all():
        if t is not None:
            sid_to_ticker[sid] = t.upper()
    if not sid_to_ticker:
        return ([], False)

    if ledger is None:
        ledger = _load_position_event_ledger(
            session,
            start_date,
            end_date,
            accts,
            exclude_broad_index=False,
        )

    # Initialize state at start_date
    qty: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    spy_shares_per_ticker: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    qqq_shares_per_ticker: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    # Policy basket value tracking: per-ticker accumulated $ committed at each
    # date, stored as (date, $_at_that_date) tuples per position. Then on each
    # eval day we ask "what's $X invested in the policy basket on date d worth
    # today?" via _basket_value_at.
    policy_lots_per_ticker: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    # Built once for the whole daily walk: the loop below re-values every lot
    # on every day, so a per-call linear price scan made this quadratic.
    policy_basket_index = (
        _BasketIndex(policy_weights, policy_closes_per_ticker)
        if policy_weights and policy_closes_per_ticker
        else None
    )
    for t in tk_set:
        q = qty_at_start.get(t, Decimal(0))
        qty[t] = q
        # Seed each benchmark sleeve from the SAME split-normalized V_start
        # the table uses, so every line on the chart anchors to the table's
        # V_start. Fall back to qty × Price.close only if the caller didn't
        # supply the precomputed map (keeps the helper independently callable).
        if v_start_by_ticker is not None:
            v_start_t = v_start_by_ticker.get(t, Decimal(0))
        else:
            p = prices_at_start.get(t)
            v_start_t = q * p if (q != 0 and p is not None) else Decimal(0)
        if v_start_t != 0:
            if spy_start > 0:
                spy_shares_per_ticker[t] = v_start_t / spy_start
            if qqq_closes and qqq_start and qqq_start > 0:
                qqq_shares_per_ticker[t] = v_start_t / qqq_start
            if policy_weights and policy_closes_per_ticker:
                policy_lots_per_ticker[t].append((start_date, v_start_t))

    # Pull historical prices for all tickers in window
    sids = list(sid_to_ticker.keys())
    px_rows = session.execute(
        select(Price.security_id, Price.date, Price.close)
        .where(Price.security_id.in_(sids))
        .where(Price.position_price_trade_eligibility_clause())
        .where(Price.date >= start_date - timedelta(days=14))
        .where(Price.date <= end_date)
    ).all()
    # Build {ticker: {date: price}} (take last price seen per ticker per date)
    prices: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for sid, d, c in px_rows:
        t = sid_to_ticker.get(sid)
        if t is not None:
            prices[t][d] = Decimal(c)

    # Group txs by date for walk-forward
    events_by_date: defaultdict[date, list[_PositionEvent]] = defaultdict(list)
    for event in ledger.events:
        if event.ticker in tk_set:
            events_by_date[event.date].append(event)

    out: list[PositionAlphaTimePoint] = []
    price_coverage_unavailable = False
    cur = start_date
    while cur <= end_date:
        # Apply transactions FIRST so V reflects end-of-day positions.
        # The corresponding cashflow uses the transaction's exact dollar
        # amount so this series shares the table's cashflow basis.
        # daily_return = (V[d] - V[d-1] - cashflow[d]) / V[d-1] correctly
        # isolates market-driven moves from trade-driven qty changes.
        today_cashflow = Decimal(0)
        for event in events_by_date.get(cur, []):
            t = event.ticker
            if event.quantity_delta != 0:
                qty[t] += event.quantity_delta
            if event.capital_flow == 0:
                continue
            today_cashflow += event.capital_flow
            tx_spy_px = _last_known_price(spy_closes, cur)
            if tx_spy_px and tx_spy_px > 0:
                spy_shares_per_ticker[t] += event.capital_flow / tx_spy_px
            if qqq_closes:
                tx_qqq_px = _last_known_price(qqq_closes, cur)
                if tx_qqq_px and tx_qqq_px > 0:
                    qqq_shares_per_ticker[t] += event.capital_flow / tx_qqq_px
            if policy_weights and policy_closes_per_ticker:
                policy_lots_per_ticker[t].append((cur, event.capital_flow))

        # V_portfolio. Pin the endpoints to the table's split-normalized
        # V_start / V_end so the chart reconciles to the headline exactly;
        # interior days use the same eligible price series.
        if cur == start_date and v_start_by_ticker is not None:
            v_port = sum(
                (v.quantize(Decimal("0.01")) for v in v_start_by_ticker.values()), Decimal(0)
            )
        elif cur == end_date and v_end_by_ticker is not None:
            v_port = sum(
                (v.quantize(Decimal("0.01")) for v in v_end_by_ticker.values()), Decimal(0)
            )
        else:
            v_port = Decimal(0)
            for t in tk_set:
                q = qty[t]
                if q == 0:
                    continue
                px = _last_known_fresh_positive_price(prices.get(t, {}), cur)
                if px is None:
                    price_coverage_unavailable = True
                    continue
                v_port += q * px

        # V_SPY counterfactual
        spy_px = _last_known_price(spy_closes, cur)
        v_spy = Decimal(0)
        if spy_px is not None and spy_px > 0:
            total_spy = sum((s for s in spy_shares_per_ticker.values()), Decimal(0))
            v_spy = total_spy * spy_px

        # V_QQQ counterfactual
        v_qqq = Decimal(0)
        if qqq_closes:
            qqq_px = _last_known_price(qqq_closes, cur)
            if qqq_px is not None and qqq_px > 0:
                total_qqq = sum((s for s in qqq_shares_per_ticker.values()), Decimal(0))
                v_qqq = total_qqq * qqq_px

        # V_POLICY counterfactual — evaluate each $ lot at today's basket value
        v_policy = Decimal(0)
        if policy_weights and policy_basket_index is not None:
            for ticker_lots in policy_lots_per_ticker.values():
                for lot_date, lot_amt in ticker_lots:
                    v_policy += _basket_value_at(
                        lot_amt,
                        policy_basket_index,
                        lot_date,
                        cur,
                    )

        out.append(
            PositionAlphaTimePoint(
                date=cur,
                portfolio_value=v_port.quantize(Decimal("0.01")),
                spy_counterfactual_value=v_spy.quantize(Decimal("0.01")),
                qqq_counterfactual_value=(
                    v_qqq.quantize(Decimal("0.01"))
                    if qqq_closes and qqq_start is not None
                    else None
                ),
                policy_counterfactual_value=(
                    v_policy.quantize(Decimal("0.01"))
                    if policy_weights and policy_basket_index is not None
                    else None
                ),
                position_cashflow=today_cashflow.quantize(Decimal("0.01")),
            )
        )

        cur += timedelta(days=1)

    return (out, price_coverage_unavailable)


# ---------------------------------------------------------------------------
# Helpers — walk-back qty per ticker
# ---------------------------------------------------------------------------


def _qty_per_ticker_at_date(
    session: Session, target_date: date, accts: frozenset[int]
) -> dict[str, Decimal]:
    """Return {ticker: total_qty} held on `target_date` across active accounts.

    Uses the most recent snapshot on-or-before target_date when one exists.
    Otherwise walks transactions backward from the EARLIEST snapshot to
    `target_date`, reversing each transaction's quantity effect.
    """
    # Try snapshot first
    snap_date = session.execute(
        select(HoldingSnapshot.snapshot_date)
        .where(HoldingSnapshot.snapshot_date <= target_date)
        .where(HoldingSnapshot.account_id.in_(accts))
        .order_by(HoldingSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()

    if snap_date is not None:
        # Use snapshot directly; walk-FORWARD if target_date > snap_date with tx
        return _qty_from_snapshot_forward(session, snap_date, target_date, accts)

    # No snapshot on-or-before; walk back from earliest snapshot
    earliest_snap = session.execute(
        select(HoldingSnapshot.snapshot_date)
        .where(HoldingSnapshot.account_id.in_(accts))
        .order_by(HoldingSnapshot.snapshot_date.asc())
        .limit(1)
    ).scalar_one_or_none()
    if earliest_snap is None:
        return {}
    return _qty_walk_back(session, earliest_snap, target_date, accts)


def _qty_from_snapshot_forward(
    session: Session, snap_date: date, target_date: date, accts: frozenset[int]
) -> dict[str, Decimal]:
    """Start from snapshot at snap_date, replay transactions forward to target_date.

    Quantities are normalized to today's split-adjusted units (the units the
    `prices` series uses) by scaling each snapshot/transaction quantity by the
    product of split ratios after its own date — so a split between snap_date
    and now doesn't doubled/halve the reconstructed quantity.
    """
    snap_rows = session.execute(
        select(HoldingSnapshot.security_id, HoldingSnapshot.quantity)
        .where(HoldingSnapshot.snapshot_date == snap_date)
        .where(HoldingSnapshot.account_id.in_(accts))
    ).all()
    forward_tx: list[InvestmentTransaction] = []
    if target_date > snap_date:
        forward_tx = list(
            session.execute(
                select(InvestmentTransaction)
                .where(InvestmentTransaction.account_id.in_(accts))
                .where(InvestmentTransaction.date > snap_date)
                .where(InvestmentTransaction.date <= target_date)
                .order_by(InvestmentTransaction.date.asc())
            )
            .scalars()
            .all()
        )
    sids = {sid for sid, _ in snap_rows if sid is not None}
    sids |= {tx.security_id for tx in forward_tx if tx.security_id is not None}
    factors = load_split_factors(session, sids)

    qty: dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    for sid, q in snap_rows:
        if sid is not None and q is not None:
            qty[sid] += Decimal(q) * factors.factor_after(sid, snap_date)
    for tx in forward_tx:
        if tx.security_id is None:
            continue
        delta = _forward_quantity_delta(tx)
        if delta is not None:
            qty[tx.security_id] += delta * factors.factor_after(tx.security_id, tx.date)
    return _resolve_tickers(session, qty)


def _qty_walk_back(
    session: Session, anchor_date: date, target_date: date, accts: frozenset[int]
) -> dict[str, Decimal]:
    """Walk backward from anchor snapshot, reversing each tx, to target_date.

    Tracks qty per (account_id, security_id) so the ACATS-in adjustment
    below can zero out the right rows without losing precision across
    accounts. Without per-account tracking, a multi-account ticker would
    see all its qty wiped when one account has an ACATS-in override.
    """
    anchor_rows = session.execute(
        select(
            HoldingSnapshot.account_id,
            HoldingSnapshot.security_id,
            HoldingSnapshot.quantity,
        )
        .where(HoldingSnapshot.snapshot_date == anchor_date)
        .where(HoldingSnapshot.account_id.in_(accts))
    ).all()
    backward_tx = list(
        session.execute(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.account_id.in_(accts))
            .where(InvestmentTransaction.date < anchor_date)
            .where(InvestmentTransaction.date > target_date)
            .order_by(InvestmentTransaction.date.desc())
        )
        .scalars()
        .all()
    )
    # Normalize to today's split-adjusted units (consistent with the adjusted
    # `prices` series): scale anchor and each reversed tx by the split product
    # after its own date, so a split between the tx/anchor and now isn't
    # mis-counted.
    sids = {sid for _, sid, _ in anchor_rows if sid is not None}
    sids |= {tx.security_id for tx in backward_tx if tx.security_id is not None}
    factors = load_split_factors(session, sids)

    qty_per_acct_sec: dict[tuple[int, int], Decimal] = defaultdict(lambda: Decimal(0))
    for acct_id, sid, q in anchor_rows:
        if acct_id is not None and sid is not None and q is not None:
            qty_per_acct_sec[(acct_id, sid)] += Decimal(q) * factors.factor_after(sid, anchor_date)
    for tx in backward_tx:
        if tx.security_id is None:
            continue
        delta = _reverse_quantity_delta(tx)
        if delta is not None:
            qty_per_acct_sec[(tx.account_id, tx.security_id)] += delta * factors.factor_after(
                tx.security_id, tx.date
            )

    # ACATS-in pre-window adjustment. For each cost-basis override with
    # `acquired_at > target_date`, the user did NOT have those shares in
    # that account at target_date — they hadn't been ACATS-transferred
    # in yet. Zero out the (account, security) qty so V_start excludes
    # them, preventing the "always held" inflation that would otherwise
    # mis-attribute the source-broker's pre-transfer gains to this
    # account's window-start position.
    acats_in_after_target = session.execute(
        select(CostBasisOverride.account_id, CostBasisOverride.security_id)
        .where(CostBasisOverride.acquired_at.is_not(None))
        .where(CostBasisOverride.acquired_at > target_date)
        .where(CostBasisOverride.account_id.in_(accts))
    ).all()
    for acct_id, sid in acats_in_after_target:
        qty_per_acct_sec[(acct_id, sid)] = Decimal(0)

    qty_by_sid: dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    for (_, sid), q in qty_per_acct_sec.items():
        qty_by_sid[sid] += q
    return _resolve_tickers(session, qty_by_sid)


def _forward_quantity_delta(tx: InvestmentTransaction) -> Decimal | None:
    return transaction_quantity_delta(tx)


def _reverse_quantity_delta(tx: InvestmentTransaction) -> Decimal | None:
    delta = _forward_quantity_delta(tx)
    return -delta if delta is not None else None


def _resolve_tickers(session: Session, qty_by_sid: dict[int, Decimal]) -> dict[str, Decimal]:
    """Aggregate by ticker across security_ids (deduplicate Plaid vs SnapTrade)."""
    if not qty_by_sid:
        return {}
    rows = session.execute(
        select(Security.security_id, Security.ticker).where(
            Security.security_id.in_(qty_by_sid.keys())
        )
    ).all()
    out: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for sid, ticker in rows:
        if ticker is None:
            continue
        out[ticker.upper()] += qty_by_sid.get(sid, Decimal(0))
    return dict(out)


# ---------------------------------------------------------------------------
# Helpers — price per ticker at a date
# ---------------------------------------------------------------------------


def _price_per_ticker_at_date(
    session: Session,
    tickers: list[str],
    target_date: date,
    *,
    require_position_basis: bool = False,
) -> dict[str, Decimal]:
    """Return forward-filled close for each ticker on `target_date` (or earlier).

    Position-derived callers set ``require_position_basis`` to enforce named
    yfinance split-adjusted provenance. The default preserves the broader raw
    fact lookup used by exit-quality, whose benchmark helper intentionally
    retains total-return behavior.
    """
    if not tickers:
        return {}
    # Map ticker -> security_ids
    normalized_tickers = [ticker.upper() for ticker in tickers]
    sid_rows = session.execute(
        select(Security.security_id, Security.ticker).where(
            func.upper(Security.ticker).in_(normalized_tickers)
        )
    ).all()
    sids_by_ticker: dict[str, list[int]] = defaultdict(list)
    for sid, t in sid_rows:
        if t is not None:
            sids_by_ticker[t.upper()].append(sid)

    # Pull prices in a +/- 14d window so we can forward-fill
    all_sids = [sid for sids in sids_by_ticker.values() for sid in sids]
    if not all_sids:
        return {}
    price_query = (
        select(Price.security_id, Price.date, Price.close)
        .where(Price.security_id.in_(all_sids))
        .where(Price.date >= target_date - timedelta(days=14))
        .where(Price.date <= target_date + timedelta(days=14))
    )
    if require_position_basis:
        price_query = price_query.where(Price.position_price_trade_eligibility_clause())
    rows = session.execute(price_query).all()
    by_sid: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for sid, d, c in rows:
        by_sid[sid][d] = Decimal(c)

    out: dict[str, Decimal] = {}
    for ticker, sids in sids_by_ticker.items():
        best: tuple[date, int, Decimal] | None = None
        for sid in sids:
            series = by_sid.get(sid, {})
            candidates = [
                d
                for d, price in series.items()
                if target_date - timedelta(days=_MAX_ASOF_PRICE_AGE_DAYS) <= d <= target_date
                and price > 0
            ]
            if candidates:
                price_date = max(candidates)
                candidate = (price_date, sid, series[price_date])
                if (
                    best is None
                    or price_date > best[0]
                    or (price_date == best[0] and sid < best[1])
                ):
                    best = candidate
        if best is not None:
            out[ticker] = best[2]
    return out


def _benchmark_closes_with_lookback(
    session: Session,
    symbol: str,
    start_date: date,
    end_date: date,
    *,
    total_return: bool = True,
) -> dict[date, Decimal]:
    """Benchmark closes plus a 14-day endpoint lookback/lookahead.

    Total-return close remains the default for services whose actual leg also
    includes income. Position alpha passes ``total_return=False`` because its
    actual invested-position leg intentionally excludes cash income and fees;
    using a non-dividend-adjusted close keeps the two price/trade legs
    economically comparable.
    """
    close_column = (
        func.coalesce(Benchmark.total_return_close, Benchmark.close)
        if total_return
        else Benchmark.close
    )
    rows = session.execute(
        select(
            Benchmark.date,
            close_column,
        )
        .where(Benchmark.symbol == symbol)
        .where(Benchmark.date >= start_date - timedelta(days=14))
        .where(Benchmark.date <= end_date + timedelta(days=14))
    ).all()
    return {d: Decimal(c) for d, c in rows}


def _last_known_price(closes: dict[date, Decimal], target: date) -> Decimal | None:
    candidates = [d for d, price in closes.items() if d <= target and price > 0]
    if not candidates:
        return None
    return closes[max(candidates)]


def _last_known_fresh_positive_price(closes: dict[date, Decimal], target: date) -> Decimal | None:
    candidates = [
        d
        for d, price in closes.items()
        if target - timedelta(days=_MAX_ASOF_PRICE_AGE_DAYS) <= d <= target and price > 0
    ]
    if not candidates:
        return None
    return closes[max(candidates)]


def _benchmark_has_positive_coverage(
    closes: dict[date, Decimal], start_date: date, end_date: date
) -> bool:
    current_date = start_date
    while current_date <= end_date:
        candidates = [d for d, price in closes.items() if d <= current_date and price > 0]
        if not candidates or current_date - max(candidates) > timedelta(
            days=_MAX_ASOF_PRICE_AGE_DAYS
        ):
            return False
        current_date += timedelta(days=1)
    return True
