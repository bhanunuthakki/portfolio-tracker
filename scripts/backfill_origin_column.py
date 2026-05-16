"""Backfill the `origin` column on holdings_snapshots and investment_transactions.

Rules:
  * InvestmentTransaction: rows where plaid_investment_transaction_id starts
    with 'manual:' get origin='manual'. Everything else stays 'broker'.
  * HoldingSnapshot: rows in the MANUAL_SNAPSHOTS registry get origin='manual'.
    Everything else stays 'broker'.

The migration already defaulted all rows to 'broker'; we only need to
overwrite the manual ones.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from sqlalchemy import select, update, func
from sqlalchemy.orm import Session

from portfolio_tracker.db import engine
from portfolio_tracker.models import HoldingSnapshot, InvestmentTransaction, Security


# Snapshot rows we synthesized — same registry used by session_audit.py and
# reconcile_manual_changes.py. Keep these all aligned.
MANUAL_SNAPSHOTS: set[tuple[date, int, str]] = set()

# Transfer 1 SoFi lag-window backfill (5/09-5/11) — Bhanu's acct 6
for d in [date(2026, 5, 9), date(2026, 5, 10), date(2026, 5, 11)]:
    for tick in ("BN", "MELI", "NU", "RBRK", "SGOV", "VEEV"):
        MANUAL_SNAPSHOTS.add((d, 6, tick))

# Transfer 2 destination lag bridge — Elaine's SoFi acct 17 on 5/13 only
MANUAL_SNAPSHOTS.add((date(2026, 5, 13), 17, "SPY"))
MANUAL_SNAPSHOTS.add((date(2026, 5, 13), 17, "VTI"))

# Acct 14 + acct 15 backfill (5/09-5/14) with TODAY's positions at historical
# prices, after the phantom-tx wipe on 2026-05-15.
for d in [date(2026, 5, 9), date(2026, 5, 10), date(2026, 5, 11),
          date(2026, 5, 12), date(2026, 5, 13), date(2026, 5, 14)]:
    for tick in ("RBRK", "NOW", "WIX"):
        MANUAL_SNAPSHOTS.add((d, 14, tick))
    for tick in ("MELI", "SGOV", "BN", "RBRK", "NU"):
        MANUAL_SNAPSHOTS.add((d, 15, tick))

# Acct 16 residual restorations (5/10-5/14) — assumed real per Transfer 1
# but kept in PLAN as sync-error fallback. Tagged manual.
for d in [date(2026, 5, 10), date(2026, 5, 11), date(2026, 5, 12),
          date(2026, 5, 13), date(2026, 5, 14)]:
    for tick in ("BN", "MELI", "RBRK", "SGOV"):
        MANUAL_SNAPSHOTS.add((d, 16, tick))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    with Session(engine) as session:
        # --- 1. InvestmentTransaction by ID prefix ---
        tx_manual_count = session.execute(
            select(func.count())
            .select_from(InvestmentTransaction)
            .where(InvestmentTransaction.plaid_investment_transaction_id.like("manual:%"))
        ).scalar() or 0
        tx_total = session.execute(
            select(func.count()).select_from(InvestmentTransaction)
        ).scalar() or 0
        print(f"Investment transactions: {tx_total} total, {tx_manual_count} manual (by id prefix)")
        if args.commit:
            session.execute(
                update(InvestmentTransaction)
                .where(InvestmentTransaction.plaid_investment_transaction_id.like("manual:%"))
                .values(origin="manual")
            )

        # --- 2. HoldingSnapshot by MANUAL_SNAPSHOTS registry ---
        # First resolve ticker -> security_id
        snap_total = session.execute(select(func.count()).select_from(HoldingSnapshot)).scalar() or 0
        tickers_in_registry = {tick for _, _, tick in MANUAL_SNAPSHOTS}
        sid_by_tick = {
            t: sid for sid, t in session.execute(
                select(Security.security_id, Security.ticker)
                .where(Security.ticker.in_(list(tickers_in_registry)))
            ).all()
        }
        # Build the list of (date, acct, sid) tuples actually present in the DB
        manual_targets = []
        for d, acct, tick in MANUAL_SNAPSHOTS:
            sid = sid_by_tick.get(tick)
            if sid is not None:
                manual_targets.append((d, acct, sid))

        snap_manual_count = 0
        for d, acct, sid in manual_targets:
            existing = session.execute(
                select(HoldingSnapshot)
                .where(HoldingSnapshot.snapshot_date == d)
                .where(HoldingSnapshot.account_id == acct)
                .where(HoldingSnapshot.security_id == sid)
            ).scalar_one_or_none()
            if existing is not None:
                snap_manual_count += 1
                if args.commit:
                    existing.origin = "manual"

        print(f"Holdings snapshots: {snap_total} total, {snap_manual_count} marked manual (from registry of {len(MANUAL_SNAPSHOTS)} entries)")

        if args.commit:
            session.commit()
            print("\nCOMMITTED")
        else:
            print("\n(dry-run; re-run with --commit)")


if __name__ == "__main__":
    main()
