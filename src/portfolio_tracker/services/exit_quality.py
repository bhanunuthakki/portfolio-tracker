"""Exit quality / regret: re-price sold shares to today and vs a SPY-hold.

The position-alpha engine measures OPEN positions. The sell side — was exiting
a good decision? — was invisible. For each ticker the user SOLD in the window
this re-prices those shares forward:

  * value_if_held   = sold_shares (normalized to today's split units) × today's
                      price — what the sold shares would be worth now.
  * regret_vs_hold  = value_if_held − sold_proceeds. Positive ⇒ the name is
                      worth more now than you sold it for (you left $ on the
                      table); negative ⇒ selling beat holding.
  * exit_alpha_vs_spy = (proceeds compounded in SPY since each sell) −
                      value_if_held. Positive ⇒ selling and going to SPY beat
                      holding the name (a good exit vs the market alternative);
                      negative ⇒ the name out-ran SPY after you sold it.

Dollars are split-invariant; share counts are normalized to today's
split-adjusted units (consistent with the adjusted price series).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    InvestmentTransaction,
    InvestmentTransactionType,
    Security,
)
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.position_alpha import (
    _benchmark_closes_with_lookback,  # pyright: ignore[reportPrivateUsage]
    _last_known_price,  # pyright: ignore[reportPrivateUsage]
    _price_per_ticker_at_date,  # pyright: ignore[reportPrivateUsage]
    _qty_per_ticker_at_date,  # pyright: ignore[reportPrivateUsage]
)
from portfolio_tracker.services.splits import load_split_factors

_CASH_EQUIV_TICKERS: frozenset[str] = frozenset(
    {"SGOV", "FDRXX", "SHV", "SPAXX", "CUR:USD", "VMFXX"}
)


class ExitQualityRow(BaseModel):
    ticker: str
    name: str | None
    sold_shares: Decimal  # normalized to today's split-adjusted units
    sold_proceeds: Decimal
    avg_sell_price: Decimal | None  # proceeds / sold_shares (today-share basis)
    price_now: Decimal | None
    value_if_held: Decimal
    regret_vs_hold: Decimal  # value_if_held − sold_proceeds (>0 = left on the table)
    spy_value_if_reinvested: Decimal  # proceeds grown in SPY since each sell
    exit_alpha_vs_spy: Decimal  # spy_value − value_if_held (>0 = good exit vs market)
    still_held: bool
    incomplete: bool  # missing today's price


class ExitQualityResult(BaseModel):
    start_date: date
    end_date: date
    rows: list[ExitQualityRow]
    total_sold_proceeds: Decimal
    total_value_if_held: Decimal
    total_regret_vs_hold: Decimal
    total_exit_alpha_vs_spy: Decimal


def _empty(start_date: date, end_date: date) -> ExitQualityResult:
    return ExitQualityResult(
        start_date=start_date,
        end_date=end_date,
        rows=[],
        total_sold_proceeds=Decimal(0),
        total_value_if_held=Decimal(0),
        total_regret_vs_hold=Decimal(0),
        total_exit_alpha_vs_spy=Decimal(0),
    )


def compute_exit_quality(session: Session, start_date: date, end_date: date) -> ExitQualityResult:
    accts = active_account_ids(session)
    if not accts:
        return _empty(start_date, end_date)

    sell_rows = session.execute(
        select(
            Security.ticker,
            Security.name,
            Security.security_id,
            InvestmentTransaction.date,
            InvestmentTransaction.quantity,
            InvestmentTransaction.amount,
        )
        .join(InvestmentTransaction, InvestmentTransaction.security_id == Security.security_id)
        .where(InvestmentTransaction.account_id.in_(accts))
        .where(InvestmentTransaction.date >= start_date)
        .where(InvestmentTransaction.date <= end_date)
        .where(InvestmentTransaction.type == InvestmentTransactionType.SELL.value)
        .where(Security.ticker.is_not(None))
        .where(Security.is_cash_equivalent.is_(False))
    ).all()

    # Per ticker: list of (date, shares, proceeds), plus a representative sid.
    by_ticker: defaultdict[str, list[tuple[date, Decimal, Decimal, int]]] = defaultdict(list)
    names: dict[str, str | None] = {}
    for ticker, name, sid, tx_date, quantity, amount in sell_rows:
        if ticker is None or quantity is None or amount is None:
            continue
        t_up = ticker.upper()
        if t_up in _CASH_EQUIV_TICKERS:
            continue
        by_ticker[t_up].append((tx_date, abs(Decimal(quantity)), abs(Decimal(amount)), sid))
        names.setdefault(t_up, name)
    if not by_ticker:
        return _empty(start_date, end_date)

    tickers = list(by_ticker.keys())
    price_now = _price_per_ticker_at_date(session, tickers, end_date)
    spy_closes = _benchmark_closes_with_lookback(session, "SPY", start_date, end_date)
    spy_today = _last_known_price(spy_closes, end_date)
    qty_at_end = _qty_per_ticker_at_date(session, end_date, accts)
    sids = {sid for sells in by_ticker.values() for _, _, _, sid in sells}
    factors = load_split_factors(session, sids)

    rows: list[ExitQualityRow] = []
    for t_up, sells in by_ticker.items():
        sold_shares = Decimal(0)
        sold_proceeds = Decimal(0)
        spy_value = Decimal(0)
        for tx_date, qty, proceeds, sid in sells:
            sold_shares += qty * factors.factor_after(sid, tx_date)
            sold_proceeds += proceeds
            spy_at_sell = _last_known_price(spy_closes, tx_date)
            if spy_today is not None and spy_at_sell and spy_at_sell > 0:
                spy_value += proceeds * (spy_today / spy_at_sell)

        pnow = price_now.get(t_up)
        value_if_held = sold_shares * pnow if pnow is not None else Decimal(0)
        avg_sell_price = (sold_proceeds / sold_shares) if sold_shares > 0 else None
        rows.append(
            ExitQualityRow(
                ticker=t_up,
                name=names.get(t_up),
                sold_shares=sold_shares.quantize(Decimal("0.0001")),
                sold_proceeds=sold_proceeds.quantize(Decimal("0.01")),
                avg_sell_price=(
                    avg_sell_price.quantize(Decimal("0.0001"))
                    if avg_sell_price is not None
                    else None
                ),
                price_now=pnow.quantize(Decimal("0.0001")) if pnow is not None else None,
                value_if_held=value_if_held.quantize(Decimal("0.01")),
                regret_vs_hold=(value_if_held - sold_proceeds).quantize(Decimal("0.01")),
                spy_value_if_reinvested=spy_value.quantize(Decimal("0.01")),
                exit_alpha_vs_spy=(spy_value - value_if_held).quantize(Decimal("0.01")),
                still_held=qty_at_end.get(t_up, Decimal(0)) != 0,
                incomplete=pnow is None,
            )
        )

    rows.sort(key=lambda r: r.regret_vs_hold, reverse=True)
    return ExitQualityResult(
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        total_sold_proceeds=sum((r.sold_proceeds for r in rows), Decimal(0)),
        total_value_if_held=sum((r.value_if_held for r in rows), Decimal(0)),
        total_regret_vs_hold=sum((r.regret_vs_hold for r in rows), Decimal(0)),
        total_exit_alpha_vs_spy=sum((r.exit_alpha_vs_spy for r in rows), Decimal(0)),
    )
