"""One-shot daily refresh: Plaid snapshot + SnapTrade sync + value cache.

This is the script you point Task Scheduler / cron / launchd at. It:

  1. Snapshots every linked Plaid Item (writes today's holdings rows and
     refreshes today's `portfolio_values_daily` cache row inline).
  2. Captures Plaid investment activity and its source attestations. The
     snapshot stores balances only, so returns need this capture.
  3. Syncs every configured SnapTrade profile (primary + spouse), pulling
     today's holdings and recent transactions into the same tables.
  4. Re-runs the daily-values cache update so today's row reflects whatever
     SnapTrade pulled (snapshot.run already did this for the Plaid leg, but
     SnapTrade lands afterwards so we redo it).

Prints a one-line summary per step and continues even if one source is
unavailable — a Plaid 500 shouldn't kill the SnapTrade leg, and vice versa.

Run manually:
    python -m portfolio_tracker.jobs.daily_refresh

Schedule daily (Windows, see `scripts/run_daily_refresh.bat`):
    Task Scheduler → Create Basic Task → Daily → 6:00 AM →
    Start a program → wscript "scripts\run_daily_refresh.vbs"
"""

from __future__ import annotations

import traceback
from datetime import date, timedelta

from fastapi import HTTPException

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.jobs import (
    backfill,
    daily_values,
    reconcile_option_direction,
    snapshot,
)
from portfolio_tracker.services.provider_transaction_provenance import (
    ProviderTransactionConflictError,
)


def run() -> int:
    """Run the full daily refresh. Returns 0 on success, nonzero if any
    step failed. Continues through individual failures so a broken
    aggregator doesn't block the others."""
    today = date.today()
    print(f"[daily_refresh] {today} starting")
    failures = 0

    # 1. Plaid snapshot — handles its own session and also refreshes
    #    today's row in `portfolio_values_daily` after committing.
    try:
        written = snapshot.run()
        print(f"[daily_refresh]   plaid snapshot: {written} holdings rows written")
    except Exception:
        failures += 1
        print("[daily_refresh]   plaid snapshot: FAILED")
        traceback.print_exc()

    # 1b. Plaid investment activity. The snapshot above stores balances only.
    #     Without this capture, flow evidence for every Plaid account stops at
    #     the last manual backfill and every later return window is unavailable.
    #     SnapTrade already refreshes the same evidence in its sync below.
    try:
        capture = backfill.run_with_delivery_receipts(end_date=today)
        print(
            f"[daily_refresh]   plaid transactions: {capture.rows_written} rows written "
            f"across {len(capture.item_receipts)} item(s)"
        )
    except ProviderTransactionConflictError as exc:
        # The rollback keeps the prior evidence. The plan is private.
        plan = exc.correction_plan
        failures += 1
        print(
            "[daily_refresh]   plaid transactions: FAILED (provider conflict on "
            f"{len(plan.corrections) if plan is not None else 'unknown'} transaction(s); "
            "inspect the private correction preview)"
        )
    except Exception:
        failures += 1
        print("[daily_refresh]   plaid transactions: FAILED")
        traceback.print_exc()

    # 1c. Option direction. Plaid omits long/short for SoFi options, so the
    #     sign comes from broker fills; re-check now that today's are captured.
    try:
        corrected = reconcile_option_direction.run(today)
        print(f"[daily_refresh]   option direction: {corrected} row(s) corrected")
    except Exception:
        failures += 1
        print("[daily_refresh]   option direction: FAILED")
        traceback.print_exc()

    # 2. SnapTrade sync — once per configured profile. Imported lazily so
    #    a missing snaptrade SDK doesn't take down the Plaid leg.
    try:
        from portfolio_tracker.api.routes.snaptrade import (
            SnapTradeProfile,
        )
        from portfolio_tracker.api.routes.snaptrade import (
            sync as snaptrade_sync,
        )

        for profile in SnapTradeProfile:
            try:
                with SessionLocal() as session:
                    result = snaptrade_sync(session, profile)
                print(
                    f"[daily_refresh]   snaptrade {profile.value}: "
                    f"items={result.items_synced} "
                    f"accounts={result.accounts_synced} "
                    f"holdings={result.holdings_written} "
                    f"txs={result.transactions_written} "
                    f"descriptions_preserved={result.descriptions_preserved}"
                )
            except HTTPException as exc:
                # 404 = no SnapTrade user for this profile yet; that's fine.
                if exc.status_code == 404:
                    print(f"[daily_refresh]   snaptrade {profile.value}: skipped (not configured)")
                    continue
                failures += 1
                print(
                    f"[daily_refresh]   snaptrade {profile.value}: "
                    f"FAILED ({exc.status_code} {exc.detail})"
                )
            except Exception:
                failures += 1
                print(f"[daily_refresh]   snaptrade {profile.value}: FAILED")
                traceback.print_exc()
    except Exception:
        failures += 1
        print("[daily_refresh]   snaptrade module: FAILED to import / configure")
        traceback.print_exc()

    # 3. Final daily-values cache refresh — idempotent, makes sure today's
    #    row reflects whatever the syncs pulled.
    try:
        daily_values.run(start_date=today, end_date=today)
        print("[daily_refresh]   daily_values cache: refreshed")
    except Exception:
        failures += 1
        print("[daily_refresh]   daily_values cache: FAILED")
        traceback.print_exc()

    # 4. Benchmarks — pull SPY/QQQ + every policy ticker. Without this,
    #    a newly added policy ticker (e.g., VTI/VWO after the user edits
    #    their allocation) silently has no benchmark data, and the policy
    #    synthetic line on the chart renormalizes around the gap. We run
    #    benchmarks AFTER snapshots/syncs so any newly-resolved symbols
    #    from today's transactions also flow into the pull list.
    try:
        from datetime import timedelta as _td

        from portfolio_tracker.jobs import benchmarks

        # A 30-day rolling pull keeps current revisions fresh. When a policy
        # revision is still required, run_with_coverage automatically expands
        # the one-time fetch/validation to the canonical two-year return horizon.
        result = benchmarks.run_with_coverage(today - _td(days=30), today)
        if result.status == "partial":
            print(
                "[daily_refresh]   benchmarks: PARTIAL "
                f"({result.rows_written} rows upserted; "
                f"{len(result.endpoint_coverage.missing_marks)} endpoint mark(s) unavailable)"
            )
        else:
            print(f"[daily_refresh]   benchmarks: {result.rows_written} rows upserted")
    except Exception:
        failures += 1
        print("[daily_refresh]   benchmarks: FAILED")
        traceback.print_exc()

    # 5. Per-security prices — yfinance/Stooq close history for every held
    #    security. daily_refresh historically skipped this, so the `prices`
    #    table drifted stale between manual `jobs.prices` runs, degrading the
    #    transaction walk-back valuation and the data-quality
    #    `no_historical_prices` checks. Like benchmarks, a 30-day rolling pull
    #    keeps the latest closes fresh without re-fetching years of static
    #    history every day. Runs AFTER snapshots/syncs so securities added by
    #    today's activity get priced. Imported lazily to keep yfinance off the
    #    critical-path startup, matching the benchmarks leg.
    try:
        from portfolio_tracker.jobs import prices

        price_rows, by_source = prices.run(
            today - timedelta(days=30), today, prices.DAILY_TIME_BUDGET_SECONDS
        )
        deferred = by_source.get("deferred", 0)
        print(
            f"[daily_refresh]   prices: {'PARTIAL ' if deferred else ''}"
            f"{price_rows} rows written "
            f"(yfinance={by_source.get('yfinance', 0)} "
            f"stooq={by_source.get('stooq', 0)} "
            f"none={by_source.get('none', 0)} deferred={deferred})"
        )
    except Exception:
        failures += 1
        print("[daily_refresh]   prices: FAILED")
        traceback.print_exc()

    # 6. Security classification — sector/region enrichment for the positioning
    #    view. `only_missing=True` so the daily path only hits yfinance for
    #    securities never classified before (a no-op on a steady book); a full
    #    refetch is a manual `python -m portfolio_tracker.jobs.classify_securities`.
    #    Imported lazily to keep yfinance off the critical path, like the legs above.
    try:
        from portfolio_tracker.jobs import classify_securities

        counts = classify_securities.run(only_missing=True)
        print(
            f"[daily_refresh]   classify_securities: "
            f"classified={counts['classified']} no_data={counts['no_data']}"
        )
    except Exception:
        failures += 1
        print("[daily_refresh]   classify_securities: FAILED")
        traceback.print_exc()

    status = "OK" if failures == 0 else f"{failures} step(s) FAILED"
    print(f"[daily_refresh] {today} done: {status}")
    return failures


if __name__ == "__main__":
    raise SystemExit(0 if run() == 0 else 1)
