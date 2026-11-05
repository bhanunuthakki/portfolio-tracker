"""Operational reporting for the one-shot daily refresh job."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any, cast

from portfolio_tracker.api.routes import snaptrade as snaptrade_routes
from portfolio_tracker.jobs import backfill, benchmarks, classify_securities, daily_refresh, prices
from portfolio_tracker.services.provider_transaction_provenance import (
    ProviderTransactionConflictError,
)


def _patch_steps_after_plaid_capture(monkeypatch, calls: list[str]) -> None:
    monkeypatch.setattr(daily_refresh.snapshot, "run", lambda: calls.append("snapshot") or 0)
    monkeypatch.setattr(daily_refresh.daily_values, "run", lambda **_kwargs: 0)

    def sync(*_args: object) -> SimpleNamespace:
        calls.append("snaptrade")
        return SimpleNamespace(
            items_synced=0,
            accounts_synced=0,
            holdings_written=0,
            transactions_written=0,
        )

    monkeypatch.setattr(snaptrade_routes, "sync", sync)
    monkeypatch.setattr(
        benchmarks,
        "run_with_coverage",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("benchmarks skipped in this test")),
    )
    monkeypatch.setattr(prices, "run", lambda *_args: calls.append("prices") or (0, {}))
    monkeypatch.setattr(
        classify_securities,
        "run",
        lambda **_kwargs: {"classified": 0, "no_data": 0},
    )


def test_daily_refresh_captures_plaid_activity_before_slow_market_data(
    monkeypatch,
    capsys,
) -> None:
    calls: list[str] = []
    _patch_steps_after_plaid_capture(monkeypatch, calls)

    def capture(*, end_date: date) -> backfill.BackfillRunResult:
        assert end_date == date.today()
        calls.append("plaid_transactions")
        return backfill.BackfillRunResult(rows_written=3, item_receipts=())

    monkeypatch.setattr(backfill, "run_with_delivery_receipts", capture)

    daily_refresh.run()

    assert calls.index("snapshot") < calls.index("plaid_transactions") < calls.index("prices")
    assert "plaid transactions: 3 rows written" in capsys.readouterr().out


def test_plaid_conflict_is_reported_without_blocking_later_steps(monkeypatch, capsys) -> None:
    calls: list[str] = []
    _patch_steps_after_plaid_capture(monkeypatch, calls)
    private_marker = "private-provider-record"
    plan = SimpleNamespace(corrections=(SimpleNamespace(record=private_marker),))

    def conflict(*, end_date: date) -> backfill.BackfillRunResult:
        raise ProviderTransactionConflictError(private_marker, correction_plan=cast("Any", plan))

    monkeypatch.setattr(backfill, "run_with_delivery_receipts", conflict)

    failures = daily_refresh.run()

    output = capsys.readouterr().out
    assert failures >= 1
    assert "plaid transactions: FAILED (provider conflict on 1 transaction(s)" in output
    assert private_marker not in output
    assert "snaptrade" in calls
    assert "prices" in calls


def test_daily_refresh_reports_partial_benchmark_endpoint_coverage(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(daily_refresh.snapshot, "run", lambda: 0)
    monkeypatch.setattr(
        backfill,
        "run_with_delivery_receipts",
        lambda **_kwargs: backfill.BackfillRunResult(rows_written=0, item_receipts=()),
    )
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
