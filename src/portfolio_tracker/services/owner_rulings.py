"""Owner rulings that the calculation services apply in code.

A ruling records an owner-confirmed fact that provider data cannot express.
Each ruling names the exact account and its expected identity, and each one
verifies itself against stored evidence for the requested window. When the
evidence contradicts a ruling, the ruling does not apply, the calculation fails
closed through its normal coverage checks, and data quality reports the
contradiction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_tracker.models import Account, AccountValuationObservation, HoldingSnapshot, Item


@dataclass(frozen=True)
class ZeroValueFlowThroughRuling:
    """An account that receives external contributions and holds no value.

    Contributions arrive as external inflows and sweep to sibling accounts in
    the same return universe, where they are internal. The account itself
    holds nothing at end of day, so it never reports holdings. It stays in the
    flow ledger and is valued at zero instead of blocking every date as an
    account with missing holdings.
    """

    account_id: int
    institution_name: str
    ruled_on: date


ZERO_VALUE_FLOW_THROUGH: tuple[ZeroValueFlowThroughRuling, ...] = (
    ZeroValueFlowThroughRuling(
        account_id=19, institution_name="Fidelity", ruled_on=date(2026, 11, 12)
    ),
)


def zero_value_ruling_contradictions(
    session: Session,
    start_date: date,
    end_date: date,
    rulings: tuple[ZeroValueFlowThroughRuling, ...] | None = None,
) -> dict[int, str]:
    """Return ``{account_id: reason}`` for each ruling the evidence contradicts."""
    contradictions: dict[int, str] = {}
    for ruling in ZERO_VALUE_FLOW_THROUGH if rulings is None else rulings:
        identity = session.execute(
            select(Item.institution_name)
            .join(Account, Account.item_id == Item.item_id)
            .where(Account.account_id == ruling.account_id)
        ).scalar_one_or_none()
        if identity is None:
            contradictions[ruling.account_id] = "account_missing"
            continue
        if (identity or "").strip().lower() != ruling.institution_name.strip().lower():
            contradictions[ruling.account_id] = "institution_mismatch"
            continue
        holding_rows = session.scalar(
            select(func.count())
            .select_from(HoldingSnapshot)
            .where(HoldingSnapshot.account_id == ruling.account_id)
            .where(HoldingSnapshot.snapshot_date >= start_date)
            .where(HoldingSnapshot.snapshot_date <= end_date)
            .where(func.coalesce(HoldingSnapshot.institution_value, 0) != 0)
        )
        if holding_rows:
            contradictions[ruling.account_id] = "holdings_reported"
            continue
        valued_rows = session.scalar(
            select(func.count())
            .select_from(AccountValuationObservation)
            .where(AccountValuationObservation.account_id == ruling.account_id)
            .where(AccountValuationObservation.as_of_date >= start_date)
            .where(AccountValuationObservation.as_of_date <= end_date)
            .where(AccountValuationObservation.is_empty.is_(False))
            .where(AccountValuationObservation.total_value != 0)
        )
        if valued_rows:
            contradictions[ruling.account_id] = "nonzero_valuation_reported"
    return contradictions


def zero_value_account_ids(
    session: Session,
    start_date: date,
    end_date: date,
    rulings: tuple[ZeroValueFlowThroughRuling, ...] | None = None,
) -> frozenset[int]:
    """Ruled zero-value accounts whose ruling holds for the whole window."""
    active = ZERO_VALUE_FLOW_THROUGH if rulings is None else rulings
    contradicted = zero_value_ruling_contradictions(session, start_date, end_date, active)
    return frozenset(r.account_id for r in active if r.account_id not in contradicted)
