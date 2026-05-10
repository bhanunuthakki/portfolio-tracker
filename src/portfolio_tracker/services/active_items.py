"""Single source of truth for "which accounts count toward our numbers".

Aggregations across the codebase (V series, transactions, holdings,
trade analysis, data quality) must respect `Item.is_data_active`.
Rather than repeat the JOIN in every query and risk forgetting one,
each call site resolves through `active_account_ids()` and applies a
plain `account_id IN (...)` filter.

Why a frozenset of ints rather than a SQL CTE / subquery? We sit on a
small SQLite — 10s of accounts, never 10,000. The dataset is trivially
small enough that a single `SELECT account_id FROM accounts JOIN items
WHERE is_data_active` returns in <1ms, and a Python-side `IN` filter
is unambiguous to read at every call site. If the aggregations ever
grow to a meaningful scale we'll switch to a JOIN; until then, clarity
wins.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import Account, Item


def active_account_ids(session: Session) -> frozenset[int]:
    """Return account_ids belonging to items where `is_data_active` is True."""
    rows = session.execute(
        select(Account.account_id)
        .join(Item, Item.item_id == Account.item_id)
        .where(Item.is_data_active.is_(True))
    ).scalars().all()
    return frozenset(rows)
