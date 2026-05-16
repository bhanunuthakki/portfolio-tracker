"""Per-ticker trade analysis: winners, losers, open positions, turnover.

The simple-aggregation pitfall: averaging buy and sell prices over a
ticker's *full window* of activity hides multi-phase reality. A position
that was bought at $20, sold at $20 (loss-harvest), re-bought at $35,
and sold partially at $40 looks like "average buy $27.50, average sell
$30" — which obscures both the loss harvest AND the second-phase win.

Cleaner economic measure per ticker:

    P&L_$ = (current_market_value) + (cumulative cash from sells)
            − (cumulative cash to buys)

That's true total return on the name within the window: it nets the
realized cash flows against today's mark, regardless of how many phases
the position went through. It still misses option-premium income (puts
sold for cash, then assigned), but it correctly reflects the share-side
P&L.

Two important corners we've gotten wrong before:

1. **Window-bounded buys, lifetime today_value.** If we filter buys to
   the user's chart window but today_value still reflects the full open
   position, P&L is overstated for every ticker that was opened before
   the window. A position bought 3 years ago at $20 (outside window),
   still held at $40 today, with no in-window activity, looked like a
   $40-of-pure-profit-per-share winner. Fix: aggregates over **all-time**
   transactions; the activity counters and trade-count remain
   window-bounded, since the question "was I overtrading?" is a
   window-bounded one.

2. **ACATS-in / pre-history shares with no recorded buy.** SnapTrade
   sees Robinhood holdings transferred in from another broker as $0-
   cost shares without a buy transaction. So lifetime "bought" misses
   their cost basis entirely, and P&L = today_value + sells − bought
   massively overstates the win. We can't recover the true cost basis
   without ingesting 1099-Bs, but we *can* detect the condition (sold
   shares > bought shares OR today_qty > total_buys_qty) and surface a
   per-row warning so the user knows the row is unreliable.

3. **Duplicate aggregator connections.** When the same brokerage is
   reachable via two aggregators (Plaid + SnapTrade for Robinhood),
   transactions get recorded twice — both `bought` and `sold` get
   double-counted, shifting P&L unpredictably. Filtering through
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
    pnl_dollars: Decimal           # market_value + sells − buys (lifetime)
    pnl_pct: float | None          # vs total $ deployed (buys); None if buys = 0
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

    Per-ticker P&L is **lifetime** (regardless of `start_date`) because
    today's market value reflects a lifetime position; pairing window-
    bounded buys against a lifetime position is the bug source we've
    repeatedly hit. Activity stats (turnover, trade count by month) stay
    window-bounded, since "am I overtrading lately?" is the window-bounded
    question.

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

    # ---- compose ticker rows -------------------------------------------
    tickers: list[TickerTrade] = []
    for ticker, b in by_ticker.items():
        notional = b["bought"] + b["sold"]
        if notional < _MIN_TICKER_NOTIONAL:
            continue
        mv = today_value.get(ticker, Decimal(0))
        qty_today = today_qty.get(ticker, Decimal(0))
        # P&L methodology:
        #   * Currently-held positions: use Holdings-style math
        #     (today_value - effective_cost_basis). This matches the
        #     Holdings page exactly when ACATS overrides are present.
        #   * Closed positions (qty=0 today): fall back to lifetime
        #     `sold - bought` (realized cashflow).
        #   * If the position is open but we have no effective cost
        #     basis (no snapshot, no override), fall back to bought/sold
        #     math too.
        if qty_today > 0 and effective_cost_known.get(ticker):
            pnl = mv - effective_cost.get(ticker, Decimal(0))
            denom = effective_cost.get(ticker, Decimal(0))
        else:
            pnl = mv + b["sold"] - b["bought"]
            denom = b["bought"]
        pnl_pct = (
            float(pnl / denom * 100) if denom > 0 else None
        )
        # The cost_basis_unreliable flag previously fired any time
        # today_qty exceeded bought_qty (the classic ACATS-in detector:
        # shares appeared without buy transactions). Once an override has
        # filled in the cost basis — manual entry, or inferred_acats from
        # the inference script — the row is reliable again. Suppress the
        # warning in that case so the UI stops nagging on rows we've fixed.
        has_basis_now = qty_today > 0 and effective_cost_known.get(ticker)
        unreliable = (
            False
            if has_basis_now
            else _detect_cost_basis_gap(
                bought_qty=b["bought_qty"],
                sold_qty=b["sold_qty"],
                today_qty=today_qty.get(ticker, Decimal(0)),
            )
        )
        tickers.append(
            TickerTrade(
                ticker=ticker,
                name=b["name"],
                first_buy=b["first_buy"],
                last_action=b["last_action"],
                bought_total=b["bought"],
                sold_total=b["sold"],
                today_qty=today_qty.get(ticker, Decimal(0)),
                today_value=mv,
                pnl_dollars=pnl,
                pnl_pct=pnl_pct,
                trade_count=b["trade_count"],
                is_open=today_qty.get(ticker, Decimal(0)) > 0,
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
            f"Net realized + unrealized (reliable rows only): "
            f"${float(win_total + loss_total):+,.0f} "
            f"(winners ${float(win_total):+,.0f} on {len(winners)} names, "
            f"losers ${float(loss_total):+,.0f} on {len(losers)})."
        )
    if unreliable:
        names = ", ".join(t.ticker for t in unreliable[:8])
        more = f" + {len(unreliable) - 8} more" if len(unreliable) > 8 else ""
        notes.append(
            f"{len(unreliable)} ticker(s) flagged as cost-basis unreliable "
            f"(likely ACATS-in / pre-history shares with no recorded buy): "
            f"{names}{more}. Their P&L on this card is missing those shares' "
            f"original cost basis — treat the dollar values as upper bounds."
        )
    notes.append(
        "P&L = today's market value + lifetime sell proceeds − lifetime buy "
        "cost. Activity counts (trades, notional, turnover) are window-bounded; "
        "P&L is lifetime so today's open position is paired with the right "
        "cost basis instead of just window-internal buys."
    )
    return notes
