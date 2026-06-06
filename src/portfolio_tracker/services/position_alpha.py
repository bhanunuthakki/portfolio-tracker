"""Per-ticker dollar alpha vs SPY for a chosen window.

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

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import TypedDict

from pydantic import BaseModel
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
from portfolio_tracker.services.policy import load_policy_weights

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


class PositionAlphaRow(BaseModel):
    ticker: str
    name: str | None
    value_at_start: Decimal  # qty_start × price_start
    bought_in_window: Decimal  # sum of buy $
    sold_in_window: Decimal  # sum of sell $
    value_at_end: Decimal  # qty_end × price_end
    actual_pl: Decimal  # V_end + sold − bought − V_start
    spy_counterfactual_pl: Decimal
    qqq_counterfactual_pl: Decimal
    policy_counterfactual_pl: Decimal
    alpha: Decimal  # actual_pl − spy_counterfactual_pl (primary)
    alpha_vs_qqq: Decimal
    alpha_vs_policy: Decimal
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
    spy_counterfactual_value: Decimal
    qqq_counterfactual_value: Decimal
    policy_counterfactual_value: Decimal
    position_cashflow: Decimal = Decimal(0)


class PositionAlphaResult(BaseModel):
    start_date: date
    end_date: date
    rows: list[PositionAlphaRow]
    total_actual_pl: Decimal
    total_spy_pl: Decimal
    total_qqq_pl: Decimal
    total_policy_pl: Decimal
    total_alpha: Decimal  # vs SPY (primary)
    total_alpha_vs_qqq: Decimal
    total_alpha_vs_policy: Decimal
    series: list[PositionAlphaTimePoint] = []
    v_start: Decimal = Decimal(0)
    v_end: Decimal = Decimal(0)
    has_policy: bool = False


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
            start_date=start_date,
            end_date=end_date,
            rows=[],
            total_actual_pl=Decimal(0),
            total_spy_pl=Decimal(0),
            total_qqq_pl=Decimal(0),
            total_policy_pl=Decimal(0),
            total_alpha=Decimal(0),
            total_alpha_vs_qqq=Decimal(0),
            total_alpha_vs_policy=Decimal(0),
        )

    # 1. Quantities per ticker at start_date and end_date (walk-back if needed)
    qty_at_start = _qty_per_ticker_at_date(session, start_date, accts)
    qty_at_end = _qty_per_ticker_at_date(session, end_date, accts)

    # 2. In-window buy/sell transactions per ticker
    tx_rows = session.execute(
        select(
            Security.ticker,
            Security.name,
            Security.security_id,
            InvestmentTransaction.date,
            InvestmentTransaction.type,
            InvestmentTransaction.amount,
        )
        .join(InvestmentTransaction, InvestmentTransaction.security_id == Security.security_id)
        .where(InvestmentTransaction.account_id.in_(accts))
        .where(InvestmentTransaction.date >= start_date)
        .where(InvestmentTransaction.date <= end_date)
        .where(
            InvestmentTransaction.type.in_(
                [
                    InvestmentTransactionType.BUY.value,
                    InvestmentTransactionType.SELL.value,
                ]
            )
        )
        .where(Security.ticker.is_not(None))
        .where(Security.is_cash_equivalent.is_(False))
    ).all()

    by_ticker: defaultdict[str, _TickerAgg] = defaultdict(_new_ticker_agg)
    for ticker, name, sid, tx_date, tx_type, amount in tx_rows:
        if amount is None or ticker is None:
            continue
        t_up = ticker.upper()
        if t_up in _CASH_EQUIV_TICKERS:
            continue
        if exclude_broad_index and t_up in _BROAD_INDEX_TICKERS:
            continue
        b = by_ticker[t_up]
        if b["name"] is None:
            b["name"] = name
        b["sid"] = sid
        evt = (tx_date, abs(Decimal(amount)))
        if tx_type == InvestmentTransactionType.BUY.value:
            b["buys"].append(evt)
        else:
            b["sells"].append(evt)

    # Make sure every ticker with a non-zero start or end qty also has an entry
    for t_up, qty in qty_at_start.items():
        if t_up in _CASH_EQUIV_TICKERS:
            continue
        if exclude_broad_index and t_up in _BROAD_INDEX_TICKERS:
            continue
        if qty != 0 and t_up not in by_ticker:
            by_ticker[t_up] = {"name": None, "buys": [], "sells": [], "sid": None}
    for t_up, qty in qty_at_end.items():
        if t_up in _CASH_EQUIV_TICKERS:
            continue
        if exclude_broad_index and t_up in _BROAD_INDEX_TICKERS:
            continue
        if qty != 0 and t_up not in by_ticker:
            by_ticker[t_up] = {"name": None, "buys": [], "sells": [], "sid": None}

    # 3. Price-at-start and price-at-end per ticker
    all_tickers = list(by_ticker.keys())
    prices_start = _price_per_ticker_at_date(session, all_tickers, start_date)
    prices_end = _price_per_ticker_at_date(session, all_tickers, end_date)
    # 3b. Broker-reported V from snapshots when one exists on the exact
    # date. This is the same source Holdings + Trade Analysis + Trade
    # Timeline use, so cross-view dollar amounts agree. We only fall
    # back to `qty × Price.close` when there's no snapshot for the date
    # — i.e. for historical window-start dates the user didn't already
    # have snapshots on. yfinance close and broker close can drift up
    # to ~3% on international ADRs and thinly traded names; for
    # current-day end_date this drift was the only remaining cross-
    # view inconsistency after the cost_basis_override unification.
    snapshot_values_start = _snapshot_value_per_ticker(session, start_date, accts, all_tickers)
    snapshot_values_end = _snapshot_value_per_ticker(session, end_date, accts, all_tickers)

    # 4. Benchmark closes (SPY, QQQ, and policy basket)
    spy_closes = _benchmark_closes_with_lookback(session, "SPY", start_date, end_date)
    qqq_closes = _benchmark_closes_with_lookback(session, "QQQ", start_date, end_date)
    policy_weights = load_policy_weights(session)
    has_policy = bool(policy_weights)
    policy_closes_per_ticker: dict[str, dict[date, Decimal]] = {}
    if has_policy:
        for ticker in policy_weights:
            policy_closes_per_ticker[ticker] = _benchmark_closes_with_lookback(
                session, ticker, start_date, end_date
            )

    spy_start = _last_known_price(spy_closes, start_date)
    spy_end = _last_known_price(spy_closes, end_date)
    qqq_start = _last_known_price(qqq_closes, start_date)
    qqq_end = _last_known_price(qqq_closes, end_date)
    if spy_start is None or spy_end is None or spy_start == 0:
        return PositionAlphaResult(
            start_date=start_date,
            end_date=end_date,
            rows=[],
            total_actual_pl=Decimal(0),
            total_spy_pl=Decimal(0),
            total_qqq_pl=Decimal(0),
            total_policy_pl=Decimal(0),
            total_alpha=Decimal(0),
            total_alpha_vs_qqq=Decimal(0),
            total_alpha_vs_policy=Decimal(0),
            has_policy=has_policy,
        )

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
        # Prefer broker `institution_value` from the snapshot when one
        # exists for the exact date. Falls back to qty × Price.close
        # when there's no snapshot (e.g. historical window-start dates
        # the user hadn't been running snapshots for). Keeps Holdings,
        # Trade Timeline, Trade Analysis, and Position Alpha agreeing
        # on dollar values when they share the same observation date.
        snap_start = snapshot_values_start.get(t_up)
        snap_end = snapshot_values_end.get(t_up)
        if snap_start is not None and snap_start > 0:
            v_start = snap_start
        else:
            v_start = q_start * p_start if (q_start != 0 and p_start is not None) else Decimal(0)
        if snap_end is not None and snap_end > 0:
            v_end = snap_end
        else:
            v_end = q_end * p_end if (q_end != 0 and p_end is not None) else Decimal(0)

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
        if qqq_start is not None and qqq_end is not None and qqq_start > 0:
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
        if has_policy:
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
                actual_pl=actual_pl.quantize(Decimal("0.01")),
                spy_counterfactual_pl=spy_pl.quantize(Decimal("0.01")),
                qqq_counterfactual_pl=qqq_pl.quantize(Decimal("0.01")),
                policy_counterfactual_pl=policy_pl.quantize(Decimal("0.01")),
                alpha=alpha.quantize(Decimal("0.01")),
                alpha_vs_qqq=alpha_qqq.quantize(Decimal("0.01")),
                alpha_vs_policy=alpha_policy.quantize(Decimal("0.01")),
                incomplete=incomplete,
            )
        )
        total_actual += actual_pl
        total_spy += spy_pl
        total_qqq += qqq_pl
        total_policy += policy_pl

    rows.sort(key=lambda r: r.alpha)

    agg_v_start = sum((r.value_at_start for r in rows), Decimal(0))
    agg_v_end = sum((r.value_at_end for r in rows), Decimal(0))

    series = _compute_alpha_series(
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
    )

    return PositionAlphaResult(
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        total_actual_pl=total_actual.quantize(Decimal("0.01")),
        total_spy_pl=total_spy.quantize(Decimal("0.01")),
        total_qqq_pl=total_qqq.quantize(Decimal("0.01")),
        total_policy_pl=total_policy.quantize(Decimal("0.01")),
        total_alpha=(total_actual - total_spy).quantize(Decimal("0.01")),
        total_alpha_vs_qqq=(total_actual - total_qqq).quantize(Decimal("0.01")),
        total_alpha_vs_policy=(total_actual - total_policy).quantize(Decimal("0.01")),
        series=series,
        v_start=agg_v_start.quantize(Decimal("0.01")),
        v_end=agg_v_end.quantize(Decimal("0.01")),
        has_policy=has_policy,
    )


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
    end_value = Decimal(0)
    # V_start lot, split across policy tickers
    end_value += _basket_value_at(
        v_start,
        weights,
        closes_per_ticker,
        start_date,
        end_date,
    )
    # Each buy: add $ to basket on buy date
    for d, a in buys:
        end_value += _basket_value_at(a, weights, closes_per_ticker, d, end_date)
    # Each sell: remove $ from basket on sell date (basket "sells" same $)
    for d, a in sells:
        end_value -= _basket_value_at(a, weights, closes_per_ticker, d, end_date)
    return end_value + sold_sum - bought_sum - v_start


def _basket_value_at(
    capital: Decimal,
    weights: dict[str, Decimal],
    closes_per_ticker: dict[str, dict[date, Decimal]],
    purchase_date: date,
    eval_date: date,
) -> Decimal:
    """How much would `capital` invested in the policy basket on
    `purchase_date` be worth on `eval_date`?

    Renormalizes weights against priceable tickers so total capital lands.
    """
    if capital == 0:
        return Decimal(0)
    priceable: list[tuple[str, Decimal, Decimal, Decimal]] = []  # ticker, weight, p_buy, p_eval
    total_w = Decimal(0)
    for ticker, w in weights.items():
        series = closes_per_ticker.get(ticker, {})
        p_buy = _last_known_price(series, purchase_date)
        p_eval = _last_known_price(series, eval_date)
        if p_buy is None or p_buy == 0 or p_eval is None:
            continue
        priceable.append((ticker, w, p_buy, p_eval))
        total_w += w
    if not priceable or total_w == 0:
        return Decimal(0)
    value = Decimal(0)
    for _ticker, w, p_buy, p_eval in priceable:
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
) -> list[PositionAlphaTimePoint]:
    """Build the daily aggregate V and benchmark V series for the chart.

    Walks transactions forward, maintaining per-ticker qty (for V_portfolio)
    and per-ticker benchmark-shares accumulators (one per benchmark) using
    dollar-matched conversions at each trade's date.
    """
    if not tickers or not spy_closes:
        return []

    # Map ticker -> security_id (pick any one; positions are aggregated by ticker)
    tk_set = {t.upper() for t in tickers if t}
    sid_to_ticker: dict[int, str] = {}
    for sid, t in session.execute(
        select(Security.security_id, Security.ticker).where(Security.ticker.in_(tk_set))
    ).all():
        if t is not None:
            sid_to_ticker[sid] = t.upper()
    if not sid_to_ticker:
        return []

    # Pull all in-window transactions sorted by date
    tx_rows = (
        session.execute(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.account_id.in_(accts))
            .where(InvestmentTransaction.date >= start_date)
            .where(InvestmentTransaction.date <= end_date)
            .where(InvestmentTransaction.security_id.in_(sid_to_ticker.keys()))
            .where(
                InvestmentTransaction.type.in_(
                    [
                        InvestmentTransactionType.BUY.value,
                        InvestmentTransactionType.SELL.value,
                    ]
                )
            )
            .order_by(
                InvestmentTransaction.date.asc(),
                InvestmentTransaction.plaid_investment_transaction_id.asc(),
            )
        )
        .scalars()
        .all()
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
    for t in tk_set:
        q = qty_at_start.get(t, Decimal(0))
        qty[t] = q
        p = prices_at_start.get(t)
        if q != 0 and p is not None:
            v_start_t = q * p
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
    txs_by_date: defaultdict[date, list[InvestmentTransaction]] = defaultdict(list)
    for tx in tx_rows:
        txs_by_date[tx.date].append(tx)

    out: list[PositionAlphaTimePoint] = []
    cur = start_date
    while cur <= end_date:
        # Apply transactions FIRST so V reflects end-of-day positions.
        # The corresponding cashflow uses today's close price so that
        # daily_return = (V[d] - V[d-1] - cashflow[d]) / V[d-1] correctly
        # isolates market-driven moves from trade-driven qty changes.
        today_cashflow = Decimal(0)
        for tx in txs_by_date.get(cur, []):
            if tx.security_id is None:
                continue
            t = sid_to_ticker.get(tx.security_id)
            if t is None:
                continue
            qty_delta = _forward_quantity_delta(tx)
            if qty_delta is not None and qty_delta != 0:
                qty[t] += qty_delta
                px = _last_known_price(prices.get(t, {}), cur)
                if px is not None:
                    today_cashflow += qty_delta * px
            amt = abs(Decimal(tx.amount))
            if amt == 0:
                continue
            sign = (
                1
                if tx.type == InvestmentTransactionType.BUY.value
                else -1
                if tx.type == InvestmentTransactionType.SELL.value
                else 0
            )
            if sign == 0:
                continue
            tx_spy_px = _last_known_price(spy_closes, cur)
            if tx_spy_px and tx_spy_px > 0:
                spy_shares_per_ticker[t] += sign * amt / tx_spy_px
            if qqq_closes:
                tx_qqq_px = _last_known_price(qqq_closes, cur)
                if tx_qqq_px and tx_qqq_px > 0:
                    qqq_shares_per_ticker[t] += sign * amt / tx_qqq_px
            if policy_weights and policy_closes_per_ticker:
                policy_lots_per_ticker[t].append((cur, sign * amt))

        # V_portfolio: end-of-day positions × today's close
        v_port = Decimal(0)
        for t in tk_set:
            q = qty[t]
            if q == 0:
                continue
            px = _last_known_price(prices.get(t, {}), cur)
            if px is not None:
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
        if policy_weights and policy_closes_per_ticker:
            for ticker_lots in policy_lots_per_ticker.values():
                for lot_date, lot_amt in ticker_lots:
                    v_policy += _basket_value_at(
                        lot_amt,
                        policy_weights,
                        policy_closes_per_ticker,
                        lot_date,
                        cur,
                    )

        out.append(
            PositionAlphaTimePoint(
                date=cur,
                portfolio_value=v_port.quantize(Decimal("0.01")),
                spy_counterfactual_value=v_spy.quantize(Decimal("0.01")),
                qqq_counterfactual_value=v_qqq.quantize(Decimal("0.01")),
                policy_counterfactual_value=v_policy.quantize(Decimal("0.01")),
                position_cashflow=today_cashflow.quantize(Decimal("0.01")),
            )
        )

        cur += timedelta(days=1)

    return out


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
    """Start from snapshot at snap_date, replay transactions forward to target_date."""
    qty: dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    for sid, q in session.execute(
        select(HoldingSnapshot.security_id, HoldingSnapshot.quantity)
        .where(HoldingSnapshot.snapshot_date == snap_date)
        .where(HoldingSnapshot.account_id.in_(accts))
    ):
        if sid is not None and q is not None:
            qty[sid] += Decimal(q)
    if target_date > snap_date:
        # Replay forward (apply, not reverse)
        forward_tx = (
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
        for tx in forward_tx:
            if tx.security_id is None:
                continue
            delta = _forward_quantity_delta(tx)
            if delta is not None:
                qty[tx.security_id] += delta
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
    qty_per_acct_sec: dict[tuple[int, int], Decimal] = defaultdict(lambda: Decimal(0))
    for acct_id, sid, q in session.execute(
        select(
            HoldingSnapshot.account_id,
            HoldingSnapshot.security_id,
            HoldingSnapshot.quantity,
        )
        .where(HoldingSnapshot.snapshot_date == anchor_date)
        .where(HoldingSnapshot.account_id.in_(accts))
    ):
        if acct_id is not None and sid is not None and q is not None:
            qty_per_acct_sec[(acct_id, sid)] += Decimal(q)
    backward_tx = (
        session.execute(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.account_id.in_(accts))
            .where(InvestmentTransaction.date < anchor_date)
            .where(InvestmentTransaction.date >= target_date)
            .order_by(InvestmentTransaction.date.desc())
        )
        .scalars()
        .all()
    )
    for tx in backward_tx:
        if tx.security_id is None:
            continue
        delta = _reverse_quantity_delta(tx)
        if delta is not None:
            qty_per_acct_sec[(tx.account_id, tx.security_id)] += delta

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
    magnitude = abs(Decimal(tx.quantity))
    if magnitude == 0:
        return None
    tx_type = tx.type
    if tx_type == InvestmentTransactionType.BUY.value:
        return magnitude
    if tx_type == InvestmentTransactionType.SELL.value:
        return -magnitude
    if tx_type == InvestmentTransactionType.TRANSFER.value:
        return Decimal(tx.quantity)
    if tx_type == InvestmentTransactionType.CASH.value:
        subtype = (tx.subtype or "").lower().strip()
        if subtype in {
            "external_asset_transfer_in",
            "external_asset_transfer_out",
            "optionassignment",
            "optionexpiration",
            "rei",
        }:
            return Decimal(tx.quantity)
    return None


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


def _snapshot_value_per_ticker(
    session: Session,
    target_date: date,
    accts: frozenset[int],
    tickers: list[str],
) -> dict[str, Decimal]:
    """Return broker-reported `institution_value` summed per ticker for
    snapshots ON `target_date`. Empty dict when no snapshot exists on
    that exact date.

    Caller (Position Alpha) prefers this over qty × Price.close when
    a snapshot is available, so V_end matches what Holdings shows.
    yfinance / Stooq closes can drift 1-3 % from broker close on
    international ADRs (NVO), Korea ETFs (FLKR), and other thin names
    — falling back only when there's no snapshot for the date keeps
    historical reconstruction working for window-start dates that
    predate snapshot history.
    """
    if not tickers:
        return {}
    rows = session.execute(
        select(
            Security.ticker,
            func.sum(HoldingSnapshot.institution_value),
        )
        .join(HoldingSnapshot, HoldingSnapshot.security_id == Security.security_id)
        .where(HoldingSnapshot.snapshot_date == target_date)
        .where(HoldingSnapshot.account_id.in_(accts))
        .where(Security.ticker.in_(tickers))
        .group_by(Security.ticker)
    ).all()
    out: dict[str, Decimal] = {}
    for t, v in rows:
        if t is None or v is None:
            continue
        # Group key matches `Price` table casing — uppercase for the lookup.
        out[t.upper()] = Decimal(v)
    return out


def _price_per_ticker_at_date(
    session: Session, tickers: list[str], target_date: date
) -> dict[str, Decimal]:
    """Return forward-filled close for each ticker on `target_date` (or earlier)."""
    if not tickers:
        return {}
    # Map ticker -> security_ids
    sid_rows = session.execute(
        select(Security.security_id, Security.ticker).where(Security.ticker.in_(tickers))
    ).all()
    sids_by_ticker: dict[str, list[int]] = defaultdict(list)
    for sid, t in sid_rows:
        if t is not None:
            sids_by_ticker[t.upper()].append(sid)

    # Pull prices in a +/- 14d window so we can forward-fill
    all_sids = [sid for sids in sids_by_ticker.values() for sid in sids]
    if not all_sids:
        return {}
    rows = session.execute(
        select(Price.security_id, Price.date, Price.close)
        .where(Price.security_id.in_(all_sids))
        .where(Price.date >= target_date - timedelta(days=14))
        .where(Price.date <= target_date + timedelta(days=14))
    ).all()
    by_sid: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for sid, d, c in rows:
        by_sid[sid][d] = Decimal(c)

    out: dict[str, Decimal] = {}
    for ticker, sids in sids_by_ticker.items():
        best = None
        for sid in sids:
            series = by_sid.get(sid, {})
            candidates = [d for d in series if d <= target_date]
            if candidates:
                px = series[max(candidates)]
                if best is None or px > 0:
                    best = px
                # Keep scanning the remaining security_ids only while the best
                # so far is non-positive — a stop on the first sid would pin a
                # 0/negative close even when another aggregator's row for the
                # same ticker carries a real price.
                if px > 0:
                    break
        if best is not None:
            out[ticker] = best
    return out


def _benchmark_closes_with_lookback(
    session: Session, symbol: str, start_date: date, end_date: date
) -> dict[date, Decimal]:
    """Closes for one benchmark symbol, plus a 14-day forward/backward window
    so non-trading start/end dates fall back to the most recent close."""
    rows = session.execute(
        select(Benchmark.date, Benchmark.close)
        .where(Benchmark.symbol == symbol)
        .where(Benchmark.date >= start_date - timedelta(days=14))
        .where(Benchmark.date <= end_date + timedelta(days=14))
    ).all()
    return {d: Decimal(c) for d, c in rows}


def _last_known_price(closes: dict[date, Decimal], target: date) -> Decimal | None:
    candidates = [d for d in closes if d <= target]
    if not candidates:
        return None
    return closes[max(candidates)]
