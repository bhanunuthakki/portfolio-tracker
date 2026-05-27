"""Per-ticker trade analysis: winners, losers, open positions, turnover.

P&L methodology — split by position state, to match Holdings:

* **Open position (today_qty > 0):**
      P&L_$ = today_value − effective_cost

  Where `effective_cost` = per-account override-or-broker cost basis,
  merged with the same precedence Holdings uses (override beats broker;
  see `portfolio.py:_consolidate_holdings`). This is exactly the
  unrealized P&L Holdings shows. Realized P&L from prior in-position
  sells is intentionally NOT included — that's the cost of view
  consistency. The Dashboard's position-alpha card still reflects total
  windowed economic P&L vs benchmarks.

* **Closed position (today_qty = 0):**
      P&L_$ = sold_total − bought_total

  Pure realized cashflow. ACATS-in for a fully closed position is a
  data-completeness issue we can't recover (we'd need the source
  broker's cost lots); those rows are flagged via
  `_detect_cost_basis_gap`.

This is a deliberate departure from the older "lifetime cashflow"
formula (`today_value + sold − bought`), which double-counted ACATS-in
capital: `bought_total` came from `investment_transactions` only and
missed pre-history shares the user transferred in. The old formula
made Holdings and Trade Analysis disagree on identical positions
whenever an override was present. The new split keeps them in
lockstep.

Two corners worth knowing:

1. **Window-bounded activity, lifetime P&L.** Aggregates over **all-time**
   transactions so the open-position cost basis isn't paired with just
   window-internal buys. Activity counters and trade-count remain
   window-bounded, since "was I overtrading lately?" is a window-bounded
   question.

2. **Duplicate aggregator connections.** When the same brokerage is
   reachable via two aggregators (Plaid + SnapTrade for Robinhood),
   transactions get recorded twice. Filtering through
   `active_account_ids()` drops the data-disabled half cleanly.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    CostBasisOverride,
    HoldingSnapshot,
    InvestmentTransaction,
    InvestmentTransactionType,
    Security,
)
from portfolio_tracker.services import earnings_summary as earnings_summary_svc
from portfolio_tracker.services.active_items import active_account_ids
from sqlalchemy import func, and_

# Tickers below this combined buy+sell notional are dropped from the
# per-ticker breakdown. They're noise — small one-off trades that don't
# warrant a coaching call-out.
_MIN_TICKER_NOTIONAL: Decimal = Decimal(2000)

# When sold-share count exceeds bought-share count by more than this
# fraction (or today_qty exceeds total bought by it), almost certainly
# the position had ACATS-in / pre-history shares with no recorded buy.
# In that case bought_total under-counts the true cost basis and P&L
# is overstated — flag the row so the UI can mark it unreliable.
_ACATS_INFLATION_THRESHOLD: Decimal = Decimal("0.10")


class TickerTrade(BaseModel):
    ticker: str
    name: str | None
    first_buy: date | None
    last_action: date | None
    bought_total: Decimal
    sold_total: Decimal
    today_qty: Decimal
    today_value: Decimal
    # Open positions: today_value − effective_cost (unrealized, matches Holdings).
    # Closed positions: sold_total − bought_total (realized cashflow).
    pnl_dollars: Decimal
    # vs denominator implied by the methodology: effective_cost for open
    # positions, bought_total for closed positions. None when denominator is 0.
    pnl_pct: float | None
    trade_count: int               # window-bounded, not lifetime
    is_open: bool                  # today_qty > 0
    cost_basis_unreliable: bool    # ACATS-in / pre-history shares detected
    # Cross-project enrichment (None when earnings-summary unreachable):
    es_tracked: bool = False
    es_next_earnings_date: date | None = None
    es_thesis_status: str | None = None
    es_has_brief: bool = False
    es_latest_brief_iso_date: str | None = None


class TradingActivity(BaseModel):
    start_date: date
    end_date: date
    total_trades: int
    total_notional: Decimal
    average_position_value: Decimal | None
    annualized_turnover_pct: float | None
    trade_count_by_month: dict[str, int]


class TradeAnalysisResult(BaseModel):
    start_date: date
    end_date: date
    activity: TradingActivity
    tickers: list[TickerTrade]
    notes: list[str]


def analyze_trades(
    session: Session,
    start_date: date,
    end_date: date,
) -> TradeAnalysisResult:
    """Build the trade-analysis payload for [start_date, end_date].

    Per-ticker P&L is **lifetime** and splits by position state to match
    Holdings (see module docstring): open positions report unrealized
    P&L vs Holdings-style effective cost; closed positions report
    realized cashflow. Activity stats (turnover, trade count by month)
    stay window-bounded since "am I overtrading lately?" is a
    window-bounded question.

    Includes only tickers with > _MIN_TICKER_NOTIONAL lifetime notional
    traded. Ranks descending by absolute P&L so the most material trades
    show up first.
    """
    accts = active_account_ids(session)
    if not accts:
        return TradeAnalysisResult(
            start_date=start_date,
            end_date=end_date,
            activity=TradingActivity(
                start_date=start_date,
                end_date=end_date,
                total_trades=0,
                total_notional=Decimal(0),
                average_position_value=None,
                annualized_turnover_pct=None,
                trade_count_by_month={},
            ),
            tickers=[],
            notes=[
                "No active items — every linked aggregator is currently set "
                "to is_data_active=False. Mark at least one item active to "
                "see trade analysis."
            ],
        )

    today = _latest_snapshot_date(session, accts) or end_date

    # ---- per-ticker lifetime buy / sell aggregates ---------------------
    # NOTE: no start_date filter on transactions for the per-ticker pass.
    # See module docstring point (1) — pairing window-bounded buys with
    # a lifetime today_value overstates winners that were opened pre-window.
    rows = session.execute(
        select(
            Security.security_id,
            Security.ticker,
            Security.name,
            InvestmentTransaction.date,
            InvestmentTransaction.type,
            InvestmentTransaction.amount,
            InvestmentTransaction.quantity,
        )
        .join(InvestmentTransaction, InvestmentTransaction.security_id == Security.security_id)
        .where(InvestmentTransaction.account_id.in_(accts))
        .where(InvestmentTransaction.date <= end_date)
        .where(Security.is_cash_equivalent.is_(False))
        .where(Security.ticker.is_not(None))
    ).all()

    by_ticker: dict[str, dict] = defaultdict(
        lambda: {
            "name": None,
            "first_buy": None,
            "last_action": None,
            "bought": Decimal(0),
            "sold": Decimal(0),
            "bought_qty": Decimal(0),
            "sold_qty": Decimal(0),
            "trade_count": 0,
        }
    )
    for sid, ticker, name, tx_date, tx_type, amount, quantity in rows:
        if amount is None:
            continue
        bucket = by_ticker[ticker]
        if bucket["name"] is None:
            bucket["name"] = name
        magnitude = abs(Decimal(amount))
        qty_magnitude = abs(Decimal(quantity or 0))
        in_window = start_date <= tx_date <= end_date
        if tx_type == InvestmentTransactionType.BUY.value:
            bucket["bought"] += magnitude
            bucket["bought_qty"] += qty_magnitude
            if in_window:
                bucket["trade_count"] += 1
            if bucket["first_buy"] is None or tx_date < bucket["first_buy"]:
                bucket["first_buy"] = tx_date
            if bucket["last_action"] is None or tx_date > bucket["last_action"]:
                bucket["last_action"] = tx_date
        elif tx_type == InvestmentTransactionType.SELL.value:
            bucket["sold"] += magnitude
            bucket["sold_qty"] += qty_magnitude
            if in_window:
                bucket["trade_count"] += 1
            if bucket["last_action"] is None or tx_date > bucket["last_action"]:
                bucket["last_action"] = tx_date

    # ---- today's market value AND effective cost basis per ticker ------
    # Pulls the latest snapshot's market value AND the effective cost basis
    # (snapshot.cost_basis, with cost_basis_overrides winning when present).
    # Using the same merge logic Holdings uses, so P&L numbers agree on
    # currently-held positions — critical for ACATS-in shares where the
    # broker reports $0 cost.
    today_rows = session.execute(
        select(
            Security.ticker,
            HoldingSnapshot.account_id,
            HoldingSnapshot.security_id,
            HoldingSnapshot.quantity,
            HoldingSnapshot.institution_value,
            HoldingSnapshot.cost_basis,
        )
        .join(HoldingSnapshot, HoldingSnapshot.security_id == Security.security_id)
        .where(HoldingSnapshot.snapshot_date == today)
        .where(HoldingSnapshot.account_id.in_(accts))
        .where(Security.is_cash_equivalent.is_(False))
        .where(Security.ticker.is_not(None))
    ).all()
    overrides = {
        (o.account_id, o.security_id): o.total_cost_basis
        for o in session.execute(select(CostBasisOverride)).scalars()
    }
    today_qty: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    today_value: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    effective_cost: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    effective_cost_known: dict[str, bool] = defaultdict(lambda: False)
    # Ticker has at least one broker-only account whose cost basis looks
    # implausibly low vs market value (same heuristic as Holdings UNREL).
    # When set, the cost-basis-derived P&L is contaminated by that
    # account's bad number and the row should be marked unreliable.
    ticker_has_broker_unreliable: dict[str, bool] = defaultdict(lambda: False)
    for ticker, acct_id, sec_id, qty, val, cb in today_rows:
        today_qty[ticker] += Decimal(qty or 0)
        if val is not None:
            today_value[ticker] += Decimal(val)
        # Override wins (matches Holdings consolidation logic)
        override = overrides.get((acct_id, sec_id))
        effective = override if override is not None else cb
        if effective is not None:
            effective_cost[ticker] += Decimal(effective)
            effective_cost_known[ticker] = True
        if override is None and _is_account_cost_unreliable(cb, val):
            ticker_has_broker_unreliable[ticker] = True

    # ---- compose ticker rows -------------------------------------------
    tickers: list[TickerTrade] = []
    for ticker, b in by_ticker.items():
        notional = b["bought"] + b["sold"]
        if notional < _MIN_TICKER_NOTIONAL:
            continue
        mv = today_value.get(ticker, Decimal(0))
        qty_today = today_qty.get(ticker, Decimal(0))
        is_open = qty_today > 0
        share_count_gap = _detect_cost_basis_gap(
            bought_qty=b["bought_qty"],
            sold_qty=b["sold_qty"],
            today_qty=qty_today,
        )
        broker_unreliable = ticker_has_broker_unreliable.get(ticker, False)
        # P&L methodology splits by position state — see module docstring.
        #   * Open + we have cost basis on at least one contributing account
        #     → today_value − effective_cost (matches Holdings unrealized)
        #   * Open + no cost basis anywhere
        #     → cashflow fallback, mark unreliable (rare; held-but-no-cost
        #       is a data hole the user can fix with an override)
        #   * Closed → sold − bought (realized cashflow on a fully exited name)
        if is_open and effective_cost_known.get(ticker):
            eff = effective_cost.get(ticker, Decimal(0))
            pnl = mv - eff
            denom = eff
            # Holdings-style: contaminated when at least one contributing
            # account reports an implausibly low broker cost (e.g.
            # BrokerageLink $48 cost on a $12k position drags
            # effective_cost down). Matches Holdings' has_unreliable_cost_basis.
            unreliable = broker_unreliable
        elif is_open:
            pnl = mv + b["sold"] - b["bought"]
            denom = b["bought"]
            unreliable = True
        else:
            pnl = b["sold"] - b["bought"]
            denom = b["bought"]
            # Closed positions with share-count gap mean shares were
            # sold that we never saw bought — ACATS-in then exited.
            # We can't recover the source-broker cost so the realized
            # P&L is overstated.
            unreliable = share_count_gap
        pnl_pct = (
            float(pnl / denom * 100) if denom > 0 else None
        )
        tickers.append(
            TickerTrade(
                ticker=ticker,
                name=b["name"],
                first_buy=b["first_buy"],
                last_action=b["last_action"],
                bought_total=b["bought"],
                sold_total=b["sold"],
                today_qty=qty_today,
                today_value=mv,
                pnl_dollars=pnl,
                pnl_pct=pnl_pct,
                trade_count=b["trade_count"],
                is_open=is_open,
                cost_basis_unreliable=unreliable,
            )
        )
    tickers.sort(key=lambda t: -abs(t.pnl_dollars))

    # ---- earnings-summary enrichment (no-op when unavailable) ----------
    es_summary = earnings_summary_svc.summary_by_ticker([t.ticker for t in tickers])
    for t in tickers:
        es = es_summary.get(t.ticker.upper())
        if es is None:
            continue
        t.es_tracked = es.tracked
        t.es_next_earnings_date = es.next_earnings_date
        t.es_thesis_status = es.thesis_status
        t.es_has_brief = es.has_brief
        t.es_latest_brief_iso_date = es.latest_brief_iso_date

    # ---- trading activity / turnover -----------------------------------
    activity = _trading_activity(session, accts, start_date, end_date, today_value)

    notes = _build_notes(activity, tickers)
    return TradeAnalysisResult(
        start_date=start_date,
        end_date=end_date,
        activity=activity,
        tickers=tickers,
        notes=notes,
    )


# Mirror of portfolio.py's _is_cost_basis_unreliable so the trade-analysis
# row can propagate the same per-account heuristic to the ticker-level
# `cost_basis_unreliable` flag. Duplicated rather than imported to keep the
# trade-analysis service standalone for tests; both should evolve together.
_UNRELIABLE_MIN_VALUE = Decimal("1000")
_UNRELIABLE_COST_RATIO = Decimal("0.05")


def _is_account_cost_unreliable(
    cost_basis: Decimal | None, institution_value: Decimal | None
) -> bool:
    """True iff broker-reported cost basis is implausibly low vs market value
    on a non-trivial position. See portfolio._is_cost_basis_unreliable."""
    if institution_value is None or institution_value < _UNRELIABLE_MIN_VALUE:
        return False
    if cost_basis is None:
        return True
    if cost_basis <= 0:
        return True
    return (Decimal(cost_basis) / Decimal(institution_value)) < _UNRELIABLE_COST_RATIO


def _detect_cost_basis_gap(
    bought_qty: Decimal,
    sold_qty: Decimal,
    today_qty: Decimal,
) -> bool:
    """Return True if the share-count math implies missing buy transactions.

    Two flavors of the same condition:
      - sells exceed buys: shares were sold that we never saw bought
        (ACATS-in shares typically)
      - today's holding exceeds net (bought − sold): same root cause,
        showing up as currently-held shares with no acquisition record

    Either way, our `bought_total` under-counts the true cost basis and
    P&L on this ticker is overstated. The threshold tolerates small
    rounding noise (fractional reinvested-dividend shares etc.).
    """
    if bought_qty <= 0:
        # Pure transfer-in with no buys. If they sold or still hold any,
        # cost basis is missing.
        return sold_qty > 0 or today_qty > 0
    if sold_qty > bought_qty * (Decimal(1) + _ACATS_INFLATION_THRESHOLD):
        return True
    expected_today = bought_qty - sold_qty
    if today_qty > expected_today * (Decimal(1) + _ACATS_INFLATION_THRESHOLD):
        return True
    return False


def _latest_snapshot_date(
    session: Session, accts: frozenset[int]
) -> date | None:
    return session.execute(
        select(HoldingSnapshot.snapshot_date)
        .where(HoldingSnapshot.account_id.in_(accts))
        .order_by(HoldingSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def _trading_activity(
    session: Session,
    accts: frozenset[int],
    start_date: date,
    end_date: date,
    today_value: dict[str, Decimal],
) -> TradingActivity:
    rows = session.execute(
        select(
            InvestmentTransaction.date,
            InvestmentTransaction.type,
            InvestmentTransaction.amount,
        )
        .where(InvestmentTransaction.date >= start_date)
        .where(InvestmentTransaction.date <= end_date)
        .where(InvestmentTransaction.account_id.in_(accts))
        .where(
            InvestmentTransaction.type.in_(
                [
                    InvestmentTransactionType.BUY.value,
                    InvestmentTransactionType.SELL.value,
                ]
            )
        )
    ).all()

    total_trades = 0
    total_notional = Decimal(0)
    by_month: dict[str, int] = defaultdict(int)
    for tx_date, _tx_type, amount in rows:
        if amount is None:
            continue
        total_trades += 1
        total_notional += abs(Decimal(amount))
        by_month[tx_date.strftime("%Y-%m")] += 1

    avg_position = (
        sum(today_value.values(), Decimal(0)) if today_value else Decimal(0)
    )
    days = max((end_date - start_date).days, 1)
    years = Decimal(days) / Decimal(365)

    # Annualized turnover ≈ total_notional / 2 / avg_position / years.
    # Dividing by 2 because each round-trip (buy + sell) double-counts the
    # capital deployed once. Caps at 999% just to keep the UI sane.
    turnover_pct: float | None = None
    if avg_position > 0 and years > 0:
        raw = total_notional / Decimal(2) / avg_position / years * Decimal(100)
        turnover_pct = float(min(raw, Decimal(999)))

    return TradingActivity(
        start_date=start_date,
        end_date=end_date,
        total_trades=total_trades,
        total_notional=total_notional,
        average_position_value=avg_position if avg_position > 0 else None,
        annualized_turnover_pct=turnover_pct,
        trade_count_by_month=dict(sorted(by_month.items())),
    )


def _build_notes(activity: TradingActivity, tickers: list[TickerTrade]) -> list[str]:
    """Computed observations the UI surfaces below the table."""
    notes: list[str] = []
    if activity.annualized_turnover_pct is not None:
        if activity.annualized_turnover_pct > 100:
            notes.append(
                f"Annualized turnover is {activity.annualized_turnover_pct:.0f}% — "
                f"high. Fundamental investors typically run 15–40%. Each trade "
                f"costs slippage and (in taxable accounts) short-term capital gains."
            )
        elif activity.annualized_turnover_pct > 50:
            notes.append(
                f"Turnover is {activity.annualized_turnover_pct:.0f}% — moderate. "
                f"Worth checking whether each trade had a written thesis or was "
                f"reactive."
            )
    reliable = [t for t in tickers if not t.cost_basis_unreliable]
    winners = [t for t in reliable if t.pnl_dollars > 0]
    losers = [t for t in reliable if t.pnl_dollars < 0]
    unreliable = [t for t in tickers if t.cost_basis_unreliable]
    if winners and losers:
        win_total = sum((t.pnl_dollars for t in winners), Decimal(0))
        loss_total = sum((t.pnl_dollars for t in losers), Decimal(0))
        notes.append(
            f"Net unrealized (open) + realized (closed), reliable rows only: "
            f"${float(win_total + loss_total):+,.0f} "
            f"(winners ${float(win_total):+,.0f} on {len(winners)} names, "
            f"losers ${float(loss_total):+,.0f} on {len(losers)})."
        )
    if unreliable:
        names = ", ".join(t.ticker for t in unreliable[:8])
        more = f" + {len(unreliable) - 8} more" if len(unreliable) > 8 else ""
        notes.append(
            f"{len(unreliable)} ticker(s) flagged as cost-basis unreliable: "
            f"{names}{more}. Either a contributing account reports an "
            f"implausibly low broker cost (fixable with a cost-basis override "
            f"on Holdings), or the position is fully closed but sold more "
            f"shares than we have buy records for (ACATS-in then exited; "
            f"realized P&L is overstated)."
        )
    notes.append(
        "P&L methodology: open positions use today_value − effective_cost "
        "(Holdings-style unrealized, override beats broker cost). Closed "
        "positions use sold − bought (realized cashflow). Activity counts "
        "(trades, notional, turnover) are window-bounded; P&L is lifetime."
    )
    return notes
