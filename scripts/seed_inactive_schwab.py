"""Create an INACTIVE placeholder Schwab Item + Account for old 2021/2022
1099 records that don't correspond to any currently-linked account.

The Item is marked is_data_active=False so it doesn't affect V chart or
TWR. The Account exists purely so 1099 imports have an account_id to
attach to for ownership tracking.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.db import engine
from portfolio_tracker.models import Account, Item


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    with Session(engine) as session:
        # Already exists?
        existing = session.execute(
            select(Item).where(Item.institution_name == "Schwab")
        ).scalars().all()
        if existing:
            print(f"Schwab Item(s) already exist: {[i.item_id for i in existing]}")
            for i in existing:
                accts = session.execute(
                    select(Account).where(Account.item_id == i.item_id)
                ).scalars().all()
                for a in accts:
                    print(f"  acct[{a.account_id}] {a.name} mask={a.mask}")
            return

        print("Creating placeholder INACTIVE Schwab Item + Account ...")
        if args.commit:
            item = Item(
                source="manual",
                institution_name="Schwab",
                is_data_active=False,
                linked_at=datetime.now(timezone.utc),
            )
            session.add(item)
            session.flush()
            print(f"  + Item id={item.item_id}: Schwab (manual, INACTIVE)")

            acct = Account(
                item_id=item.item_id,
                plaid_account_id=f"manual:schwab-historical-{item.item_id}",
                name="Schwab Historical (1099-only)",
                type="brokerage",
                subtype=None,
                mask=None,
                currency="USD",
            )
            session.add(acct)
            session.flush()
            print(f"  + Account id={acct.account_id}: Schwab Historical (1099-only)")

            session.commit()
            print("\nCOMMITTED")
        else:
            print("(dry-run; re-run with --commit)")


if __name__ == "__main__":
    main()
