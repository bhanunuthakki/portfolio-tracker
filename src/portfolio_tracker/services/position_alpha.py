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

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Benchmark,
    HoldingSnapshot,
    InvestmentTransaction,
    InvestmentTransactionType,
    Price,
    Security,
)
from portfolio_tracker.services.active_items import active_account_ids


# Skip these from the per-ticker view — they're cash-equivalent vehicles
# whose "alpha vs SPY" doesn't carry meaning (they're effectively cash).
_CASH_EQUIV_TICKERS: frozenset[str] = frozenset(
    {"SGOV", "FDRXX", "SHV", "SPAXX", "CUR:USD", "VMFXX"}
)


class PositionAlphaRow(BaseModel):
    ticker: str
    name: str | None
    value_at_start: Decimal       # qty_start × price_start
    bought_in_window: Decimal     # sum of buy $
    sold_in_window: Decimal       # sum of sell $
    value_at_end: Decimal         # qty_end × price_end
    actual_pl: Decimal            # V_end + sold − bought − V_start
    spy_counterfactual_pl: Decimal
    alpha: Decimal                # actual_pl − spy_counterfactual_pl
    # Diagnostic — if walk-back couldn't determine qty_start (no price data,
    # no transactions), this fires and the row is approximate.
    incomplete: bool


class PositionAlphaTimePoint(BaseModel):
    """One day on the dashboard chart.

    `portfolio_value` and `spy_counterfactual_value` are the dollar trajectories
    starting at V_start (the value at window_start of all positions traded in
    window). The CHART plots `portfolio_value` and `spy_counterfactual_value`
    as two lines, with the gap = `alpha`.
    """
    date: date
    portfolio_value: Decimal           # sum of ticker_qty[d] × price[d]
    spy_counterfactual_value: Decimal  # sum of per-ticker SPY counterfactual
    alpha: Decimal                     # portfolio_value − spy_counterfactual_value


class PositionAlphaResult(BaseModel):
    start_date: date
    end_date: date
    rows: list[PositionAlphaRow]
    total_actual_pl: Decimal
    total_spy_pl: Decimal
    total_alpha: Decimal
    series: list[PositionAlphaTimePoint] = []
    v_start: Decimal = Decimal(0)
    v_end: Decimal = Decimal(0)


def compute_position_alpha(
    session: Session,
    start_date: date,
    end_date: date,
) -> PositionAlphaResult:
    """Build the per-ticker windowed alpha breakdown for [start_date, end_date]."""
    accts = active_account_ids(session)
    if not accts:
        return PositionAlphaResult(
            start_date=start_date, end_date=end_date, rows=[],
            total_actual_pl=Decimal(0), total_spy_pl=Decimal(0),
            total_alpha=Decimal(0),
        )

    # 1. Quantities per ticker at start_date and end_date (walk-back if needed)
    qty_at_start = _qty_per_ticker_at_date(session, start_date, accts)
    qty_at_end = _qty_per_ticker_at_date(session, end_date, accts)

    # 2. In-window buy/sell transactions per ticker
    tx_rows = session.execute(
        select(
            Security.ticker, Security.name, Security.security_id,
            InvestmentTransaction.date,
            InvestmentTransaction.type,
            InvestmentTransaction.amount,
        )
        .join(InvestmentTransaction, InvestmentTransaction.security_id == Security.security_id)
        .where(InvestmentTransaction.account_id.in_(accts))
        .where(InvestmentTransaction.date >= start_date)
        .where(InvestmentTransaction.date <= end_date)
        .where(InvestmentTransaction.type.in_([
            InvestmentTransactionType.BUY.value,
            InvestmentTransactionType.SELL.value,
        ]))
        .where(Security.ticker.is_not(None))
        .where(Security.is_cash_equivalent.is_(False))
    ).all()

    by_ticker: dict[str, dict] = defaultdict(
        lambda: {"name": None, "buys": [], "sells": [], "sid": None}
    )
    for ticker, name, sid, tx_date, tx_type, amount in tx_rows:
        if amount is None or ticker is None:
            continue
        t_up = ticker.upper()
        if t_up in _CASH_EQUIV_TICKERS:
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
        if qty != 0 and t_up not in by_ticker:
            by_ticker[t_up] = {"name": None, "buys": [], "sells": [], "sid": None}
    for t_up, qty in qty_at_end.items():
        if t_up in _CASH_EQUIV_TICKERS:
            continue
        if qty != 0 and t_up not in by_ticker:
            by_ticker[t_up] = {"name": None, "buys": [], "sells": [], "sid": None}

    # 3. Price-at-start and price-at-end per ticker
    all_tickers = list(by_ticker.keys())
    prices_start = _price_per_ticker_at_date(session, all_tickers, start_date)
    prices_end = _price_per_ticker_at_date(session, all_tickers, end_date)

    # 4. SPY closes
    spy_closes = _spy_closes_with_lookback(session, start_date, end_date)
    spy_start = _last_known_price(spy_closes, start_date)
    spy_end = _last_known_price(spy_closes, end_date)
    if spy_start is None or spy_end is None or spy_start == 0:
        # No SPY anchor — return empty result
        return PositionAlphaResult(
            start_date=start_date, end_date=end_date, rows=[],
            total_actual_pl=Decimal(0), total_spy_pl=Decimal(0),
            total_alpha=Decimal(0),
        )

    # 5. Compute per-ticker alpha
    rows: list[PositionAlphaRow] = []
    total_actual = Decimal(0)
    total_spy = Decimal(0)

    for t_up, b in by_ticker.items():
        q_start = qty_at_start.get(t_up, Decimal(0))
        q_end = qty_at_end.get(t_up, Decimal(0))
        p_start = prices_start.get(t_up)
        p_end = prices_end.get(t_up)
        v_start = q_start * p_start if (q_start != 0 and p_start is not None) else Decimal(0)
        v_end = q_end * p_end if (q_end != 0 and p_end is not None) else Decimal(0)

        bought_sum = sum((a for _, a in b["buys"]), Decimal(0))
        sold_sum = sum((a for _, a in b["sells"]), Decimal(0))

        # Skip rows with nothing happening and no holding either side
        if v_start == 0 and v_end == 0 and bought_sum == 0 and sold_sum == 0:
            continue

        actual_pl = v_end + sold_sum - bought_sum - v_start

        # SPY counterfactual
        spy_shares = (v_start / Decimal(str(spy_start))) if v_start != 0 else Decimal(0)
        for d, a in b["buys"]:
            px = _last_known_price(spy_closes, d)
            if px and px > 0:
                spy_shares += a / Decimal(str(px))
        for d, a in b["sells"]:
            px = _last_known_price(spy_closes, d)
            if px and px > 0:
                spy_shares -= a / Decimal(str(px))
        spy_end_value = spy_shares * Decimal(str(spy_end))
        spy_pl = spy_end_value + sold_sum - bought_sum - v_start
        alpha = actual_pl - spy_pl

        incomplete = (q_start != 0 and p_start is None) or (q_end != 0 and p_end is None)

        rows.append(PositionAlphaRow(
            ticker=t_up,
            name=b["name"],
            value_at_start=v_start.quantize(Decimal("0.01")),
            bought_in_window=bought_sum.quantize(Decimal("0.01")),
            sold_in_window=sold_sum.quantize(Decimal("0.01")),
            value_at_end=v_end.quantize(Decimal("0.01")),
            actual_pl=actual_pl.quantize(Decimal("0.01")),
            spy_counterfactual_pl=spy_pl.quantize(Decimal("0.01")),
            alpha=alpha.quantize(Decimal("0.01")),
            incomplete=incomplete,
        ))
        total_actual += actual_pl
        total_spy += spy_pl

    rows.sort(key=lambda r: r.alpha)

    # Aggregate V_start and V_end (sum of position values, NOT including cash)
    agg_v_start = sum((r.value_at_start for r in rows), Decimal(0))
    agg_v_end = sum((r.value_at_end for r in rows), Decimal(0))

    # Build the time series for the dashboard chart
    series = _compute_alpha_series(
        session, start_date, end_date, accts,
        list(by_ticker.keys()), qty_at_start, prices_start, spy_closes,
        Decimal(str(spy_start)),
    )

    return PositionAlphaResult(
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        total_actual_pl=total_actual.quantize(Decimal("0.01")),
        total_spy_pl=total_spy.quantize(Decimal("0.01")),
        total_alpha=(total_actual - total_spy).quantize(Decimal("0.01")),
        series=series,
        v_start=agg_v_start.quantize(Decimal("0.01")),
        v_end=agg_v_end.quantize(Decimal("0.01")),
    )


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
) -> list[PositionAlphaTimePoint]:
    """Build the daily aggregate V and V_SPY series for the chart.

    Walks transactions forward from start_date, applying each one to both
    the per-ticker qty (for V_portfolio) and the per-ticker SPY-shares
    accumulator (for V_SPY_counterfactual). For each day in [start, end],
    emits the aggregate V values.
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
    tx_rows = session.execute(
        select(InvestmentTransaction)
        .where(InvestmentTransaction.account_id.in_(accts))
        .where(InvestmentTransaction.date >= start_date)
        .where(InvestmentTransaction.date <= end_date)
        .where(InvestmentTransaction.security_id.in_(sid_to_ticker.keys()))
        .where(InvestmentTransaction.type.in_([
            InvestmentTransactionType.BUY.value,
            InvestmentTransactionType.SELL.value,
        ]))
        .order_by(InvestmentTransaction.date.asc(),
                  InvestmentTransaction.plaid_investment_transaction_id.asc())
    ).scalars().all()

    # Initialize state at start_date
    qty: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    spy_shares_per_ticker: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for t in tk_set:
        q = qty_at_start.get(t, Decimal(0))
        qty[t] = q
        p = prices_at_start.get(t)
        if q != 0 and p is not None and spy_start > 0:
            spy_shares_per_ticker[t] = (q * p) / spy_start

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
    txs_by_date: dict[date, list] = defaultdict(list)
    for tx in tx_rows:
        txs_by_date[tx.date].append(tx)

    out: list[PositionAlphaTimePoint] = []
    cur = start_date
    while cur <= end_date:
        # Snapshot the V values BEFORE applying today's transactions
        # (this matches the convention that end-of-prior-day positions ÷ today's close)
        v_port = Decimal(0)
        for t in tk_set:
            q = qty[t]
            if q == 0:
                continue
            px = _last_known_price(prices.get(t, {}), cur)
            if px is not None:
                v_port += q * px

        spy_px = _last_known_price(spy_closes, cur)
        v_spy = Decimal(0)
        if spy_px is not None and spy_px > 0:
            total_spy_shares = sum((s for s in spy_shares_per_ticker.values()), Decimal(0))
            v_spy = total_spy_shares * Decimal(str(spy_px))

        out.append(PositionAlphaTimePoint(
            date=cur,
            portfolio_value=v_port.quantize(Decimal("0.01")),
            spy_counterfactual_value=v_spy.quantize(Decimal("0.01")),
            alpha=(v_port - v_spy).quantize(Decimal("0.01")),
        ))

        # Apply transactions on this date
        for tx in txs_by_date.get(cur, []):
            t = sid_to_ticker.get(tx.security_id)
            if t is None:
                continue
            qty_delta = _forward_quantity_delta(tx)
            if qty_delta is not None:
                qty[t] += qty_delta
            # SPY counterfactual: dollar-match the trade
            if tx.amount is not None:
                amt = abs(Decimal(tx.amount))
                tx_spy_px = _last_known_price(spy_closes, cur)
                if tx_spy_px and tx_spy_px > 0 and amt > 0:
                    if tx.type == InvestmentTransactionType.BUY.value:
                        spy_shares_per_ticker[t] += amt / Decimal(str(tx_spy_px))
                    elif tx.type == InvestmentTransactionType.SELL.value:
                        spy_shares_per_ticker[t] -= amt / Decimal(str(tx_spy_px))

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
        forward_tx = session.execute(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.account_id.in_(accts))
            .where(InvestmentTransaction.date > snap_date)
            .where(InvestmentTransaction.date <= target_date)
            .order_by(InvestmentTransaction.date.asc())
        ).scalars().all()
        for tx in forward_tx:
            if tx.security_id is None or tx.quantity is None:
                continue
            delta = _forward_quantity_delta(tx)
            if delta is not None:
                qty[tx.security_id] += delta
    return _resolve_tickers(session, qty)


def _qty_walk_back(
    session: Session, anchor_date: date, target_date: date, accts: frozenset[int]
) -> dict[str, Decimal]:
    """Walk backward from anchor snapshot, reversing each tx, to target_date."""
    qty: dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    for sid, q in session.execute(
        select(HoldingSnapshot.security_id, HoldingSnapshot.quantity)
        .where(HoldingSnapshot.snapshot_date == anchor_date)
        .where(HoldingSnapshot.account_id.in_(accts))
    ):
        if sid is not None and q is not None:
            qty[sid] += Decimal(q)
    backward_tx = session.execute(
        select(InvestmentTransaction)
        .where(InvestmentTransaction.account_id.in_(accts))
        .where(InvestmentTransaction.date < anchor_date)
        .where(InvestmentTransaction.date >= target_date)
        .order_by(InvestmentTransaction.date.desc())
    ).scalars().all()
    for tx in backward_tx:
        if tx.security_id is None or tx.quantity is None:
            continue
        delta = _reverse_quantity_delta(tx)
        if delta is not None:
            qty[tx.security_id] += delta
    return _resolve_tickers(session, qty)


def _forward_quantity_delta(tx: InvestmentTransaction) -> Decimal | None:
    if tx.quantity is None:
        return None
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
        if subtype in {"external_asset_transfer_in", "external_asset_transfer_out",
                       "optionassignment", "optionexpiration", "rei"}:
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
        select(Security.security_id, Security.ticker)
        .where(Security.security_id.in_(qty_by_sid.keys()))
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
    session: Session, tickers: list[str], target_date: date
) -> dict[str, Decimal]:
    """Return forward-filled close for each ticker on `target_date` (or earlier)."""
    if not tickers:
        return {}
    # Map ticker -> security_ids
    sid_rows = session.execute(
        select(Security.security_id, Security.ticker)
        .where(Security.ticker.in_(tickers))
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
                    break
        if best is not None:
            out[ticker] = best
    return out


def _spy_closes_with_lookback(
    session: Session, start_date: date, end_date: date
) -> dict[date, Decimal]:
    rows = session.execute(
        select(Benchmark.date, Benchmark.close)
        .where(Benchmark.symbol == "SPY")
        .where(Benchmark.date >= start_date - timedelta(days=14))
        .where(Benchmark.date <= end_date + timedelta(days=14))
    ).all()
    return {d: Decimal(c) for d, c in rows}


def _last_known_price(closes: dict[date, Decimal], target: date) -> Decimal | None:
    candidates = [d for d in closes if d <= target]
    if not candidates:
        return None
    return closes[max(candidates)]
