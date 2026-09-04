"""Fetch benchmark closing prices via yfinance.

Always pulls SPY + QQQ as default comparators, plus every ticker referenced
in the `policy_weights` table so the user-defined policy benchmark can be
valued historically. Stored in a separate `benchmarks` table from `prices`
because benchmarks are symbol-keyed (no Security row needed).

Run manually:
    python -m portfolio_tracker.jobs.benchmarks --start 2023-01-01
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

import typer
import yfinance as yf
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import Benchmark, PolicyState, PolicyWeight
from portfolio_tracker.services.performance import benchmark_source_date

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

# Policy status is global rather than window-qualified. A required revision can
# therefore become ``current`` only after the longest first-class performance
# lookback (two years) is available, not merely the 30-day maintenance pull.
_POLICY_SUPPORTED_HORIZON_DAYS = 730

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BenchmarkEndpointMark:
    """Availability of one exact benchmark source mark for a requested boundary."""

    target_date: date
    source_date: date
    resolution: Literal["same_day", "previous_market_close"]
    status: Literal["available", "missing", "nonpositive"]
    return_basis: Literal["total_return_adjusted", "raw_price_fallback"] | None


@dataclass(frozen=True)
class BenchmarkSymbolEndpointCoverage:
    """Endpoint coverage and stored positive-date extent for one symbol."""

    symbol: str
    earliest_available_date: date | None
    latest_available_date: date | None
    endpoint_marks: tuple[BenchmarkEndpointMark, ...]

    @property
    def is_complete(self) -> bool:
        return all(mark.status == "available" for mark in self.endpoint_marks)


@dataclass(frozen=True)
class BenchmarkEndpointCoverage:
    """Read-only evidence that all comparator boundary marks are usable."""

    requested_start_date: date
    requested_end_date: date
    symbols: tuple[BenchmarkSymbolEndpointCoverage, ...]

    @property
    def is_complete(self) -> bool:
        return all(symbol.is_complete for symbol in self.symbols)

    @property
    def missing_marks(self) -> tuple[tuple[str, BenchmarkEndpointMark], ...]:
        return tuple(
            (symbol.symbol, mark)
            for symbol in self.symbols
            for mark in symbol.endpoint_marks
            if mark.status != "available"
        )


@dataclass(frozen=True)
class BenchmarkRefreshResult:
    """Refresh count plus exact endpoint coverage for operational receipts."""

    rows_written: int
    endpoint_coverage: BenchmarkEndpointCoverage

    @property
    def status(self) -> Literal["complete", "partial"]:
        return "complete" if self.endpoint_coverage.is_complete else "partial"


def _report_partial_endpoint_coverage(
    coverage: BenchmarkEndpointCoverage,
    *,
    rows_written: int,
) -> None:
    if coverage.is_complete:
        return
    gap_details = ",".join(
        f"{symbol}:{mark.target_date}->{mark.source_date}:{mark.status}"
        for symbol, mark in coverage.missing_marks
    )
    logger.warning(
        "benchmark refresh partial endpoint coverage: rows_written=%d gaps=%s",
        rows_written,
        gap_details,
    )


class PolicyBenchmarkCoverageError(RuntimeError):
    """The requested window lacks data for one or more policy components."""

    reason_code = "policy_benchmark_date_coverage_incomplete"

    def __init__(self, missing_dates_by_ticker: dict[str, tuple[date, ...]]) -> None:
        self.missing_dates_by_ticker = missing_dates_by_ticker
        self.missing_tickers = tuple(sorted(missing_dates_by_ticker))
        super().__init__("policy benchmark coverage incomplete")


def assess_endpoint_coverage(
    session: Session,
    start_date: date,
    end_date: date,
    *,
    symbols: set[str] | frozenset[str] | None = None,
) -> BenchmarkEndpointCoverage:
    """Inspect exact opening/ending marks without modifying benchmark state.

    The assessment mirrors performance's fail-closed boundary semantics: a US
    market session requires its same-day mark, while a weekend or known market
    holiday resolves to the immediately preceding market close. Missing
    adjusted prices remain usable through the documented raw-close fallback.
    """
    required_symbols = (
        frozenset(symbols)
        if symbols is not None
        else frozenset({"SPY", "QQQ"} | _policy_tickers(session))
    )
    target_dates = tuple(dict.fromkeys((start_date, end_date)))
    source_dates = {target_date: benchmark_source_date(target_date) for target_date in target_dates}
    requested_source_dates = frozenset(source_dates.values())

    rows = session.execute(
        select(
            Benchmark.symbol,
            Benchmark.date,
            Benchmark.close,
            Benchmark.total_return_close,
        ).where(
            Benchmark.symbol.in_(required_symbols),
            Benchmark.date.in_(requested_source_dates),
        )
    ).all()
    rows_by_key = {
        (symbol, benchmark_date): (
            Decimal(close),
            Decimal(total_return_close) if total_return_close is not None else None,
        )
        for symbol, benchmark_date, close, total_return_close in rows
    }

    available_extents = {
        symbol: (earliest, latest)
        for symbol, earliest, latest in session.execute(
            select(
                Benchmark.symbol,
                func.min(Benchmark.date),
                func.max(Benchmark.date),
            )
            .where(
                Benchmark.symbol.in_(required_symbols),
                func.coalesce(Benchmark.total_return_close, Benchmark.close) > 0,
            )
            .group_by(Benchmark.symbol)
        ).all()
    }

    symbol_coverage: list[BenchmarkSymbolEndpointCoverage] = []
    for symbol in sorted(required_symbols):
        endpoint_marks: list[BenchmarkEndpointMark] = []
        for target_date in target_dates:
            source_date = source_dates[target_date]
            stored = rows_by_key.get((symbol, source_date))
            if stored is None:
                status: Literal["available", "missing", "nonpositive"] = "missing"
                return_basis = None
            else:
                raw_close, adjusted_close = stored
                selected_close = adjusted_close if adjusted_close is not None else raw_close
                status = "available" if selected_close > 0 else "nonpositive"
                return_basis = (
                    "total_return_adjusted" if adjusted_close is not None else "raw_price_fallback"
                )
            endpoint_marks.append(
                BenchmarkEndpointMark(
                    target_date=target_date,
                    source_date=source_date,
                    resolution=(
                        "same_day" if target_date == source_date else "previous_market_close"
                    ),
                    status=status,
                    return_basis=return_basis,
                )
            )
        earliest, latest = available_extents.get(symbol, (None, None))
        symbol_coverage.append(
            BenchmarkSymbolEndpointCoverage(
                symbol=symbol,
                earliest_available_date=earliest,
                latest_available_date=latest,
                endpoint_marks=tuple(endpoint_marks),
            )
        )
    return BenchmarkEndpointCoverage(
        requested_start_date=start_date,
        requested_end_date=end_date,
        symbols=tuple(symbol_coverage),
    )


def run_with_coverage(start_date: date, end_date: date) -> BenchmarkRefreshResult:
    rows_written = 0
    with SessionLocal() as session:
        policy_tickers = _policy_tickers(session)
        state = session.get(PolicyState, 1)
        target_revision = (
            state.revision if state is not None and state.benchmark_status == "required" else None
        )
        coverage_start_date = (
            min(start_date, end_date - timedelta(days=_POLICY_SUPPORTED_HORIZON_DAYS))
            if target_revision is not None
            else start_date
        )
        symbols = sorted(set(_DEFAULT_BENCHMARKS) | policy_tickers)
        fetch_start_date = benchmark_source_date(coverage_start_date)
        for symbol in symbols:
            rows_written += _fetch_symbol(session, symbol, fetch_start_date, end_date)
        endpoint_coverage = assess_endpoint_coverage(
            session,
            start_date,
            end_date,
            symbols={"SPY", "QQQ"} | policy_tickers,
        )
        _report_partial_endpoint_coverage(endpoint_coverage, rows_written=rows_written)
        if target_revision is not None:
            _complete_policy_recomputation(
                session,
                policy_revision=target_revision,
                policy_tickers=policy_tickers,
                start_date=coverage_start_date,
                end_date=end_date,
            )
        session.commit()
    return BenchmarkRefreshResult(
        rows_written=rows_written,
        endpoint_coverage=endpoint_coverage,
    )


def run(start_date: date, end_date: date) -> int:
    """Refresh benchmark rows and retain the legacy integer return contract."""
    return run_with_coverage(start_date, end_date).rows_written


def _policy_tickers(session: Session) -> set[str]:
    rows = (
        session.execute(select(PolicyWeight.ticker).where(PolicyWeight.weight_bps > 0))
        .scalars()
        .all()
    )
    return {t for t in rows if t}


def _complete_policy_recomputation(
    session: Session,
    *,
    policy_revision: int,
    policy_tickers: set[str],
    start_date: date,
    end_date: date,
) -> bool:
    """Clear one unchanged revision only after every required close exists.

    Checking for merely one row per ticker can mark a multi-year policy
    recomputation current despite holes at its opening, ending, or intervening
    dates. Requiring every market close needed by the requested calendar
    window also covers weekend/holiday targets through their preceding close.
    """
    session.flush()
    required_dates = _required_benchmark_source_dates(start_date, end_date)
    missing_dates_by_ticker: dict[str, tuple[date, ...]] = {}
    for ticker in sorted(policy_tickers):
        covered_dates = set(
            session.execute(
                select(Benchmark.date).where(
                    Benchmark.symbol == ticker,
                    Benchmark.date.in_(required_dates),
                    Benchmark.total_return_close.is_not(None),
                    Benchmark.total_return_close > 0,
                )
            )
            .scalars()
            .all()
        )
        missing_dates = tuple(day for day in required_dates if day not in covered_dates)
        if missing_dates:
            missing_dates_by_ticker[ticker] = missing_dates
    if missing_dates_by_ticker:
        raise PolicyBenchmarkCoverageError(missing_dates_by_ticker)
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


def _required_benchmark_source_dates(start_date: date, end_date: date) -> tuple[date, ...]:
    """Unique market closes required to value every date in the window."""
    if end_date < start_date:
        return ()
    return tuple(
        sorted(
            {
                benchmark_source_date(start_date + timedelta(days=offset))
                for offset in range((end_date - start_date).days + 1)
            }
        )
    )


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
    result = run_with_coverage(start_date, end_date)
    symbols = [symbol.symbol for symbol in result.endpoint_coverage.symbols]
    print(
        f"benchmarks {result.status}: {result.rows_written} rows written "
        f"across required comparators {symbols}"
    )
    if result.status == "partial":
        for symbol, mark in result.endpoint_coverage.missing_marks:
            print(
                "benchmark endpoint unavailable: "
                f"symbol={symbol} target_date={mark.target_date} "
                f"source_date={mark.source_date} reason={mark.status}"
            )


if __name__ == "__main__":
    typer.run(main)
