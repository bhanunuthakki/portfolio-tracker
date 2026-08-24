"""Policy revision completion behavior in the benchmark refresh job."""

from __future__ import annotations

from datetime import UTC, date, datetime
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

    with pytest.raises(benchmarks.PolicyBenchmarkCoverageError):
        benchmarks.run(day, day)

    session.expire_all()
    state = session.get_one(PolicyState, 1)
    assert state.revision == 3
    assert state.benchmark_status == "required"
    assert state.benchmark_invalidated_at is not None


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
