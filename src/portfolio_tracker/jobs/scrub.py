"""Drop non-investment accounts from the local DB.

Idempotent. Run any time you've linked an Item that exposes both investment
and non-investment accounts (e.g., a brokerage that also surfaces a checking
account under the same login). Cascades through holdings + transactions via
the existing FK constraints.

Items that end up with zero remaining accounts are reported but NOT auto-
deleted, since each Item still occupies a Plaid Trial slot. The user decides
whether to unlink them via the UI.

Run manually:
    python -m portfolio_tracker.jobs.scrub
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import INVESTMENT_ACCOUNT_TYPES, Account, Item


def run() -> tuple[int, list[tuple[int, str | None]]]:
    """Return (accounts_deleted, [(item_id, institution_name) for empty Items])."""
    with SessionLocal() as session:
        deleted = _delete_non_investment_accounts(session)
        empty_items = _find_items_with_no_accounts(session)
        session.commit()
    return deleted, empty_items


def _delete_non_investment_accounts(session: Session) -> int:
    to_delete = (
        session.execute(
            select(Account.account_id).where(Account.type.notin_(INVESTMENT_ACCOUNT_TYPES))
        )
        .scalars()
        .all()
    )
    if not to_delete:
        return 0
    session.execute(delete(Account).where(Account.account_id.in_(to_delete)))
    return len(to_delete)


def _find_items_with_no_accounts(session: Session) -> list[tuple[int, str | None]]:
    rows = session.execute(
        select(Item.item_id, Item.institution_name, func.count(Account.account_id))
        .outerjoin(Account, Account.item_id == Item.item_id)
        .group_by(Item.item_id)
        .having(func.count(Account.account_id) == 0)
    ).all()
    return [(int(item_id), name) for item_id, name, _ in rows]


if __name__ == "__main__":
    deleted, empty_items = run()
    print(f"scrub complete: deleted {deleted} non-investment account(s)")
    if empty_items:
        print(f"\n{len(empty_items)} Item(s) now have 0 investment accounts:")
        for item_id, name in empty_items:
            print(f"  - item_id={item_id}  institution={name or '<unknown>'}")
        print(
            "\nThese Items are still occupying Plaid Trial slots. Unlink via the UI to free them."
        )
    else:
        print("all linked Items still have at least one investment account.")
