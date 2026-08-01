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

from portfolio_tracker.models import Account, HoldingSnapshot, Item


def active_account_ids(session: Session) -> frozenset[int]:
    """Return account_ids belonging to items where `is_data_active` is True."""
    rows = (
        session.execute(
            select(Account.account_id)
            .join(Item, Item.item_id == Account.item_id)
            .where(Item.is_data_active.is_(True))
        )
        .scalars()
        .all()
    )
    return frozenset(rows)


def valued_account_ids(session: Session) -> frozenset[int]:
    """Active accounts that have EVER produced a holdings snapshot.

    Return math only works when the value series V and the external-cashflow
    series C describe the same set of accounts. Modified Dietz computes
    ``(V_end − V_start − ΣC) / …`` — so an account that contributes to C but
    never to V has its contributions subtracted as "money you put in" while
    the assets those contributions bought never show up as "money you have."
    Every dollar deposited there is booked as a loss.

    That is not hypothetical: the Fidelity-held META 401(k) syncs its payroll
    deferrals through SnapTrade as `cash/contribution` transactions but exposes
    no positions, so it had $76,823 of contributions and zero snapshots. On the
    default window that was $11,651 of a $26,113 net inflow — 45% of the
    denominator's cashflow term — dragging the reported return down ~1.8pp.

    Aggregations that pair value against flows (the performance series) use
    THIS set. Aggregations that only read one side, or that intentionally
    inventory everything the user has linked (data-quality, account listings),
    keep using `active_account_ids`. The exclusion is surfaced to the user by
    the `cashflow_without_value` data-quality finding rather than applied
    silently — an account quietly dropped from the return math is exactly the
    kind of correction that should be visible.
    """
    accts = active_account_ids(session)
    if not accts:
        return frozenset()
    rows = (
        session.execute(
            select(HoldingSnapshot.account_id)
            .where(HoldingSnapshot.account_id.in_(accts))
            .distinct()
        )
        .scalars()
        .all()
    )
    return frozenset(rows)
