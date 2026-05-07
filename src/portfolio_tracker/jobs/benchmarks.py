"""Fetch SPY / QQQ closing prices via yfinance.

Stored separately from `prices` because benchmarks are tracked by symbol
(no Security row is created — they're not held). Add more symbols here if
you want additional comparators.

Run manually:
    python -m portfolio_tracker.jobs.benchmarks --start 2023-01-01
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import typer
import yfinance as yf
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import Benchmark

BENCHMARK_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ")


def run(start_date: date, end_date: date) -> int:
    rows_written = 0
    with SessionLocal() as session:
        for symbol in BENCHMARK_SYMBOLS:
            rows_written += _fetch_symbol(session, symbol, start_date, end_date)
        session.commit()
    return rows_written


def _fetch_symbol(session: Session, symbol: str, start_date: date, end_date: date) -> int:
    history = yf.Ticker(symbol).history(
        start=start_date.isoformat(),
        end=(end_date + timedelta(days=1)).isoformat(),
        auto_adjust=False,
    )
    if history.empty:
        return 0
    rows_written = 0
    for ts, row in history.iterrows():
        bar_date = ts.date() if hasattr(ts, "date") else ts
        close = row.get("Close")
        if close is None or close != close:  # NaN check
            continue
        close_decimal = Decimal(str(close))
        existing = session.get(Benchmark, (symbol, bar_date))
        if existing is not None:
            existing.close = close_decimal
            continue
        session.add(Benchmark(symbol=symbol, date=bar_date, close=close_decimal))
        rows_written += 1
    return rows_written


def main(
    start: str = typer.Option(..., help="ISO start date, e.g. 2023-01-01"),
    end: str | None = typer.Option(None, help="ISO end date; defaults to today"),
) -> None:
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end is not None else date.today()
    written = run(start_date, end_date)
    print(f"benchmarks complete: {written} rows written across {BENCHMARK_SYMBOLS}")


if __name__ == "__main__":
    typer.run(main)
