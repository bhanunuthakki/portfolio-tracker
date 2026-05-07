"""Portfolio performance vs. benchmarks (time-weighted return).

Builds a daily time series of `(portfolio_index, spy_index, qqq_index)` over
a chosen window, all rebased to 100 at `start_date`. The portfolio series is
a **time-weighted return** (TWR) — the standard metric for benchmarking
because it neutralizes contributions / withdrawals.

Two-stage construction:

  1. **Daily portfolio value** is sourced from `holdings_snapshots` going
     forward; for dates predating the first snapshot, it's reconstructed by
     walking `investment_transactions` backward and valuing each position
     against `prices` (yfinance backfill).

  2. **Daily TWR** chains per-day returns:
        r_d = (V_d − V_{d−1} − cashflow_d) / V_{d−1}
        TWR_index_d = TWR_index_{d−1} × (1 + r_d)
     where `cashflow_d` is the sum of *external* money movements on day `d`
     — deposits, withdrawals, ACATS transfers. Trades, dividends, fees, and
     interest are internal events: they affect V but not the cashflow term.

The raw `portfolio_value` is also returned per-point so the UI can show the
underlying dollar series alongside the rebased index if it wants.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Benchmark,
    HoldingSnapshot,
    InvestmentTransaction,
    InvestmentTransactionType,
    Price,
    Security,
)
from portfolio_tracker.schemas import PerformancePoint, PerformanceSeries

# Diagnostics-only threshold: daily portfolio-value swings beyond this are
# almost certainly reconstruction artifacts (unobserved transfers, gifted
# stock, etc.) rather than real market moves. Surfaced via the data-quality
# report so the user can investigate the underlying transactions.
_ABNORMAL_DAILY_RETURN: Decimal = Decimal("0.30")

# `transfer` is Plaid's catch-all for asset movements. EXTERNAL transfers
# (ACATS in/out, ACH deposits, wires) are TWR cashflow. INTERNAL transfers
# (option assignments, account-to-account moves within the same login) are
# NOT — they're position rearrangements that don't change the user's basis.
# Plaid signals the difference via `subtype`.
_INTERNAL_TRANSFER_SUBTYPES: frozenset[str] = frozenset(
    {
        "assignment",       # option exercise / assignment
        "exercise",
        "merger",
        "spin off",
        "split",
        "stock distribution",
    }
)

# `cash` subtypes that represent EXTERNAL flows INTO the portfolio.
# Direction is determined by the subtype NAME, not Plaid's `amount` sign —
# different brokers report contribution/deposit signs inconsistently
# (some from the cash account's perspective, some from the investor's).
_INFLOW_CASH_SUBTYPES: frozenset[str] = frozenset(
    {
        "deposit",
        "contribution",
        "rollover",
        "wire",
        "ach",
    }
)

# `cash` subtypes that represent EXTERNAL flows OUT of the portfolio.
_OUTFLOW_CASH_SUBTYPES: frozenset[str] = frozenset(
    {
        "withdrawal",
    }
)

# `cash` subtype that's ambiguous in direction — fall back to Plaid's sign.
_AMBIGUOUS_CASH_SUBTYPES: frozenset[str] = frozenset(
    {
        "transfer",
    }
)


def compute_performance_series(
    session: Session, start_date: date, end_date: date
) -> PerformanceSeries:
    """Build a money-flow-matched return series for [start_date, end_date].

    For the actual portfolio AND for synthetic SPY / QQQ "what if I'd just
    bought the index with the same money flows" portfolios, we compute a
    Modified Dietz return at every observation date:

        return_pct(d) = (V(d) - V_start - cumulative_C(d))
                        / (V_start + sum_{i: d_i<=d} C_i * (d-d_i)/(d-d_0))

    Both portfolio and benchmark series share the SAME denominator on each
    day, so even when V_start is unreliable (transaction-walk reconstruction
    misses cash), the GAP between the lines is the true relative performance.
    """
    daily_value = _daily_portfolio_value(session, start_date, end_date)
    if not daily_value:
        return PerformanceSeries(
            start_date=start_date,
            end_date=end_date,
            base_value=Decimal(0),
            points=[],
        )

    daily_cashflow = _daily_external_cashflows(session, start_date, end_date)
    benchmark_series = _benchmark_series(session, start_date, end_date)

    sorted_dates = sorted(daily_value.keys())
    base_value = daily_value[sorted_dates[0]]
    end_value = daily_value[sorted_dates[-1]]

    spy_equivalent = _money_flow_matched_value(
        sorted_dates, base_value, daily_cashflow, benchmark_series.get("SPY", {})
    )
    qqq_equivalent = _money_flow_matched_value(
        sorted_dates, base_value, daily_cashflow, benchmark_series.get("QQQ", {})
    )

    portfolio_returns = _modified_dietz_series(
        sorted_dates, daily_value, daily_cashflow, base_value
    )
    spy_returns = _modified_dietz_series(
        sorted_dates, spy_equivalent, daily_cashflow, base_value
    ) if spy_equivalent else {}
    qqq_returns = _modified_dietz_series(
        sorted_dates, qqq_equivalent, daily_cashflow, base_value
    ) if qqq_equivalent else {}

    points: list[PerformancePoint] = []
    for current_date in sorted_dates:
        points.append(
            PerformancePoint(
                date=current_date,
                portfolio_value=daily_value[current_date],
                portfolio_return_pct=portfolio_returns[current_date],
                spy_return_pct=spy_returns.get(current_date),
                qqq_return_pct=qqq_returns.get(current_date),
                spy_equivalent_value=spy_equivalent.get(current_date),
                qqq_equivalent_value=qqq_equivalent.get(current_date),
            )
        )

    return PerformanceSeries(
        start_date=start_date,
        end_date=end_date,
        base_value=base_value,
        points=points,
        earliest_observed_date=_earliest_observed_date(session, start_date, end_date),
        net_external_cashflow_in=sum(daily_cashflow.values(), Decimal(0)),
        backfill_start_unreliable=_is_start_value_unreliable(base_value, end_value),
    )


def _money_flow_matched_value(
    sorted_dates: list[date],
    base_value: Decimal,
    daily_cashflow: dict[date, Decimal],
    benchmark_closes: dict[date, Decimal],
) -> dict[date, Decimal]:
    """Build a synthetic value series: V_start invested in the benchmark at
    `sorted_dates[0]`, plus each daily cashflow `cf` invested in the
    benchmark at that day's close, all valued at the benchmark's close on
    each day in `sorted_dates`.

    Returns an empty dict when the benchmark series doesn't cover the start
    (we can't anchor the synthetic portfolio without a base price).
    """
    if not sorted_dates:
        return {}
    start_date = sorted_dates[0]
    base_price = _last_known_price(benchmark_closes, start_date)
    if base_price is None or base_price == 0:
        return {}

    # Each "lot" in the synthetic portfolio: a quantity of benchmark shares
    # purchased at the lot's date. Today's value = sum(qty * price_today).
    initial_shares = base_value / base_price
    lots: list[tuple[date, Decimal]] = [(start_date, initial_shares)]

    out: dict[date, Decimal] = {}
    for current_date in sorted_dates:
        cf = daily_cashflow.get(current_date, Decimal(0))
        if cf != 0:
            cf_price = _last_known_price(benchmark_closes, current_date)
            if cf_price is not None and cf_price != 0:
                lots.append((current_date, cf / cf_price))
        price_today = _last_known_price(benchmark_closes, current_date)
        if price_today is None:
            continue
        total_shares = sum((qty for _, qty in lots), Decimal(0))
        out[current_date] = total_shares * price_today
    return out


def _modified_dietz_series(
    sorted_dates: list[date],
    daily_value: dict[date, Decimal],
    daily_cashflow: dict[date, Decimal],
    base_value: Decimal,
) -> dict[date, Decimal]:
    """Cumulative Modified Dietz return % at each date.

    Standard formula:
        R = (V_end - V_start - C_total) / (V_start + sum(C_i * w_i))
    where w_i = (period_end - cashflow_date) / period_length. We compute it
    cumulatively: at each `current_date`, treat that as the period end.

    Returns Decimal 0 (not %) on the start date. End-of-day percentages
    expressed as the multiplier × 100.
    """
    if not sorted_dates:
        return {}
    out: dict[date, Decimal] = {sorted_dates[0]: Decimal(0)}
    start_date = sorted_dates[0]

    sorted_cashflows = sorted(
        ((d, c) for d, c in daily_cashflow.items() if c != 0 and d > start_date),
        key=lambda x: x[0],
    )

    for current_date in sorted_dates[1:]:
        period_days = (current_date - start_date).days
        if period_days <= 0:
            out[current_date] = Decimal(0)
            continue
        v_now = daily_value.get(current_date)
        if v_now is None:
            continue

        cumulative_cf = Decimal(0)
        weighted_cf = Decimal(0)
        for cf_date, cf_amount in sorted_cashflows:
            if cf_date > current_date:
                break
            cumulative_cf += cf_amount
            weight = Decimal((current_date - cf_date).days) / Decimal(period_days)
            weighted_cf += cf_amount * weight

        denominator = base_value + weighted_cf
        if denominator <= 0:
            out[current_date] = Decimal(0)
            continue
        numerator = v_now - base_value - cumulative_cf
        out[current_date] = (numerator / denominator) * Decimal(100)
    return out


def _earliest_observed_date(
    session: Session, start_date: date, end_date: date
) -> date | None:
    """First date in the window with an actual `holdings_snapshots` row.

    Earlier values (if any) come from transaction-walk reconstruction, which
    is what we warn about. Returns None if no forward snapshot lives in the
    window — the entire chart is then modeled.
    """
    earliest = session.execute(
        select(func.min(HoldingSnapshot.snapshot_date))
        .where(HoldingSnapshot.snapshot_date >= start_date)
        .where(HoldingSnapshot.snapshot_date <= end_date)
    ).scalar_one_or_none()
    return earliest


def _is_start_value_unreliable(base_value: Decimal, end_value: Decimal) -> bool:
    """Heuristic: backfilled start is suspect when far below the end value.

    The backfill reconstructs positions but not cash, so positions bought
    during the window with pre-existing cash get netted out — collapsing
    the reconstructed start. When the start is < 25% of end, the resulting
    TWR is dominated by reconstruction noise.
    """
    if base_value <= 0 or end_value <= 0:
        return True
    return (base_value / end_value) < Decimal("0.25")


def _daily_external_cashflows(
    session: Session, start_date: date, end_date: date
) -> dict[date, Decimal]:
    """Sum signed external cashflows per day (positive = INTO portfolio).

    Direction handling:
      * For unambiguously-named `cash` subtypes (`contribution`, `deposit`,
        `withdrawal`), the NAME determines direction and we use abs(amount).
        Brokers report the `amount` sign inconsistently for these — some
        from the cash account's perspective, some from the investor's.
      * For ambiguous subtypes (`cash/transfer` and the bare `transfer`
        type), we trust Plaid's standard sign convention: negative amount
        = cash going INTO the account = inflow, so cashflow_in = -amount.
    """
    rows = session.execute(
        select(InvestmentTransaction.date, InvestmentTransaction.amount,
               InvestmentTransaction.type, InvestmentTransaction.subtype)
        .where(InvestmentTransaction.date >= start_date)
        .where(InvestmentTransaction.date <= end_date)
    ).all()

    totals: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for tx_date, amount, tx_type, tx_subtype in rows:
        cashflow_in = _signed_cashflow(tx_type, tx_subtype, Decimal(amount))
        if cashflow_in == 0:
            continue
        totals[tx_date] += cashflow_in
    return dict(totals)


def _signed_cashflow(tx_type: str, tx_subtype: str | None, amount: Decimal) -> Decimal:
    """Return the signed cashflow INTO the portfolio for one transaction.

    Returns Decimal(0) for internal events (trades, dividends, fees, etc.).
    Positive return = money entered the portfolio. Negative = money left.
    """
    subtype_norm = (tx_subtype or "").lower().strip()

    if tx_type == InvestmentTransactionType.TRANSFER.value:
        if subtype_norm in _INTERNAL_TRANSFER_SUBTYPES:
            return Decimal(0)
        return -amount  # Plaid sign convention

    if tx_type == InvestmentTransactionType.CASH.value:
        if subtype_norm in _INFLOW_CASH_SUBTYPES:
            return abs(amount)
        if subtype_norm in _OUTFLOW_CASH_SUBTYPES:
            return -abs(amount)
        if subtype_norm in _AMBIGUOUS_CASH_SUBTYPES:
            return -amount

    return Decimal(0)


def _is_external_cashflow(tx_type: str, tx_subtype: str | None) -> bool:
    """Boolean version of `_signed_cashflow` used by the audit endpoint."""
    subtype_norm = (tx_subtype or "").lower().strip()
    if tx_type == InvestmentTransactionType.TRANSFER.value:
        return subtype_norm not in _INTERNAL_TRANSFER_SUBTYPES
    if tx_type == InvestmentTransactionType.CASH.value:
        return (
            subtype_norm in _INFLOW_CASH_SUBTYPES
            or subtype_norm in _OUTFLOW_CASH_SUBTYPES
            or subtype_norm in _AMBIGUOUS_CASH_SUBTYPES
        )
    return False


def _daily_portfolio_value(
    session: Session, start_date: date, end_date: date
) -> dict[date, Decimal]:
    """Daily total portfolio value, preferring snapshots and falling back to backfill."""
    forward = _forward_values_from_snapshots(session, start_date, end_date)
    earliest_snapshot = min(forward.keys()) if forward else None

    if earliest_snapshot is None or earliest_snapshot > start_date:
        backfill_end = (
            earliest_snapshot - timedelta(days=1) if earliest_snapshot is not None else end_date
        )
        backfill = _backfill_values_from_transactions(session, start_date, backfill_end)
        forward.update(backfill)

    return forward


def _forward_values_from_snapshots(
    session: Session, start_date: date, end_date: date
) -> dict[date, Decimal]:
    rows = session.execute(
        select(
            HoldingSnapshot.snapshot_date,
            HoldingSnapshot.institution_value,
            HoldingSnapshot.quantity,
            HoldingSnapshot.institution_price,
        )
        .where(HoldingSnapshot.snapshot_date >= start_date)
        .where(HoldingSnapshot.snapshot_date <= end_date)
    ).all()

    totals: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for snap_date, value, quantity, price in rows:
        if value is not None:
            totals[snap_date] += Decimal(value)
        elif price is not None:
            totals[snap_date] += Decimal(quantity) * Decimal(price)
    return dict(totals)


def _backfill_values_from_transactions(
    session: Session, start_date: date, end_date: date
) -> dict[date, Decimal]:
    """Reconstruct daily portfolio values walking transactions backward.

    Strategy:
      * Take the earliest snapshot date as the anchor; positions on that date
        are the truth.
      * For each transaction strictly before the anchor (in date-descending
        order), reverse its effect on quantities.
      * For every trading day in [start_date, end_date], multiply each
        security's quantity by its `prices.close` and sum.
    """
    anchor_row = session.execute(
        select(HoldingSnapshot.snapshot_date)
        .order_by(HoldingSnapshot.snapshot_date.asc())
        .limit(1)
    ).scalar_one_or_none()
    if anchor_row is None:
        return {}
    anchor_date = anchor_row

    positions = _anchor_positions(session, anchor_date)
    backward_tx = session.execute(
        select(InvestmentTransaction)
        .where(InvestmentTransaction.security_id.is_not(None))
        .where(InvestmentTransaction.date < anchor_date)
        .where(InvestmentTransaction.date >= start_date)
        .order_by(InvestmentTransaction.date.desc())
    ).scalars().all()

    daily_quantities: dict[date, dict[int, Decimal]] = {}
    rolling = dict(positions)
    daily_quantities[anchor_date - timedelta(days=1)] = dict(rolling)
    cursor_date = anchor_date - timedelta(days=1)
    for tx in backward_tx:
        while cursor_date > tx.date:
            cursor_date -= timedelta(days=1)
            daily_quantities[cursor_date] = dict(rolling)
        if tx.security_id is None:
            continue
        delta = _reverse_transaction_quantity(tx)
        if delta is None:
            continue
        rolling[tx.security_id] = rolling.get(tx.security_id, Decimal(0)) + delta
        daily_quantities[tx.date] = dict(rolling)

    while cursor_date > start_date:
        cursor_date -= timedelta(days=1)
        daily_quantities[cursor_date] = dict(rolling)

    return _value_quantities_with_prices(session, daily_quantities, start_date, end_date)


def _anchor_positions(session: Session, anchor_date: date) -> dict[int, Decimal]:
    rows = session.execute(
        select(HoldingSnapshot.security_id, HoldingSnapshot.quantity)
        .where(HoldingSnapshot.snapshot_date == anchor_date)
    ).all()
    positions: dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    for security_id, quantity in rows:
        positions[security_id] += Decimal(quantity)
    return dict(positions)


def _reverse_transaction_quantity(tx: InvestmentTransaction) -> Decimal | None:
    """Return the share-count delta needed to undo `tx` from current positions.

    Sign conventions vary across data sources:
      * Plaid signs the quantity by direction (sell = negative, buy = positive).
      * SnapTrade reports `units` as an unsigned magnitude regardless of type.

    We use the TRANSACTION TYPE — not the sign of `quantity` — to determine
    direction, treating `quantity` as an unsigned magnitude:
      * BUY      → user gained shares; reverse subtracts |quantity|
      * SELL     → user lost shares; reverse adds |quantity|
      * TRANSFER → direction varies per broker; trust the signed value
                   and negate (transfer-in qty + → reverse subtracts)

    Returns None for cash / fee / dividend transactions (qty=0 or no
    position effect).
    """
    tx_type = tx.type
    quantity = Decimal(tx.quantity)
    magnitude = abs(quantity)
    if magnitude == 0:
        return None
    if tx_type == InvestmentTransactionType.BUY.value:
        return -magnitude
    if tx_type == InvestmentTransactionType.SELL.value:
        return magnitude
    if tx_type == InvestmentTransactionType.TRANSFER.value:
        return -quantity
    return None


def _value_quantities_with_prices(
    session: Session,
    daily_quantities: dict[date, dict[int, Decimal]],
    start_date: date,
    end_date: date,
) -> dict[date, Decimal]:
    """Multiply reconstructed quantities by historical prices to get $ values.

    Three valuation paths per security:
      1. **Cash equivalents** (USD positions, money market funds) → qty × $1.00.
         These don't have yfinance-pulled price histories but their NAV is
         essentially fixed at $1, so face value is the right answer.
      2. **Securities with yfinance/stooq price history** → forward-fill the
         most recent close on or before `current_date`.
      3. **No price, not cash** → fall back to the most recent
         `holdings_snapshots.institution_price` for that security as a
         last-resort proxy. Better than dropping the position entirely.
    """
    relevant_dates = [d for d in daily_quantities if start_date <= d <= end_date]
    if not relevant_dates:
        return {}
    security_ids = {sid for snap in daily_quantities.values() for sid in snap}
    if not security_ids:
        return {}

    securities = session.execute(
        select(Security).where(Security.security_id.in_(security_ids))
    ).scalars().all()
    sec_meta: dict[int, Security] = {s.security_id: s for s in securities}

    # Snapshot-derived fallback price per security: most recent
    # institution_price we ever observed.
    fallback_rows = session.execute(
        select(HoldingSnapshot.security_id, HoldingSnapshot.institution_price)
        .where(HoldingSnapshot.security_id.in_(security_ids))
        .where(HoldingSnapshot.institution_price.is_not(None))
        .order_by(HoldingSnapshot.snapshot_date.desc())
    ).all()
    snapshot_price: dict[int, Decimal] = {}
    for sid, price in fallback_rows:
        if sid not in snapshot_price and price is not None:
            snapshot_price[sid] = Decimal(price)

    price_rows = session.execute(
        select(Price.security_id, Price.date, Price.close)
        .where(Price.security_id.in_(security_ids))
        .where(Price.date >= start_date)
        .where(Price.date <= end_date)
    ).all()
    price_lookup: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for security_id, price_date, close in price_rows:
        price_lookup[security_id][price_date] = Decimal(close)

    totals: dict[date, Decimal] = {}
    for current_date in sorted(relevant_dates):
        snap = daily_quantities[current_date]
        total = Decimal(0)
        for security_id, quantity in snap.items():
            sec = sec_meta.get(security_id)
            if sec is not None and sec.is_cash_equivalent:
                # Cash equivalents (USD, money market funds): face value.
                total += quantity * Decimal(1)
                continue
            close = _last_known_price(price_lookup.get(security_id, {}), current_date)
            if close is None:
                close = snapshot_price.get(security_id)
            if close is None:
                continue
            total += quantity * close
        if total > 0:
            totals[current_date] = total
    return totals


def _last_known_price(
    series: dict[date, Decimal], target_date: date
) -> Decimal | None:
    """Forward-fill: most recent price on or before `target_date`."""
    candidates = [d for d in series if d <= target_date]
    if not candidates:
        return None
    return series[max(candidates)]


def _benchmark_series(
    session: Session, start_date: date, end_date: date
) -> dict[str, dict[date, Decimal]]:
    rows = session.execute(
        select(Benchmark.symbol, Benchmark.date, Benchmark.close)
        .where(Benchmark.date >= start_date)
        .where(Benchmark.date <= end_date)
    ).all()
    out: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for symbol, bench_date, close in rows:
        out[symbol][bench_date] = Decimal(close)
    return dict(out)


def _first_available(series: dict[date, Decimal], target_date: date) -> Decimal | None:
    candidates = [d for d in series if d >= target_date]
    if not candidates:
        return None
    return series[min(candidates)]


def _index_value(
    series: dict[date, Decimal], target_date: date, base: Decimal | None
) -> Decimal | None:
    if base is None or base == 0:
        return None
    last = _last_known_price(series, target_date)
    if last is None:
        return None
    return (last / base) * _NORMALIZATION_BASE
