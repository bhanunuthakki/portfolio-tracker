"""One-shot daily refresh: Plaid snapshot + SnapTrade sync + value cache.

This is the script you point Task Scheduler / cron / launchd at. It:

  1. Snapshots every linked Plaid Item (writes today's holdings rows and
     refreshes today's `portfolio_values_daily` cache row inline).
  2. Syncs every configured SnapTrade profile (primary + spouse), pulling
     today's holdings and recent transactions into the same tables.
  3. Re-runs the daily-values cache update so today's row reflects whatever
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
from datetime import date

from fastapi import HTTPException

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.jobs import daily_values, snapshot


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

    # 2. SnapTrade sync — once per configured profile. Imported lazily so
    #    a missing snaptrade SDK doesn't take down the Plaid leg.
    try:
        from portfolio_tracker.api.routes.snaptrade import (
            SnapTradeProfile,
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
                    f"txs={result.transactions_written}"
                )
            except HTTPException as exc:
                # 404 = no SnapTrade user for this profile yet; that's fine.
                if exc.status_code == 404:
                    print(
                        f"[daily_refresh]   snaptrade {profile.value}: "
                        f"skipped (not configured)"
                    )
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

    status = "OK" if failures == 0 else f"{failures} step(s) FAILED"
    print(f"[daily_refresh] {today} done: {status}")
    return failures


if __name__ == "__main__":
    raise SystemExit(0 if run() == 0 else 1)
