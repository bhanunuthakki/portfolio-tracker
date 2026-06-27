"""Fetch benchmark closing prices via yfinance.

Always pulls SPY + QQQ as default comparators, plus every ticker referenced
in the `policy_weights` table so the user-defined policy benchmark can be
valued historically. Stored in a separate `benchmarks` table from `prices`
because benchmarks are symbol-keyed (no Security row needed).

Run manually:
    python -m portfolio_tracker.jobs.benchmarks --start 2023-01-01
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

import typer
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import Benchmark, PolicyWeight

# Always-on benchmarks. Anything else comes from policy_weights.
# `^IRX` is the 13-week US T-bill yield (in percent) — stored like a benchmark
# so the risk-metrics service can use a time-varying risk-free rate instead of
# a flat constant. It's never used as a return counterfactual (nothing
# references the "^IRX" symbol except `beta._window_risk_free`).
_DEFAULT_BENCHMARKS: tuple[str, ...] = ("SPY", "QQQ", "^IRX")


def run(start_date: date, end_date: date) -> int:
    rows_written = 0
    with SessionLocal() as session:
        symbols = sorted(set(_DEFAULT_BENCHMARKS) | _policy_tickers(session))
        for symbol in symbols:
            rows_written += _fetch_symbol(session, symbol, start_date, end_date)
        session.commit()
    return rows_written


def _policy_tickers(session: Session) -> set[str]:
    rows = session.execute(select(PolicyWeight.ticker)).scalars().all()
    return {t for t in rows if t}


def _fetch_symbol(session: Session, symbol: str, start_date: date, end_date: date) -> int:
    history = yf.Ticker(symbol).history(
        start=start_date.isoformat(),
        end=(end_date + timedelta(days=1)).isoformat(),
        auto_adjust=False,
    )
    if history.empty:
        return 0
    rows_written = 0
    for ts, row in cast(Any, history.iterrows()):
        bar_date = ts.date() if hasattr(ts, "date") else ts
        close = row.get("Close")
        if close is None or close != close:  # NaN check
            continue
        close_decimal = Decimal(str(close))
        # `Adj Close` is split- AND dividend-adjusted (dividends reinvested) —
        # the total-return series. Fall back to the raw close if it's missing
        # or NaN so the row is never written without a usable comparison value.
        adj = row.get("Adj Close")
        tr_decimal = Decimal(str(adj)) if adj is not None and adj == adj else close_decimal
        existing = session.get(Benchmark, (symbol, bar_date))
        if existing is not None:
            existing.close = close_decimal
            existing.total_return_close = tr_decimal
            continue
        session.add(
            Benchmark(
                symbol=symbol,
                date=bar_date,
                close=close_decimal,
                total_return_close=tr_decimal,
            )
        )
        rows_written += 1
    return rows_written


def main(
    start: str = typer.Option(..., help="ISO start date, e.g. 2023-01-01"),
    end: str | None = typer.Option(None, help="ISO end date; defaults to today"),
) -> None:
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end is not None else date.today()
    written = run(start_date, end_date)
    with SessionLocal() as session:
        symbols = sorted(set(_DEFAULT_BENCHMARKS) | _policy_tickers(session))
    print(f"benchmarks complete: {written} rows written across {symbols}")


if __name__ == "__main__":
    typer.run(main)
