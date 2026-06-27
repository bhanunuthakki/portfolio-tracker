"""Fetch corporate stock-split events via yfinance into `stock_splits`.

The `prices` series is split-adjusted (back-adjusted), so the walk-back must
normalize as-traded transaction/snapshot quantities to today's split-adjusted
units (see `services/splits.py`). This job records each held security's split
history so that normalization has data to work with.

Run manually:
    python -m portfolio_tracker.jobs.splits
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import typer
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import Security, StockSplit, TickerOverride


def run() -> int:
    rows_written = 0
    with SessionLocal() as session:
        override_map = {
            ov.security_id: ov.ticker
            for ov in session.execute(select(TickerOverride)).scalars().all()
        }
        securities = (
            session.execute(
                select(Security).where(Security.is_cash_equivalent == False)  # noqa: E712
            )
            .scalars()
            .all()
        )
        for security in securities:
            ticker = override_map.get(security.security_id) or security.ticker
            if ticker is None:
                continue
            rows_written += _fetch_one(session, security.security_id, ticker)
        session.commit()
    return rows_written


def _fetch_one(session: Session, security_id: int, ticker: str) -> int:
    try:
        splits = yf.Ticker(ticker).splits
        if splits.empty:
            return 0
        items = list(cast(Any, splits.items()))
    except Exception:  # yfinance raises a variety of exceptions / odd shapes
        return 0
    written = 0
    for ts, ratio in items:
        split_date = ts.date() if hasattr(ts, "date") else ts
        try:
            ratio_dec = Decimal(str(ratio))
        except (InvalidOperation, ValueError):
            continue
        if ratio_dec <= 0:
            continue
        existing = session.get(StockSplit, (security_id, split_date))
        if existing is not None:
            existing.ratio = ratio_dec
            continue
        session.add(StockSplit(security_id=security_id, split_date=split_date, ratio=ratio_dec))
        written += 1
    return written


def main() -> None:
    written = run()
    # Plain ASCII so the message survives the Windows cp1252 console.
    print(f"splits complete: {written} new split rows written (as of {date.today()})")


if __name__ == "__main__":
    typer.run(main)
