"""Deterministic resolution of known provider-unresolved cash-flow decisions.

Provider synchronization records a provider-authored ``unresolved`` decision
when a delivered row cannot be classified automatically. Some of these rows
have one unambiguous economic reading that the stored evidence proves. This
module resolves exactly those patterns, and nothing else:

* ``contribution_inflow``: a provider transfer described exactly as
  "contribution" with a positive source amount is an external inflow
  (employer/payroll contribution).
* ``zero_economics``: a row with zero amount and zero quantity moves no money
  and no shares, so it contributes no flow.
* ``paired_in_kind_transfer``: an in-kind share transfer whose equal and
  opposite ACATS leg is recorded in another account of the same return
  universe moved shares inside the portfolio, so it is internal.

A rule never overrides an owner or statement decision, never invents an
amount, and requires the provider record to resolve to exactly one stored
transaction in the attested account. Consumers must verify that the canonical
ledger treats the record the way the rule says, and fail closed when it does
not. Every resolution is reported with its rule code.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    CashFlowReconciliationDecision,
    CashFlowSourceAttestation,
    CashFlowSourceEvent,
    InvestmentTransaction,
)

RuleCode = Literal["contribution_inflow", "zero_economics", "paired_in_kind_transfer"]
RuleClassification = Literal["external_in", "internal"]

_CONTRIBUTION_ASSUMPTIONS = frozenset({"provider_transfer_classification_unresolved"})
_IN_KIND_ASSUMPTIONS = frozenset(
    {"provider_in_kind_requires_reconciliation", "provider_external_cash_amount_zero"}
)
_ACATS_IN = "external_asset_transfer_in"
_ACATS_OUT = "external_asset_transfer_out"
_PAIR_WINDOW = timedelta(days=5)
_QUANTITY_TOLERANCE = Decimal("0.000001")


@dataclass(frozen=True)
class RuleResolution:
    source_event_id: str
    rule: RuleCode
    classification: RuleClassification
    record_transaction_id: str
    event_date: date


def _direction(transaction: InvestmentTransaction) -> Literal["in", "out"] | None:
    subtype = (transaction.subtype or "").lower().strip()
    name = (transaction.name or "").lower()
    if subtype == _ACATS_OUT or "outgoing" in name or "transfer out" in name:
        return "out"
    if subtype == _ACATS_IN or "incoming" in name or "transfer in" in name:
        return "in"
    return None


def _has_opposite_leg(
    session: Session, transaction: InvestmentTransaction, universe: frozenset[int]
) -> bool:
    direction = _direction(transaction)
    if direction is None or transaction.security_id is None:
        return False
    quantity = abs(Decimal(transaction.quantity or 0))
    if quantity == 0:
        return False
    opposite_subtype = _ACATS_IN if direction == "out" else _ACATS_OUT
    others = universe - {transaction.account_id}
    if not others:
        return False
    candidates = session.scalars(
        select(InvestmentTransaction)
        .where(InvestmentTransaction.account_id.in_(others))
        .where(InvestmentTransaction.security_id == transaction.security_id)
        .where(InvestmentTransaction.subtype == opposite_subtype)
        .where(InvestmentTransaction.date >= transaction.date - _PAIR_WINDOW)
        .where(InvestmentTransaction.date <= transaction.date + _PAIR_WINDOW)
    ).all()
    return any(
        abs(abs(Decimal(candidate.quantity or 0)) - quantity) <= _QUANTITY_TOLERANCE
        for candidate in candidates
    )


def resolve(
    session: Session,
    event: CashFlowSourceEvent,
    attestation: CashFlowSourceAttestation,
    decision: CashFlowReconciliationDecision,
    universe: frozenset[int],
) -> RuleResolution | None:
    """Return the deterministic resolution for one current unresolved decision."""
    if decision.resolution_kind != "unresolved" or decision.decision_authority != "provider":
        return None
    if decision.superseded_at is not None or not event.source_record_id:
        return None
    transaction = session.get(InvestmentTransaction, event.source_record_id)
    if transaction is None or transaction.account_id != attestation.account_id:
        return None
    amount = Decimal(transaction.amount or 0)
    quantity = Decimal(transaction.quantity or 0)
    source_amount = Decimal(event.source_amount)

    def found(rule: RuleCode, classification: RuleClassification) -> RuleResolution:
        return RuleResolution(
            source_event_id=event.source_event_id,
            rule=rule,
            classification=classification,
            record_transaction_id=transaction.plaid_investment_transaction_id,
            event_date=(
                event.activity_date
                or event.process_date
                or event.settlement_date
                or transaction.date
            ),
        )

    if amount == 0 and quantity == 0 and source_amount == 0:
        return found("zero_economics", "internal")
    if (
        decision.assumption_code in _CONTRIBUTION_ASSUMPTIONS
        and (transaction.name or "").strip().lower() == "contribution"
        and amount > 0
        and source_amount > 0
    ):
        return found("contribution_inflow", "external_in")
    if (
        decision.assumption_code in _IN_KIND_ASSUMPTIONS
        and amount == 0
        and quantity != 0
        and _has_opposite_leg(session, transaction, universe)
    ):
        return found("paired_in_kind_transfer", "internal")
    return None


def resolve_events(
    session: Session,
    rows: Iterable[
        tuple[CashFlowSourceEvent, CashFlowSourceAttestation, CashFlowReconciliationDecision]
    ],
    universe: frozenset[int],
) -> dict[str, RuleResolution]:
    """Resolve each eligible (event, attestation, current decision) triple."""
    resolved: dict[str, RuleResolution] = {}
    for event, attestation, decision in rows:
        resolution = resolve(session, event, attestation, decision, universe)
        if resolution is not None:
            resolved[event.source_event_id] = resolution
    return resolved
