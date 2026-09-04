"""Read-only benchmark endpoint coverage and refresh receipts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from portfolio_tracker.jobs import benchmarks
from portfolio_tracker.models import Benchmark


def test_endpoint_coverage_names_missing_exact_trading_day(session: Session) -> None:
    start = date(2026, 8, 21)  # Friday
    end = date(2026, 8, 24)  # Monday
    session.add(
        Benchmark(
            symbol="SPY",
            date=start,
            close=Decimal("100"),
            total_return_close=Decimal("101"),
        )
    )
    session.commit()

    coverage = benchmarks.assess_endpoint_coverage(
        session,
        start,
        end,
        symbols={"SPY"},
    )

    assert coverage.is_complete is False
    assert len(coverage.symbols) == 1
    symbol = coverage.symbols[0]
    assert symbol.earliest_available_date == start
    assert symbol.latest_available_date == start
    assert symbol.is_complete is False
    assert symbol.endpoint_marks == (
        benchmarks.BenchmarkEndpointMark(
            target_date=start,
            source_date=start,
            resolution="same_day",
            status="available",
            return_basis="total_return_adjusted",
        ),
        benchmarks.BenchmarkEndpointMark(
            target_date=end,
            source_date=end,
            resolution="same_day",
            status="missing",
            return_basis=None,
        ),
    )
    assert coverage.missing_marks == (("SPY", symbol.endpoint_marks[1]),)


def test_endpoint_coverage_uses_raw_close_when_adjusted_close_is_null(
    session: Session,
) -> None:
    day = date(2026, 8, 21)
    session.add(
        Benchmark(
            symbol="SPY",
            date=day,
            close=Decimal("100"),
            total_return_close=None,
        )
    )
    session.commit()

    coverage = benchmarks.assess_endpoint_coverage(session, day, day, symbols={"SPY"})

    assert coverage.is_complete is True
    assert coverage.symbols[0].endpoint_marks == (
        benchmarks.BenchmarkEndpointMark(
            target_date=day,
            source_date=day,
            resolution="same_day",
            status="available",
            return_basis="raw_price_fallback",
        ),
    )


def test_endpoint_coverage_resolves_non_session_boundary_to_prior_close(
    session: Session,
) -> None:
    friday = date(2026, 8, 21)
    weekend = date(2026, 8, 23)
    session.add(
        Benchmark(
            symbol="QQQ",
            date=friday,
            close=Decimal("200"),
            total_return_close=Decimal("201"),
        )
    )
    session.commit()

    coverage = benchmarks.assess_endpoint_coverage(
        session,
        weekend,
        weekend,
        symbols={"QQQ"},
    )

    assert coverage.is_complete is True
    assert coverage.symbols[0].endpoint_marks == (
        benchmarks.BenchmarkEndpointMark(
            target_date=weekend,
            source_date=friday,
            resolution="previous_market_close",
            status="available",
            return_basis="total_return_adjusted",
        ),
    )


def test_endpoint_coverage_distinguishes_nonpositive_from_missing(session: Session) -> None:
    day = date(2026, 8, 21)
    session.add(
        Benchmark(
            symbol="SPY",
            date=day,
            close=Decimal("100"),
            total_return_close=Decimal("0"),
        )
    )
    session.commit()

    coverage = benchmarks.assess_endpoint_coverage(session, day, day, symbols={"SPY"})

    mark = coverage.symbols[0].endpoint_marks[0]
    assert mark.status == "nonpositive"
    assert mark.return_basis == "total_return_adjusted"
    assert coverage.symbols[0].earliest_available_date is None
    assert coverage.symbols[0].latest_available_date is None


def test_refresh_result_reports_partial_required_comparator_coverage(engine, monkeypatch) -> None:
    day = date(2026, 8, 21)
    warnings: list[tuple[str, tuple[object, ...]]] = []

    def _session_local() -> Session:
        return Session(engine)

    def _fetch(job_session: Session, symbol: str, start_date: date, _end_date: date) -> int:
        if symbol != "SPY":
            return 0
        job_session.add(
            Benchmark(
                symbol=symbol,
                date=start_date,
                close=Decimal("100"),
                total_return_close=Decimal("100"),
            )
        )
        return 1

    monkeypatch.setattr(benchmarks, "SessionLocal", _session_local)
    monkeypatch.setattr(benchmarks, "_fetch_symbol", _fetch)
    monkeypatch.setattr(
        benchmarks.logger,
        "warning",
        lambda message, *args: warnings.append((message, args)),
    )

    result = benchmarks.run_with_coverage(day, day)

    assert result.rows_written == 1
    assert result.status == "partial"
    assert [
        (symbol, mark.target_date, mark.status)
        for symbol, mark in result.endpoint_coverage.missing_marks
    ] == [("QQQ", day, "missing")]
    assert warnings == [
        (
            "benchmark refresh partial endpoint coverage: rows_written=%d gaps=%s",
            (1, "QQQ:2026-08-21->2026-08-21:missing"),
        )
    ]


def test_legacy_run_retains_integer_return_contract(engine, monkeypatch) -> None:
    day = date(2026, 8, 21)

    def _session_local() -> Session:
        return Session(engine)

    def _fetch(job_session: Session, symbol: str, start_date: date, _end_date: date) -> int:
        if symbol not in {"SPY", "QQQ"}:
            return 0
        job_session.add(
            Benchmark(
                symbol=symbol,
                date=start_date,
                close=Decimal("100"),
                total_return_close=Decimal("100"),
            )
        )
        return 1

    monkeypatch.setattr(benchmarks, "SessionLocal", _session_local)
    monkeypatch.setattr(benchmarks, "_fetch_symbol", _fetch)

    written = benchmarks.run(day, day)

    assert written == 2
    assert isinstance(written, int)


def test_cli_reports_partial_endpoint_details(monkeypatch, capsys) -> None:
    day = date(2026, 8, 21)
    mark = benchmarks.BenchmarkEndpointMark(
        target_date=day,
        source_date=day,
        resolution="same_day",
        status="missing",
        return_basis=None,
    )
    result = benchmarks.BenchmarkRefreshResult(
        rows_written=7,
        endpoint_coverage=benchmarks.BenchmarkEndpointCoverage(
            requested_start_date=day,
            requested_end_date=day,
            symbols=(
                benchmarks.BenchmarkSymbolEndpointCoverage(
                    symbol="QQQ",
                    earliest_available_date=date(2026, 8, 20),
                    latest_available_date=date(2026, 8, 20),
                    endpoint_marks=(mark,),
                ),
            ),
        ),
    )
    monkeypatch.setattr(benchmarks, "run_with_coverage", lambda *_args: result)

    benchmarks.main(start=day.isoformat(), end=day.isoformat())

    output = capsys.readouterr().out
    assert "benchmarks partial: 7 rows written" in output
    assert "symbol=QQQ target_date=2026-08-21 source_date=2026-08-21 reason=missing" in output
