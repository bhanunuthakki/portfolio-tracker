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
P&L. We surface that caveat to the UI.

Returns one row per ticker plus an aggregate "trading activity" summary:
total transactions, total notional, annualized turnover. The UI uses
this as the data backing for the Trade Analysis section on the
dashboard — "what worked, what didn't, am I overtrading."
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    HoldingSnapshot,
    InvestmentTransaction,
    InvestmentTransactionType,
    Security,
)

# Tickers below this combined buy+sell notional are dropped from the
# per-ticker breakdown. They're noise — small one-off trades that don't
# warrant a coaching call-out.
_MIN_TICKER_NOTIONAL: Decimal = Decimal(2000)


class TickerTrade(BaseModel):
    ticker: str
    name: str | None
    first_buy: date | None
    last_action: date | None
    bought_total: Decimal
    sold_total: Decimal
    today_qty: Decimal
    today_value: Decimal
    pnl_dollars: Decimal           # market_value + sells − buys
    pnl_pct: float | None          # vs total $ deployed (buys); None if buys = 0
    trade_count: int
    is_open: bool                  # today_qty > 0


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

    Includes only tickers with > _MIN_TICKER_NOTIONAL notional traded.
    Ranks descending by absolute P&L magnitude so the most material
    trades show up first.
    """
    today = _latest_snapshot_date(session) or end_date

    # ---- per-ticker buy / sell aggregates ------------------------------
    rows = session.execute(
        select(
            Security.security_id,
            Security.ticker,
            Security.name,
            InvestmentTransaction.date,
            InvestmentTransaction.type,
            InvestmentTransaction.amount,
        )
        .join(InvestmentTransaction, InvestmentTransaction.security_id == Security.security_id)
        .where(InvestmentTransaction.date >= start_date)
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
            "trade_count": 0,
        }
    )
    for sid, ticker, name, tx_date, tx_type, amount in rows:
        if amount is None:
            continue
        bucket = by_ticker[ticker]
        if bucket["name"] is None:
            bucket["name"] = name
        magnitude = abs(Decimal(amount))
        if tx_type == InvestmentTransactionType.BUY.value:
            bucket["bought"] += magnitude
            bucket["trade_count"] += 1
            if bucket["first_buy"] is None or tx_date < bucket["first_buy"]:
                bucket["first_buy"] = tx_date
            if bucket["last_action"] is None or tx_date > bucket["last_action"]:
                bucket["last_action"] = tx_date
        elif tx_type == InvestmentTransactionType.SELL.value:
            bucket["sold"] += magnitude
            bucket["trade_count"] += 1
            if bucket["last_action"] is None or tx_date > bucket["last_action"]:
                bucket["last_action"] = tx_date

    # ---- today's market value per ticker -------------------------------
    today_rows = session.execute(
        select(
            Security.ticker,
            HoldingSnapshot.quantity,
            HoldingSnapshot.institution_value,
        )
        .join(HoldingSnapshot, HoldingSnapshot.security_id == Security.security_id)
        .where(HoldingSnapshot.snapshot_date == today)
        .where(Security.is_cash_equivalent.is_(False))
        .where(Security.ticker.is_not(None))
    ).all()
    today_qty: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    today_value: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for ticker, qty, val in today_rows:
        today_qty[ticker] += Decimal(qty or 0)
        if val is not None:
            today_value[ticker] += Decimal(val)

    # ---- compose ticker rows -------------------------------------------
    tickers: list[TickerTrade] = []
    for ticker, b in by_ticker.items():
        notional = b["bought"] + b["sold"]
        if notional < _MIN_TICKER_NOTIONAL:
            continue
        mv = today_value.get(ticker, Decimal(0))
        pnl = mv + b["sold"] - b["bought"]
        pnl_pct = (
            float(pnl / b["bought"] * 100)
            if b["bought"] > 0
            else None
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
            )
        )
    tickers.sort(key=lambda t: -abs(t.pnl_dollars))

    # ---- trading activity / turnover -----------------------------------
    activity = _trading_activity(session, start_date, end_date, today_value)

    notes = _build_notes(activity, tickers)
    return TradeAnalysisResult(
        start_date=start_date,
        end_date=end_date,
        activity=activity,
        tickers=tickers,
        notes=notes,
    )


def _latest_snapshot_date(session: Session) -> date | None:
    return session.execute(
        select(HoldingSnapshot.snapshot_date)
        .order_by(HoldingSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def _trading_activity(
    session: Session,
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
    winners = [t for t in tickers if t.pnl_dollars > 0]
    losers = [t for t in tickers if t.pnl_dollars < 0]
    if winners and losers:
        win_total = sum((t.pnl_dollars for t in winners), Decimal(0))
        loss_total = sum((t.pnl_dollars for t in losers), Decimal(0))
        notes.append(
            f"Net realized + unrealized: ${float(win_total + loss_total):+,.0f} "
            f"(winners ${float(win_total):+,.0f} on {len(winners)} names, "
            f"losers ${float(loss_total):+,.0f} on {len(losers)})."
        )
    notes.append(
        "P&L = today's market value + cumulative sell proceeds − cumulative buy "
        "cost. Excludes option-premium income (puts sold and assigned aren't "
        "fully captured). For closed positions, equals realized cash gain."
    )
    return notes
