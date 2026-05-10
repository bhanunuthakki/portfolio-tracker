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
    PolicyWeight,
    PortfolioValueDaily,
    Price,
    Security,
)
from portfolio_tracker.schemas import PerformancePoint, PerformanceSeries
from portfolio_tracker.services.active_items import active_account_ids

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

# `cash`-typed subtypes that **change share quantities** without a real cash
# flow. SnapTrade emits these instead of TRANSFER for ACATS in/out, option
# assignment/expiration to underlying shares, and dividend reinvestments.
# The walk-back must reverse the *quantity* (treat exactly like TRANSFER —
# `rolling[sid] += -signed_quantity`) AND must NOT touch the cash adjustment
# series (these have either amount=$0 or a paired-but-not-recorded cash
# event, so applying the default cash/`-magnitude` rule produces phantom
# cash holes on past dates).
#
# Surfaced bug this fixes: a 2026-05-08 outgoing ACATS that moved 2187 NU,
# 708 BN, 272 RBRK, 28 VEEV, 23 MELI, 128 SGOV out of Robinhood Roth IRA
# was not being reversed during walk-back. Anchor positions (today) reflect
# the post-transfer state — without reversing the transfer, the walk-back
# left those shares OUT of historical rolling. Combined with the buys we
# do reverse during walk-back, the rolling quantity went **negative**,
# producing negative dollar position values that flipped sign across days
# and manifested as $30-$60k step-ups in the chart.
_SHARE_MOVING_CASH_SUBTYPES: frozenset[str] = frozenset(
    {
        "external_asset_transfer_in",
        "external_asset_transfer_out",
        "optionassignment",
        "optionexpiration",
        "rei",  # dividend reinvestment — shares acquired, no separate cash leg
    }
)

# Broad-market US-equity ETFs the user likely treats as "core / index
# allocation" rather than active stock-picking. When the user toggles the
# "Exclude broad-index holdings" view on the dashboard, the value of any
# positions in these tickers is removed from V on every date so the chart
# isolates the active stock-picking portion. Internal flows from buying /
# selling these (which transfer cash between active and index buckets) are
# also tracked so the synthetic benchmark gets a fair matched-flow series.
#
# Excludes QQQ deliberately — it's already used as a benchmark line. Add
# total-market mutual-fund tickers if the user holds them (VTSAX, FXAIX).
_BROAD_INDEX_ETF_TICKERS: frozenset[str] = frozenset(
    {"VTI", "VOO", "SPY", "IVV", "RSP"}
)


def compute_performance_series(
    session: Session,
    start_date: date,
    end_date: date,
    reserve_amount: Decimal = Decimal(0),
    exclude_index_etfs: bool = False,
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

    `reserve_amount` carves a fixed dollar amount off the top of both the
    actual portfolio's V and the synthetic-benchmark starting capital. The
    intent is "treat the first $X as untouchable emergency reserves and
    only show the investable portion's return." The same number is
    subtracted from V on every date (it's the same reserve every day) and
    from each synthetic's starting base. Cashflows are unchanged — labeling
    cash "reserves" doesn't change the fact that real-money deposits
    happened. Set to `Decimal(0)` for the standard full-portfolio view.

    `exclude_index_etfs` strips broad-market index ETFs (VTI/VOO/SPY/IVV/
    RSP — see `_BROAD_INDEX_ETF_TICKERS`) from V on every date so the chart
    isolates the active stock-picking portion. Buying/selling those ETFs
    moves cash between the "index bucket" and the "active bucket" inside
    the user's portfolio — these are *internal* flows from the active
    portion's perspective. The synthetic benchmark must see those same
    flows to be apples-to-apples, otherwise active V grows free of them
    and the comparison is rigged in active's favor. We compute internal
    flows from index-ETF BUYs (active outflow) and SELLs (active inflow)
    and add them to the cashflow series used for both Modified Dietz on
    V_active and for the synthetic benchmarks.
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

    # ---- Index-ETF carve-out -------------------------------------------
    daily_value_active = daily_value
    if exclude_index_etfs:
        index_sids = _broad_index_security_ids(session)
        if index_sids:
            daily_index_value = _daily_subset_value(
                session, start_date, end_date, index_sids
            )
            daily_value_active = {
                d: daily_value[d] - daily_index_value.get(d, Decimal(0))
                for d in daily_value
            }
            internal_flows = _daily_internal_index_cashflows(
                session, start_date, end_date, index_sids
            )
            for d, c in internal_flows.items():
                daily_cashflow[d] = daily_cashflow.get(d, Decimal(0)) + c

    # Reserve adjustment: shift V_start, V[d], and synthetic bases down by
    # `reserve_amount`. The cashflow series is left intact. Clamp to zero
    # so a too-large reserve doesn't produce a negative denominator.
    base_value = daily_value_active[sorted_dates[0]]
    end_value = daily_value_active[sorted_dates[-1]]
    reserve = max(Decimal(0), reserve_amount)
    if reserve > base_value:
        reserve = base_value
    base_value_adj = base_value - reserve
    daily_value_adj = (
        {d: v - reserve for d, v in daily_value_active.items()}
        if reserve > 0
        else daily_value_active
    )

    spy_equivalent = _money_flow_matched_value(
        sorted_dates, base_value_adj, daily_cashflow, benchmark_series.get("SPY", {})
    )
    qqq_equivalent = _money_flow_matched_value(
        sorted_dates, base_value_adj, daily_cashflow, benchmark_series.get("QQQ", {})
    )
    policy_weights = _load_policy_weights(session)
    policy_equivalent = (
        _policy_matched_value(
            sorted_dates, base_value_adj, daily_cashflow, benchmark_series, policy_weights
        )
        if policy_weights
        else {}
    )

    portfolio_returns = _modified_dietz_series(
        sorted_dates, daily_value_adj, daily_cashflow, base_value_adj
    )
    spy_returns = _modified_dietz_series(
        sorted_dates, spy_equivalent, daily_cashflow, base_value_adj
    ) if spy_equivalent else {}
    qqq_returns = _modified_dietz_series(
        sorted_dates, qqq_equivalent, daily_cashflow, base_value_adj
    ) if qqq_equivalent else {}
    policy_returns = _modified_dietz_series(
        sorted_dates, policy_equivalent, daily_cashflow, base_value_adj
    ) if policy_equivalent else {}

    points: list[PerformancePoint] = []
    for current_date in sorted_dates:
        points.append(
            PerformancePoint(
                date=current_date,
                portfolio_value=daily_value_adj[current_date],
                portfolio_return_pct=portfolio_returns[current_date],
                spy_return_pct=spy_returns.get(current_date),
                qqq_return_pct=qqq_returns.get(current_date),
                policy_return_pct=policy_returns.get(current_date),
                spy_equivalent_value=spy_equivalent.get(current_date),
                qqq_equivalent_value=qqq_equivalent.get(current_date),
                policy_equivalent_value=policy_equivalent.get(current_date),
            )
        )

    return PerformanceSeries(
        start_date=start_date,
        end_date=end_date,
        base_value=base_value_adj,
        points=points,
        earliest_observed_date=_earliest_observed_date(session, start_date, end_date),
        net_external_cashflow_in=sum(daily_cashflow.values(), Decimal(0)),
        backfill_start_unreliable=_is_start_value_unreliable(base_value, end_value),
    )


def _load_policy_weights(session: Session) -> dict[str, Decimal]:
    """Return ticker → fraction (0..1) for the user's policy mix.

    Empty result means no policy is set; the caller should skip the
    synthetic policy series. Total fraction may not exactly sum to 1.0
    if the user's weights aren't fully balanced — we keep the raw
    fractions and let the synthetic portfolio scale accordingly.
    """
    rows = session.execute(select(PolicyWeight)).scalars().all()
    return {r.ticker: Decimal(r.weight_bps) / Decimal(10000) for r in rows if r.weight_bps > 0}


def _broad_index_security_ids(session: Session) -> frozenset[int]:
    """Resolve `_BROAD_INDEX_ETF_TICKERS` to a set of `security_id`s.

    A single ticker can have multiple `security_id`s in our DB when the
    same fund was ingested via different aggregators (e.g., Plaid vs
    SnapTrade) and ended up with slight metadata differences. We want the
    union of all of them.
    """
    rows = session.execute(
        select(Security.security_id).where(
            Security.ticker.in_(_BROAD_INDEX_ETF_TICKERS)
        )
    ).scalars().all()
    return frozenset(rows)


def _daily_subset_value(
    session: Session,
    start_date: date,
    end_date: date,
    security_ids: frozenset[int],
) -> dict[date, Decimal]:
    """Daily total value of just the listed securities (no cash adjustment).

    Forward dates use `holdings_snapshots` filtered to the subset. Earlier
    dates walk transactions backward, anchored on the first snapshot,
    tracking only the subset's positions. Used to extract the
    "broad-index ETF" portion of V for the active-equity carve-out.
    """
    if not security_ids:
        return {}

    forward = _forward_subset_values(session, start_date, end_date, security_ids)
    earliest_known = min(forward.keys()) if forward else None
    if earliest_known is None or earliest_known > start_date:
        backfill_end = (
            earliest_known - timedelta(days=1)
            if earliest_known is not None
            else end_date
        )
        backfill = _backfill_subset_values(
            session, start_date, backfill_end, security_ids
        )
        for d, v in backfill.items():
            forward.setdefault(d, v)
    return forward


def _forward_subset_values(
    session: Session,
    start_date: date,
    end_date: date,
    security_ids: frozenset[int],
) -> dict[date, Decimal]:
    accts = active_account_ids(session)
    if not accts:
        return {}
    rows = session.execute(
        select(
            HoldingSnapshot.snapshot_date,
            HoldingSnapshot.institution_value,
            HoldingSnapshot.quantity,
            HoldingSnapshot.institution_price,
        )
        .where(HoldingSnapshot.snapshot_date >= start_date)
        .where(HoldingSnapshot.snapshot_date <= end_date)
        .where(HoldingSnapshot.security_id.in_(security_ids))
        .where(HoldingSnapshot.account_id.in_(accts))
    ).all()
    totals: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for snap_date, value, quantity, price in rows:
        if value is not None:
            totals[snap_date] += Decimal(value)
        elif price is not None:
            totals[snap_date] += Decimal(quantity) * Decimal(price)
    return dict(totals)


def _backfill_subset_values(
    session: Session,
    start_date: date,
    end_date: date,
    security_ids: frozenset[int],
) -> dict[date, Decimal]:
    """Walk transactions backward, tracking ONLY the subset's positions.

    No cash adjustment is applied here — the subset's V is just `qty *
    price` summed across the listed securities, which is what we want for
    "value of the index-ETF portion."
    """
    accts = active_account_ids(session)
    if not accts:
        return {}
    anchor_date = session.execute(
        select(HoldingSnapshot.snapshot_date)
        .where(HoldingSnapshot.account_id.in_(accts))
        .order_by(HoldingSnapshot.snapshot_date.asc())
        .limit(1)
    ).scalar_one_or_none()
    if anchor_date is None:
        return {}

    rows = session.execute(
        select(HoldingSnapshot.security_id, HoldingSnapshot.quantity)
        .where(HoldingSnapshot.snapshot_date == anchor_date)
        .where(HoldingSnapshot.security_id.in_(security_ids))
        .where(HoldingSnapshot.account_id.in_(accts))
    ).all()
    rolling: dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    for sid, qty in rows:
        rolling[sid] += Decimal(qty)

    backward_tx = session.execute(
        select(InvestmentTransaction)
        .where(InvestmentTransaction.security_id.in_(security_ids))
        .where(InvestmentTransaction.account_id.in_(accts))
        .where(InvestmentTransaction.date < anchor_date)
        .where(InvestmentTransaction.date >= start_date)
        .order_by(InvestmentTransaction.date.desc())
    ).scalars().all()

    daily_quantities: dict[date, dict[int, Decimal]] = {}
    cursor_date = anchor_date - timedelta(days=1)
    daily_quantities[cursor_date] = dict(rolling)
    for tx in backward_tx:
        while cursor_date > tx.date:
            cursor_date -= timedelta(days=1)
            daily_quantities[cursor_date] = dict(rolling)
        if tx.security_id is None:
            continue
        delta = _reverse_transaction_quantity(tx)
        if delta is not None:
            rolling[tx.security_id] = rolling.get(tx.security_id, Decimal(0)) + delta
    while cursor_date > start_date:
        cursor_date -= timedelta(days=1)
        daily_quantities[cursor_date] = dict(rolling)

    return _value_subset_with_prices(
        session, daily_quantities, security_ids, start_date, end_date
    )


def _value_subset_with_prices(
    session: Session,
    daily_quantities: dict[date, dict[int, Decimal]],
    security_ids: frozenset[int],
    start_date: date,
    end_date: date,
) -> dict[date, Decimal]:
    """Mark-to-market valuation for the subset. No `snapshot_price`
    fallback (the listed securities are liquid index ETFs with full price
    coverage; if a price is missing on a given day we skip rather than
    fabricate)."""
    relevant_dates = [d for d in daily_quantities if start_date <= d <= end_date]
    if not relevant_dates:
        return {}

    securities = session.execute(
        select(Security).where(Security.security_id.in_(security_ids))
    ).scalars().all()
    sec_meta: dict[int, Security] = {s.security_id: s for s in securities}

    price_rows = session.execute(
        select(Price.security_id, Price.date, Price.close)
        .where(Price.security_id.in_(security_ids))
        .where(Price.date >= start_date - timedelta(days=14))
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
                total += quantity * Decimal(1)
                continue
            close = _last_known_price(price_lookup.get(security_id, {}), current_date)
            if close is None:
                continue
            total += quantity * close
        totals[current_date] = total
    return totals


def _daily_internal_index_cashflows(
    session: Session,
    start_date: date,
    end_date: date,
    security_ids: frozenset[int],
) -> dict[date, Decimal]:
    """Internal flows from buying / selling broad-index ETFs.

    From the *active-portion* point of view, buying VTI is an OUTFLOW
    (cash leaves active to fund the index purchase) and selling VTI is
    an INFLOW (cash from the index sale lands in active). Including these
    in the cashflow series lets the synthetic benchmark match exactly the
    money the active portion actually had to work with on each day, so
    the comparison is fair.

    Sign convention follows `_reverse_transaction_cash_delta` — derive
    direction from the transaction TYPE, not from amount sign, because
    Plaid and SnapTrade disagree.
    """
    if not security_ids:
        return {}
    accts = active_account_ids(session)
    if not accts:
        return {}
    rows = session.execute(
        select(InvestmentTransaction)
        .where(InvestmentTransaction.security_id.in_(security_ids))
        .where(InvestmentTransaction.account_id.in_(accts))
        .where(InvestmentTransaction.date >= start_date)
        .where(InvestmentTransaction.date <= end_date)
    ).scalars().all()
    totals: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for tx in rows:
        if tx.amount is None:
            continue
        magnitude = abs(Decimal(tx.amount))
        # SELL of an index → cash returns to the active bucket (inflow).
        # BUY of an index → cash leaves the active bucket (outflow).
        if tx.type == InvestmentTransactionType.SELL.value:
            totals[tx.date] += magnitude
        elif tx.type == InvestmentTransactionType.BUY.value:
            totals[tx.date] -= magnitude
    return dict(totals)


def _policy_matched_value(
    sorted_dates: list[date],
    base_value: Decimal,
    daily_cashflow: dict[date, Decimal],
    benchmark_series: dict[str, dict[date, Decimal]],
    weights: dict[str, Decimal],
) -> dict[date, Decimal]:
    """Synthetic value of a multi-ticker policy portfolio over time.

    Same logic as `_money_flow_matched_value` but for a basket: every
    purchase (initial + each cashflow) is split across the policy tickers
    in their target weights. Each lot is valued at today's prices for
    every component.

    Missing-data handling: when a policy ticker has no benchmark price on
    the deployment date, we **renormalize** the remaining weights so the
    full lot still gets invested. Pre-fix behavior dropped the missing
    ticker silently and only invested the surviving fraction of capital,
    which manifested as a hard −70% drop on day 1 if (e.g.) VTI/VWO had
    no pulled benchmark data and only QQQ/SGOV did. The chart line was
    "sit at full capital but only invest 30% of it" — a meaningless
    portfolio that's not the user's policy at all.

    The renormalized version answers the right hypothetical: "what would
    your full capital have done invested in the *priceable* portion of
    your policy mix in the same proportions?" The data-quality report
    surfaces the missing-data condition separately so the user knows the
    line is approximated.
    """
    if not sorted_dates or not weights:
        return {}
    start_date = sorted_dates[0]

    # Initial lot at start_date: split base_value across priceable tickers,
    # renormalized to sum to 1.
    initial_lot = _build_lot_renormalized(
        base_value, weights, benchmark_series, start_date
    )
    if not initial_lot:
        return {}
    lots: list[dict[str, Decimal]] = [initial_lot]

    out: dict[date, Decimal] = {}
    for current_date in sorted_dates:
        cf = daily_cashflow.get(current_date, Decimal(0))
        if cf != 0:
            cf_lot = _build_lot_renormalized(
                cf, weights, benchmark_series, current_date
            )
            if cf_lot:
                lots.append(cf_lot)

        # Value all lots at today's prices for every component.
        total = Decimal(0)
        for lot in lots:
            for ticker, qty in lot.items():
                price_today = _last_known_price(
                    benchmark_series.get(ticker, {}), current_date
                )
                if price_today is None:
                    continue
                total += qty * price_today
        if total > 0:
            out[current_date] = total
    return out


def _build_lot_renormalized(
    capital: Decimal,
    weights: dict[str, Decimal],
    benchmark_series: dict[str, dict[date, Decimal]],
    on_date: date,
) -> dict[str, Decimal]:
    """Split `capital` across policy tickers that have a price on `on_date`,
    renormalizing surviving weights to sum to 1 so the full capital lands.

    Returns `{ticker: shares}`. Empty dict if no policy ticker is priceable
    on the date — caller decides whether to skip the lot or fail loudly.
    """
    priceable: list[tuple[str, Decimal, Decimal]] = []
    surviving_weight = Decimal(0)
    for ticker, weight in weights.items():
        closes = benchmark_series.get(ticker, {})
        price = _last_known_price(closes, on_date)
        if price is None or price == 0:
            continue
        priceable.append((ticker, weight, price))
        surviving_weight += weight
    if not priceable or surviving_weight == 0:
        return {}
    lot: dict[str, Decimal] = {}
    for ticker, weight, price in priceable:
        renormed_weight = weight / surviving_weight
        lot[ticker] = (capital * renormed_weight) / price
    return lot


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
    accts = active_account_ids(session)
    if not accts:
        return {}
    rows = session.execute(
        select(InvestmentTransaction.date, InvestmentTransaction.amount,
               InvestmentTransaction.type, InvestmentTransaction.subtype)
        .where(InvestmentTransaction.date >= start_date)
        .where(InvestmentTransaction.date <= end_date)
        .where(InvestmentTransaction.account_id.in_(accts))
    ).all()

    totals: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for tx_date, amount, tx_type, tx_subtype in rows:
        cashflow_in = _signed_cashflow(tx_type, tx_subtype, Decimal(amount))
        if cashflow_in == 0:
            continue
        totals[tx_date] += cashflow_in

    # Plus: net dollar value of share-side ACATS in/out events that have
    # no matching counter-event in our linked accounts. SnapTrade emits
    # `cash/external_asset_transfer_in/out` with amount=$0 (the
    # counterparty doesn't tell us a USD value), so we have to compute
    # it ourselves at the close price on the transfer date. We then
    # treat the net as an external cashflow for TWR purposes — same
    # as a deposit / withdrawal — because from our portfolio's frame of
    # reference, value entered or left without a market gain/loss.
    #
    # Matching logic: an in/out pair with same (date, ticker, abs(qty))
    # netted to zero means an internal move between two of the user's
    # linked accounts; we skip those. Anything left over is a true
    # external flow.
    transfer_flows = _share_transfer_external_cashflows(session, start_date, end_date, accts)
    for d, c in transfer_flows.items():
        totals[d] = totals.get(d, Decimal(0)) + c

    return dict(totals)


def _share_transfer_external_cashflows(
    session: Session,
    start_date: date,
    end_date: date,
    accts: frozenset[int],
) -> dict[date, Decimal]:
    """Net dollar-valued cashflow from unmatched share-side ACATS events.

    For each (date, security_id), sum signed share quantities across
    `cash/external_asset_transfer_*` events. Net != 0 means the user
    moved shares to/from an account our DB doesn't see, which from the
    portfolio's frame is an external dollar inflow (in) or outflow (out).

    We value at close price on the transfer date and contribute the
    signed dollar to the cashflow series:
      net_qty > 0 (more in than out) → inflow → cf += +value
      net_qty < 0 (more out than in) → outflow → cf += -value
    """
    rows = session.execute(
        select(
            InvestmentTransaction.date,
            InvestmentTransaction.security_id,
            InvestmentTransaction.quantity,
        )
        .where(InvestmentTransaction.date >= start_date)
        .where(InvestmentTransaction.date <= end_date)
        .where(InvestmentTransaction.account_id.in_(accts))
        .where(InvestmentTransaction.type == InvestmentTransactionType.CASH.value)
        .where(
            InvestmentTransaction.subtype.in_(
                ["external_asset_transfer_in", "external_asset_transfer_out"]
            )
        )
    ).all()
    if not rows:
        return {}

    # Net signed qty per (date, sid)
    net_by: dict[tuple[date, int], Decimal] = defaultdict(lambda: Decimal(0))
    for tx_date, sid, qty in rows:
        if sid is None:
            continue
        net_by[(tx_date, sid)] += Decimal(qty or 0)

    sids_needed = frozenset(sid for (_, sid) in net_by)
    if not sids_needed:
        return {}
    # Pull a window of prices around the transfer dates so we can
    # forward-fill if the exact date is a non-trading day.
    earliest_d = min(d for (d, _) in net_by) - timedelta(days=14)
    price_rows = session.execute(
        select(Price.security_id, Price.date, Price.close)
        .where(Price.security_id.in_(sids_needed))
        .where(Price.date >= earliest_d)
        .where(Price.date <= end_date)
    ).all()
    price_lookup: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for sid, d, c in price_rows:
        price_lookup[sid][d] = Decimal(c)

    out: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for (tx_date, sid), net_qty in net_by.items():
        if net_qty == 0:
            continue
        close = _last_known_price(price_lookup.get(sid, {}), tx_date)
        if close is None:
            # Last-resort fallback to today's institution_price. Better
            # than skipping — that would leave a phantom V step.
            sp_row = session.execute(
                select(HoldingSnapshot.institution_price)
                .where(HoldingSnapshot.security_id == sid)
                .where(HoldingSnapshot.institution_price.is_not(None))
                .order_by(HoldingSnapshot.snapshot_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if sp_row is None:
                continue
            close = Decimal(sp_row)
        out[tx_date] += net_qty * close
    return dict(out)


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
    """Daily total portfolio value across three sources, in priority order:

    1. **`holdings_snapshots`** (forward) — real broker data for the date.
       Authoritative whenever it exists. Includes the day's late-arriving
       SnapTrade pulls if the daily-refresh cron has run.
    2. **`portfolio_values_daily`** (cache) — pre-baked from earlier runs.
       Used only for dates with no forward snapshot. Avoids re-walking
       transactions for old dates that are otherwise stable.
    3. **Transaction walk-back** — reconstructs anything still missing
       before the earliest snapshot, with cash adjustment so V_start
       doesn't collapse on net deployment.

    Cache must NEVER override forward snapshots. Earlier versions of this
    function did `merged.update(cached)` which clobbered fresh snapshot
    data with stale cached values — every time the SnapTrade leg of the
    daily refresh wrote new rows for "today," the next chart load would
    show the older mid-morning V instead of the post-sync V, manifesting
    as a fake $80k drop on the chart.
    """
    forward = _forward_values_from_snapshots(session, start_date, end_date)
    cached = _cached_daily_values(session, start_date, end_date)

    # Snapshot wins on overlap; cache fills anything the snapshot missed.
    merged: dict[date, Decimal] = dict(cached)
    merged.update(forward)

    # Backfill is anchored on the earliest *real* snapshot, so figure out
    # whether we still have a gap at the start of the window.
    earliest_known = min(merged.keys()) if merged else None
    if earliest_known is None or earliest_known > start_date:
        backfill_end = (
            earliest_known - timedelta(days=1) if earliest_known is not None else end_date
        )
        backfill = _backfill_values_from_transactions(session, start_date, backfill_end)
        # Don't overwrite snapshot or cache rows we already have.
        for d, v in backfill.items():
            merged.setdefault(d, v)

    return merged


def _cached_daily_values(
    session: Session, start_date: date, end_date: date
) -> dict[date, Decimal]:
    rows = session.execute(
        select(PortfolioValueDaily.date, PortfolioValueDaily.total_value)
        .where(PortfolioValueDaily.date >= start_date)
        .where(PortfolioValueDaily.date <= end_date)
    ).all()
    return {d: Decimal(v) for d, v in rows}


def _forward_values_from_snapshots(
    session: Session, start_date: date, end_date: date
) -> dict[date, Decimal]:
    accts = active_account_ids(session)
    if not accts:
        return {}
    rows = session.execute(
        select(
            HoldingSnapshot.snapshot_date,
            HoldingSnapshot.institution_value,
            HoldingSnapshot.quantity,
            HoldingSnapshot.institution_price,
        )
        .where(HoldingSnapshot.snapshot_date >= start_date)
        .where(HoldingSnapshot.snapshot_date <= end_date)
        .where(HoldingSnapshot.account_id.in_(accts))
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

    The walk tracks two parallel state machines, both anchored at the first
    holdings snapshot:

      1. **Positions** — share quantities reconstructed by reversing each
         BUY / SELL / TRANSFER, then valued at historical prices. Cash
         equivalents (USD, money-market funds) sit in `positions` at face
         value; they're the *anchor-date* cash, not historical cash.
      2. **Cash adjustment (delta)** — a scalar per day capturing how much
         cash the user held on date *d* relative to anchor cash. Reversing
         a BUY adds the buy amount (cash was higher before the buy). Without
         this term, V_start collapses by the cash deployed during the window
         and the chart shows a fake ramp.

    Daily total = positions × historical_prices  +  cash_adjustment[d].
    """
    accts = active_account_ids(session)
    if not accts:
        return {}
    anchor_row = session.execute(
        select(HoldingSnapshot.snapshot_date)
        .where(HoldingSnapshot.account_id.in_(accts))
        .order_by(HoldingSnapshot.snapshot_date.asc())
        .limit(1)
    ).scalar_one_or_none()
    if anchor_row is None:
        return {}
    anchor_date = anchor_row

    positions = _anchor_positions(session, anchor_date)
    cash_equivalent_security_ids = frozenset(
        session.execute(
            select(Security.security_id).where(Security.is_cash_equivalent.is_(True))
        ).scalars().all()
    )
    # Walk EVERY transaction in window — pure-cash ones (no security_id)
    # are needed for the cash-adjustment series even though they don't
    # affect positions.
    backward_tx = session.execute(
        select(InvestmentTransaction)
        .where(InvestmentTransaction.account_id.in_(accts))
        .where(InvestmentTransaction.date < anchor_date)
        .where(InvestmentTransaction.date >= start_date)
        .order_by(InvestmentTransaction.date.desc())
    ).scalars().all()

    daily_quantities: dict[date, dict[int, Decimal]] = {}
    daily_cash_adj: dict[date, Decimal] = {}
    rolling_positions = dict(positions)
    rolling_cash_adj = Decimal(0)

    cursor_date = anchor_date - timedelta(days=1)
    daily_quantities[cursor_date] = dict(rolling_positions)
    daily_cash_adj[cursor_date] = rolling_cash_adj

    for tx in backward_tx:
        while cursor_date > tx.date:
            cursor_date -= timedelta(days=1)
            daily_quantities[cursor_date] = dict(rolling_positions)
            daily_cash_adj[cursor_date] = rolling_cash_adj

        if tx.security_id is not None:
            delta = _reverse_transaction_quantity(tx)
            if delta is not None:
                rolling_positions[tx.security_id] = (
                    rolling_positions.get(tx.security_id, Decimal(0)) + delta
                )

        rolling_cash_adj += _reverse_transaction_cash_delta(
            tx, cash_equivalent_security_ids
        )

        # NOTE: do NOT write daily_quantities[tx.date] here. After the
        # reversal above, `rolling_*` represents EOD (tx.date - 1), not
        # EOD tx.date. The correct EOD-tx.date state was already written
        # by the while loop above (or carried over from the prior iter on
        # the same date). Writing here would shift the chart by one day,
        # making real cashflows appear on the day AFTER they happened.

    while cursor_date > start_date:
        cursor_date -= timedelta(days=1)
        daily_quantities[cursor_date] = dict(rolling_positions)
        daily_cash_adj[cursor_date] = rolling_cash_adj

    return _value_quantities_with_prices(
        session, daily_quantities, daily_cash_adj, start_date, end_date
    )


def _anchor_positions(session: Session, anchor_date: date) -> dict[int, Decimal]:
    accts = active_account_ids(session)
    if not accts:
        return {}
    rows = session.execute(
        select(HoldingSnapshot.security_id, HoldingSnapshot.quantity)
        .where(HoldingSnapshot.snapshot_date == anchor_date)
        .where(HoldingSnapshot.account_id.in_(accts))
    ).all()
    positions: dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    for security_id, quantity in rows:
        positions[security_id] += Decimal(quantity)
    return dict(positions)


def _reverse_transaction_quantity(tx: InvestmentTransaction) -> Decimal | None:
    """Return the share-count delta needed to undo `tx` from current positions.

    Sign conventions vary across data sources:
      * Plaid signs the quantity by direction (sell = negative, buy = positive).
      * SnapTrade reports `units` as an unsigned magnitude for buy/sell, but
        SIGNED quantities for transfer-flavored events (ACATS in is
        positive, ACATS out is negative).

    We use the TRANSACTION TYPE — not the sign of `quantity` — to determine
    direction for BUY/SELL, treating `quantity` as an unsigned magnitude.
    For SnapTrade's `cash`-typed share-moving events (see
    `_SHARE_MOVING_CASH_SUBTYPES`), the quantity is already signed, so we
    just negate it as TRANSFER does:
      * BUY                    → reverse subtracts |quantity|
      * SELL                   → reverse adds |quantity|
      * TRANSFER               → reverse subtracts signed(quantity)
      * cash/external_asset_transfer_in   (qty +) → reverse subtracts +qty
      * cash/external_asset_transfer_out  (qty −) → reverse subtracts −qty (= adds)
      * cash/optionassignment             (qty +) → reverse subtracts +qty
      * cash/optionexpiration             (qty +) → reverse subtracts +qty
      * cash/rei                          (qty +) → reverse subtracts +qty

    Returns None for events with no position effect (qty=0, plain
    cash/dividend/withdrawal/contribution, fees on USD).
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
    if tx_type == InvestmentTransactionType.CASH.value:
        subtype = (tx.subtype or "").lower().strip()
        if subtype in _SHARE_MOVING_CASH_SUBTYPES:
            return -quantity
    return None


def _reverse_transaction_cash_delta(
    tx: InvestmentTransaction, cash_equivalent_security_ids: frozenset[int]
) -> Decimal:
    """Cash adjustment to apply when walking back through `tx`.

    Returns `cash_adjustment[t-1] − cash_adjustment[t]` due to this
    transaction. Positive ⇒ the user held *more* cash before the trade.

    Sign convention is **derived from the transaction TYPE, not the sign
    of `amount`**, because data sources disagree on amount signing:

    * **Plaid** signs `amount` from the cash account's perspective: BUY
      and FEE have amount > 0 (cash debited), SELL and dividend have
      amount < 0 (cash credited).
    * **SnapTrade / Fidelity** signs `amount` the opposite way: BUY has
      amount < 0, SELL has amount > 0.

    A formula that just does `cash_adj += tx.amount` works for one source
    and silently inverts for the other, producing huge fake spikes on
    days with cross-source trades (e.g., Fidelity money-market sweeps).
    Use `|amount|` and let the type decide the sign:

    | Type   | Cash on day d  | cash_adj[t-1] − cash_adj[t] |
    |--------|----------------|------------------------------|
    | BUY    | decreased by m | +m                           |
    | SELL   | increased by m | −m                           |
    | FEE    | decreased by m | +m                           |
    | CASH (internal — div/int) | increased by m | −m         |
    | TRANSFER (internal)       | varies; default 0            |

    **External flows** (deposit/withdrawal/external TRANSFER) go through
    `_signed_cashflow`, which already normalizes signs across sources.

    **Cash-equivalent FEE transactions are skipped** (some brokers emit
    paired `fee/interest` ↔ `fee/miscellaneous fee` entries on the USD
    position to represent internal margin-sweeps; they sum to zero in
    theory but leak in practice).
    """
    if tx.amount is None:
        return Decimal(0)
    amount = Decimal(tx.amount)
    magnitude = abs(amount)

    if (
        tx.type == InvestmentTransactionType.FEE.value
        and tx.security_id in cash_equivalent_security_ids
    ):
        return Decimal(0)

    if _is_external_cashflow(tx.type, tx.subtype):
        return -_signed_cashflow(tx.type, tx.subtype, amount)

    if tx.type == InvestmentTransactionType.BUY.value:
        return magnitude
    if tx.type == InvestmentTransactionType.SELL.value:
        return -magnitude
    if tx.type == InvestmentTransactionType.FEE.value:
        return magnitude
    if tx.type == InvestmentTransactionType.CASH.value:
        # Share-moving cash events (ACATS, option assignment/expiration,
        # dividend reinvestment) have either amount=$0 or a paired-but-
        # not-recorded cash event. Treat as cash-neutral so we don't
        # phantom-debit USD on past dates. The position-quantity reversal
        # is handled by `_reverse_transaction_quantity`.
        subtype = (tx.subtype or "").lower().strip()
        if subtype in _SHARE_MOVING_CASH_SUBTYPES:
            return Decimal(0)
        # Internal cash events left after the external-flow check are
        # dividends / interest / similar credits — money entered, so prior
        # cash was lower.
        return -magnitude
    # Internal TRANSFER (assignment / exercise / merger) typically has
    # amount ≈ 0 and no net cash movement; safe default.
    return Decimal(0)


def _value_quantities_with_prices(
    session: Session,
    daily_quantities: dict[date, dict[int, Decimal]],
    daily_cash_adj: dict[date, Decimal],
    start_date: date,
    end_date: date,
) -> dict[date, Decimal]:
    """Multiply reconstructed quantities by historical prices to get $ values.

    Four valuation paths per security:
      1. **Cash equivalents** (USD positions, money market funds) → qty × $1.00.
         These don't have yfinance-pulled price histories but their NAV is
         essentially fixed at $1, so face value is the right answer.
      2. **Derivatives** (options, futures) — `type == "derivative"`. Almost
         always missing from yfinance feeds. We deliberately skip the
         `snapshot_price` fallback for them: at expiration they're worth
         $0 (so today's snapshot price is $0 too), and using $0 as the
         intra-life value avoids the "step-up at trade date" artifact that
         a constant non-zero fallback would create. The premium paid /
         received is fully captured by the cash adjustment, so the
         portfolio total stays right at the boundary even if mid-life MTM
         is approximated as zero.
      3. **Securities with yfinance/stooq price history** → forward-fill the
         most recent close on or before `current_date`.
      4. **No price, not cash, not derivative** → fall back to the most
         recent `holdings_snapshots.institution_price` for that security
         as a last-resort proxy. Better than dropping the position entirely.
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

    # Extend the price query backward by ~14 days so non-trading start
    # dates (e.g., YTD = Jan 1, holiday) fall back to the most recent
    # actual close instead of the wrong snapshot_price fallback.
    price_rows = session.execute(
        select(Price.security_id, Price.date, Price.close)
        .where(Price.security_id.in_(security_ids))
        .where(Price.date >= start_date - timedelta(days=14))
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
            if close is None and not _is_derivative(sec):
                # `snapshot_price` is only safe as a fallback for things
                # whose price doesn't move much (illiquid foreign stock,
                # niche ETF). For options the snapshot is almost always $0
                # at expiry, and a constant fallback would create a fake
                # step at trade dates. Skip it for derivatives.
                close = snapshot_price.get(security_id)
            if close is None:
                continue
            total += quantity * close
        # Add the cash delta induced by trades + external flows after the
        # anchor date. Anchor cash itself is already counted via cash-equiv
        # securities in `positions`; this term is the "and how much MORE
        # cash were they sitting on before the deployment" piece.
        total += daily_cash_adj.get(current_date, Decimal(0))
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


def _is_derivative(sec: Security | None) -> bool:
    """Plaid security `type` is `'derivative'` for options & futures.

    Used to short-circuit the `snapshot_price` fallback in valuation —
    derivatives are almost always missing from yfinance, and snapshot prices
    on them are $0 (post-expiry) which would create a fake step at trade
    dates if applied uniformly across the option's life.
    """
    return sec is not None and (sec.type or "").lower() == "derivative"


def _benchmark_series(
    session: Session, start_date: date, end_date: date
) -> dict[str, dict[date, Decimal]]:
    """Pull benchmark closes covering `[start_date, end_date]`.

    Extends the query backward by 14 days so we can anchor a synthetic
    benchmark portfolio whose `start_date` falls on a non-trading day
    (e.g., YTD = Jan 1, a holiday). Without this lookback, `_last_known_price`
    has no candidates ≤ start_date in the filtered dict and returns None,
    which collapses the SPY/QQQ lines to empty.
    """
    rows = session.execute(
        select(Benchmark.symbol, Benchmark.date, Benchmark.close)
        .where(Benchmark.date >= start_date - timedelta(days=14))
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
