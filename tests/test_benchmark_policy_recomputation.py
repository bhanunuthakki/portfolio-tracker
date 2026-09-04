"""Policy revision completion behavior in the benchmark refresh job."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from portfolio_tracker.jobs import benchmarks
from portfolio_tracker.models import Benchmark, PolicyState, PolicyWeight


def _seed_required_policy(session: Session) -> None:
    session.add(
        PolicyState(
            singleton_id=1,
            revision=3,
            source="earnings_summary",
            as_of=datetime(2026, 8, 23, tzinfo=UTC),
            benchmark_status="required",
            benchmark_invalidated_at=datetime(2026, 8, 23, tzinfo=UTC),
        )
    )
    session.add_all(
        [
            PolicyWeight(ticker="QQQ", weight_bps=6000),
            PolicyWeight(ticker="VXUS", weight_bps=4000),
        ]
    )
    session.commit()


def test_successful_benchmark_run_clears_matching_policy_revision(
    client, engine, session, monkeypatch
) -> None:
    _seed_required_policy(session)
    day = date(2026, 8, 21)

    def _session_local() -> Session:
        return Session(engine)

    def _fetch(job_session: Session, symbol: str, start_date: date, _end_date: date) -> int:
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
    monkeypatch.setattr(benchmarks, "_POLICY_SUPPORTED_HORIZON_DAYS", 0)

    benchmarks.run(day, day)

    session.expire_all()
    state = session.get_one(PolicyState, 1)
    assert state.revision == 3
    assert state.benchmark_status == "current"
    assert state.benchmark_invalidated_at is None
    assert client.get("/api/policy").json()["recomputation"] == {
        "status": "current",
        "policy_revision": 3,
        "reason": None,
    }


def test_incomplete_policy_coverage_fails_and_leaves_revision_required(
    engine, session, monkeypatch
) -> None:
    _seed_required_policy(session)
    day = date(2026, 8, 21)

    def _session_local() -> Session:
        return Session(engine)

    def _fetch(job_session: Session, symbol: str, start_date: date, _end_date: date) -> int:
        if symbol == "VXUS":
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
    monkeypatch.setattr(benchmarks, "_POLICY_SUPPORTED_HORIZON_DAYS", 0)

    with pytest.raises(benchmarks.PolicyBenchmarkCoverageError):
        benchmarks.run(day, day)

    session.expire_all()
    state = session.get_one(PolicyState, 1)
    assert state.revision == 3
    assert state.benchmark_status == "required"
    assert state.benchmark_invalidated_at is not None


def test_benchmark_run_fetches_prior_close_needed_for_weekend_start(
    engine, session, monkeypatch
) -> None:
    _seed_required_policy(session)
    saturday = date(2026, 8, 22)
    sunday = date(2026, 8, 23)
    friday = date(2026, 8, 21)
    fetched_starts: set[date] = set()

    def _session_local() -> Session:
        return Session(engine)

    def _fetch(job_session: Session, symbol: str, start_date: date, _end_date: date) -> int:
        fetched_starts.add(start_date)
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
    monkeypatch.setattr(benchmarks, "_POLICY_SUPPORTED_HORIZON_DAYS", 0)

    benchmarks.run(saturday, sunday)

    assert fetched_starts == {friday}


def test_completed_older_revision_cannot_clear_newer_required_revision(session) -> None:
    _seed_required_policy(session)
    day = date(2026, 8, 21)
    session.add_all(
        [
            Benchmark(
                symbol=ticker,
                date=day,
                close=Decimal("100"),
                total_return_close=Decimal("100"),
            )
            for ticker in ("QQQ", "VXUS")
        ]
    )
    state = session.get_one(PolicyState, 1)
    state.revision = 4
    session.commit()

    completed = benchmarks._complete_policy_recomputation(
        session,
        policy_revision=3,
        policy_tickers={"QQQ", "VXUS"},
        start_date=day,
        end_date=day,
    )

    assert completed is False
    session.expire_all()
    assert session.get_one(PolicyState, 1).benchmark_status == "required"


def test_policy_completion_reports_every_missing_required_market_close(session) -> None:
    _seed_required_policy(session)
    friday = date(2026, 8, 21)
    monday = date(2026, 8, 24)
    session.add_all(
        [
            Benchmark(
                symbol=ticker,
                date=friday,
                close=Decimal("100"),
                total_return_close=Decimal("100"),
            )
            for ticker in ("QQQ", "VXUS")
        ]
    )
    session.commit()

    with pytest.raises(benchmarks.PolicyBenchmarkCoverageError) as exc_info:
        benchmarks._complete_policy_recomputation(
            session,
            policy_revision=3,
            policy_tickers={"QQQ", "VXUS"},
            start_date=friday,
            end_date=monday,
        )

    assert exc_info.value.missing_tickers == ("QQQ", "VXUS")
    assert exc_info.value.reason_code == "policy_benchmark_date_coverage_incomplete"
    assert exc_info.value.missing_dates_by_ticker == {
        "QQQ": (monday,),
        "VXUS": (monday,),
    }


def test_policy_completion_resolves_weekend_window_to_previous_market_close(session) -> None:
    _seed_required_policy(session)
    friday = date(2026, 8, 21)
    saturday = date(2026, 8, 22)
    sunday = date(2026, 8, 23)
    session.add_all(
        [
            Benchmark(
                symbol=ticker,
                date=friday,
                close=Decimal("100"),
                total_return_close=Decimal("100"),
            )
            for ticker in ("QQQ", "VXUS")
        ]
    )
    session.commit()

    completed = benchmarks._complete_policy_recomputation(
        session,
        policy_revision=3,
        policy_tickers={"QQQ", "VXUS"},
        start_date=saturday,
        end_date=sunday,
    )

    assert completed is True


def test_required_policy_refresh_expands_short_pull_to_two_year_horizon(
    engine, session, monkeypatch
) -> None:
    _seed_required_policy(session)
    end = date(2026, 8, 21)
    requested_starts: set[date] = set()

    def _session_local() -> Session:
        return Session(engine)

    def _fetch(_session: Session, _symbol: str, start_date: date, _end_date: date) -> int:
        requested_starts.add(start_date)
        return 0

    monkeypatch.setattr(benchmarks, "SessionLocal", _session_local)
    monkeypatch.setattr(benchmarks, "_fetch_symbol", _fetch)

    with pytest.raises(benchmarks.PolicyBenchmarkCoverageError):
        benchmarks.run(end, end)

    expected = benchmarks.benchmark_source_date(end - timedelta(days=730))
    assert requested_starts == {expected}
    session.expire_all()
    assert session.get_one(PolicyState, 1).benchmark_status == "required"


def test_zero_weight_policy_ticker_is_not_required(session) -> None:
    session.add_all(
        [
            PolicyWeight(ticker="SPY", weight_bps=10000),
            PolicyWeight(ticker="VXUS", weight_bps=0),
        ]
    )
    session.commit()

    assert benchmarks._policy_tickers(session) == {"SPY"}
