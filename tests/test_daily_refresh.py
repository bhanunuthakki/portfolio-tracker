"""Operational reporting for the one-shot daily refresh job."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from portfolio_tracker.api.routes import snaptrade as snaptrade_routes
from portfolio_tracker.jobs import benchmarks, classify_securities, daily_refresh, prices


def test_daily_refresh_reports_partial_benchmark_endpoint_coverage(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(daily_refresh.snapshot, "run", lambda: 0)
    monkeypatch.setattr(daily_refresh.daily_values, "run", lambda **_kwargs: 0)
    monkeypatch.setattr(
        snaptrade_routes,
        "sync",
        lambda *_args: SimpleNamespace(
            items_synced=0,
            accounts_synced=0,
            holdings_written=0,
            transactions_written=0,
        ),
    )
    endpoint = benchmarks.BenchmarkEndpointMark(
        target_date=date(2026, 9, 3),
        source_date=date(2026, 9, 3),
        resolution="same_day",
        status="missing",
        return_basis=None,
    )
    result = benchmarks.BenchmarkRefreshResult(
        rows_written=7,
        endpoint_coverage=benchmarks.BenchmarkEndpointCoverage(
            requested_start_date=date(2026, 8, 4),
            requested_end_date=date(2026, 9, 3),
            symbols=(
                benchmarks.BenchmarkSymbolEndpointCoverage(
                    symbol="SPY",
                    earliest_available_date=date(2026, 8, 4),
                    latest_available_date=date(2026, 9, 2),
                    endpoint_marks=(endpoint,),
                ),
            ),
        ),
    )
    monkeypatch.setattr(benchmarks, "run_with_coverage", lambda *_args: result)
    monkeypatch.setattr(
        benchmarks,
        "run",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy run should not be used")),
    )
    monkeypatch.setattr(prices, "run", lambda *_args: (0, {}))
    monkeypatch.setattr(
        classify_securities,
        "run",
        lambda **_kwargs: {"classified": 0, "no_data": 0},
    )

    status = daily_refresh.run()

    output = capsys.readouterr().out
    assert status == 0
    assert "benchmarks: PARTIAL" in output
    assert "7 rows upserted" in output
    assert "1 endpoint mark(s) unavailable" in output
