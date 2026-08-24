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
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import Benchmark, PolicyState, PolicyWeight

# The 11 SPDR Select Sector ETFs — one per GICS sector. Stored like benchmarks
# so the Brinson allocation/selection attribution (`services.brinson`) can value
# each benchmark sector sleeve over a window. Never used as a whole-portfolio
# counterfactual; only the Brinson service references these symbols.
_SECTOR_ETFS: tuple[str, ...] = (
    "XLK",  # Information Technology
    "XLF",  # Financials
    "XLV",  # Health Care
    "XLE",  # Energy
    "XLY",  # Consumer Discretionary
    "XLP",  # Consumer Staples
    "XLI",  # Industrials
    "XLB",  # Materials
    "XLU",  # Utilities
    "XLRE",  # Real Estate
    "XLC",  # Communication Services
)

# Always-on benchmarks. Anything else comes from policy_weights.
# `^IRX` is the 13-week US T-bill yield (in percent) — stored like a benchmark
# so the risk-metrics service can use a time-varying risk-free rate instead of
# a flat constant. It's never used as a return counterfactual (nothing
# references the "^IRX" symbol except `beta._window_risk_free`).
_DEFAULT_BENCHMARKS: tuple[str, ...] = ("SPY", "QQQ", "^IRX", *_SECTOR_ETFS)


class PolicyBenchmarkCoverageError(RuntimeError):
    """The requested window lacks data for one or more policy components."""

    def __init__(self, missing_tickers: tuple[str, ...]) -> None:
        self.missing_tickers = missing_tickers
        super().__init__("policy benchmark coverage incomplete")


def run(start_date: date, end_date: date) -> int:
    rows_written = 0
    with SessionLocal() as session:
        policy_tickers = _policy_tickers(session)
        state = session.get(PolicyState, 1)
        target_revision = (
            state.revision if state is not None and state.benchmark_status == "required" else None
        )
        symbols = sorted(set(_DEFAULT_BENCHMARKS) | policy_tickers)
        for symbol in symbols:
            rows_written += _fetch_symbol(session, symbol, start_date, end_date)
        if target_revision is not None:
            _complete_policy_recomputation(
                session,
                policy_revision=target_revision,
                policy_tickers=policy_tickers,
                start_date=start_date,
                end_date=end_date,
            )
        session.commit()
    return rows_written


def _policy_tickers(session: Session) -> set[str]:
    rows = session.execute(select(PolicyWeight.ticker)).scalars().all()
    return {t for t in rows if t}


def _complete_policy_recomputation(
    session: Session,
    *,
    policy_revision: int,
    policy_tickers: set[str],
    start_date: date,
    end_date: date,
) -> bool:
    """Clear one unchanged policy revision only after component coverage exists."""
    session.flush()
    missing = tuple(
        sorted(
            ticker
            for ticker in policy_tickers
            if session.scalar(
                select(Benchmark.symbol)
                .where(
                    Benchmark.symbol == ticker,
                    Benchmark.date >= start_date,
                    Benchmark.date <= end_date,
                    Benchmark.total_return_close.is_not(None),
                )
                .limit(1)
            )
            is None
        )
    )
    if missing:
        raise PolicyBenchmarkCoverageError(missing)
    result = cast(
        CursorResult[Any],
        session.execute(
            update(PolicyState)
            .where(
                PolicyState.singleton_id == 1,
                PolicyState.revision == policy_revision,
                PolicyState.benchmark_status == "required",
            )
            .values(
                benchmark_status="current",
                benchmark_invalidated_at=None,
            )
        ),
    )
    return result.rowcount == 1


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
