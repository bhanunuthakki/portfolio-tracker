from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    CashFlowReconciliationDecision,
    CashFlowSourceAttestation,
    CashFlowSourceAttestationEventLink,
    CashFlowSourceEvent,
    CashFlowSourceGap,
    InvestmentTransaction,
    Item,
)
from portfolio_tracker.plaid_client import PlaidInvestmentTransaction
from portfolio_tracker.provider_delivery import build_provider_delivery_metadata
from portfolio_tracker.services.cashflow_source_coverage import (
    assess_cashflow_source_coverage,
    canonical_decision_payload_sha256,
)
from portfolio_tracker.services.external_flow_ledger import build_external_flow_ledger
from portfolio_tracker.services.provider_transaction_provenance import (
    ProviderAccountTransactionCapture,
    ProviderHistoryGap,
    ProviderTransactionConflictError,
    persist_provider_account_attestation,
)

_START = date(2025, 1, 1)
_END = date(2025, 1, 31)
_CAPTURED = datetime(2025, 2, 1, tzinfo=UTC)


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _account(session: Session, suffix: str = "1") -> Account:
    item = Item(source="plaid", plaid_item_id=f"item-{suffix}")
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id,
        plaid_account_id=f"account-{suffix}",
        name="Brokerage",
        type="investment",
    )
    session.add(account)
    session.flush()
    return account


def _source_transaction(
    account: Account,
    *,
    tx_id: str = "provider-tx-1",
    tx_type: str = "cash",
    subtype: str | None = "deposit",
    amount: Decimal = Decimal("100"),
    quantity: Decimal = Decimal(0),
    tx_date: date = date(2025, 1, 10),
    name: str = "Provider activity",
) -> PlaidInvestmentTransaction:
    return PlaidInvestmentTransaction(
        plaid_investment_transaction_id=tx_id,
        plaid_account_id=account.plaid_account_id,
        date=tx_date,
        name=name,
        quantity=quantity,
        amount=amount,
        type=tx_type,
        subtype=subtype,
        currency="USD",
    )


def _store_transaction(
    session: Session,
    account: Account,
    transaction: PlaidInvestmentTransaction,
) -> None:
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id=transaction.plaid_investment_transaction_id,
            account_id=account.account_id,
            security_id=None,
            date=transaction.date,
            name=transaction.name,
            quantity=transaction.quantity,
            amount=transaction.amount,
            price=transaction.price,
            fees=transaction.fees,
            type=transaction.type,
            subtype=transaction.subtype,
            currency=transaction.currency,
        )
    )


def _capture(
    account: Account,
    transactions: tuple[PlaidInvestmentTransaction, ...],
    *,
    captured_at: datetime = _CAPTURED,
    provider_history_gaps: tuple[ProviderHistoryGap, ...] = (),
    coverage_start: date = _START,
    coverage_end: date = _END,
    broker_archive_coverage_basis: Literal["provider_explicit_full_range"] | None = (
        "provider_explicit_full_range"
    ),
) -> ProviderAccountTransactionCapture:
    delivery = build_provider_delivery_metadata(
        provider="plaid",
        source_format="plaid_investment_transactions_api",
        parser_version="plaid_investment_tx.v1",
        requested_start_date=coverage_start,
        requested_end_date=coverage_end,
        page_count=1,
        provider_reported_total=len(transactions),
        record_ids=[tx.plaid_investment_transaction_id for tx in transactions],
        normalized_records=[tx.model_dump(mode="json") for tx in transactions],
    )
    return ProviderAccountTransactionCapture(
        account_id=account.account_id,
        provider_account_id=account.plaid_account_id,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        delivery=delivery,
        transactions=transactions,
        security_ids_by_provider_id={},
        captured_at=captured_at,
        provider_history_gaps=provider_history_gaps,
        broker_archive_coverage_basis=broker_archive_coverage_basis,
    )


def _supersede_provider_unresolved_with_owner_resolution(
    session: Session,
    event: CashFlowSourceEvent,
    original: CashFlowReconciliationDecision,
    transaction: PlaidInvestmentTransaction,
) -> CashFlowReconciliationDecision:
    approved_at = _CAPTURED + timedelta(hours=1)
    replacement = CashFlowReconciliationDecision(
        decision_key="0" * 64,
        source_event_id=event.source_event_id,
        target_transaction_id=transaction.plaid_investment_transaction_id,
        resolution_kind="provider_exact",
        classification="external_in",
        signed_external_amount=abs(transaction.amount),
        effective_date=transaction.date,
        effective_date_basis="owner_resolved",
        effective_timezone="America/New_York",
        decision_authority="owner_approved",
        confidence="exact",
        assumption_code="corroborating_evidence_resolves_provider_unresolved",
        methodology_version="2",
        decision_payload_sha256="0" * 64,
        approved_at=approved_at,
        superseded_at=None,
        superseded_by_decision_key=None,
    )
    replacement.decision_payload_sha256 = canonical_decision_payload_sha256(replacement)
    replacement.decision_key = _digest(
        {
            "identity_version": "cashflow_reconciliation_decision.v1",
            "source_event_id": replacement.source_event_id,
            "decision_payload_sha256": replacement.decision_payload_sha256,
        }
    )
    original.superseded_at = approved_at
    original.superseded_by_decision_key = replacement.decision_key
    session.add(replacement)
    session.flush()
    return replacement


def test_provider_deposit_persists_certifying_event_and_approved_decision(session: Session):
    account = _account(session)
    transaction = _source_transaction(account)
    _store_transaction(session, account, transaction)

    result = persist_provider_account_attestation(session, _capture(account, (transaction,)))
    session.flush()

    assert result.created is True
    assert result.provider_attestation_is_certifying is True
    attestation = session.scalar(select(CashFlowSourceAttestation))
    event = session.scalar(select(CashFlowSourceEvent))
    decision = session.scalar(select(CashFlowReconciliationDecision))
    assert attestation is not None
    assert event is not None
    assert decision is not None
    assert attestation.source_type == "provider_api"
    assert event.source_locator_kind == "provider_record"
    assert event.source_record_id == transaction.plaid_investment_transaction_id
    assert decision.target_transaction_id == transaction.plaid_investment_transaction_id
    assert decision.classification == "external_in"
    assert decision.signed_external_amount == Decimal(100)
    assert decision.effective_date_basis == "provider_posting"
    assert decision.approved_at is not None

    coverage = assess_cashflow_source_coverage(
        session,
        _START,
        _END,
        account_ids=frozenset({account.account_id}),
    )
    assert coverage.is_complete is True
    assert coverage.attestations[0].validation_reason_codes == ()


def test_count_verified_delivery_does_not_assert_broker_archive_coverage(session: Session):
    account = _account(session)
    transaction = _source_transaction(account)
    _store_transaction(session, account, transaction)

    persist_provider_account_attestation(
        session,
        _capture(account, (transaction,), broker_archive_coverage_basis=None),
    )
    session.flush()

    attestation = session.scalar(select(CashFlowSourceAttestation))
    assert attestation is not None
    assert attestation.broker_archive_coverage == "unasserted"
    assert "broker_archive_coverage=unasserted" in attestation.source_reference
    coverage = assess_cashflow_source_coverage(
        session,
        _START,
        _END,
        account_ids=frozenset({account.account_id}),
    )
    assert coverage.is_complete is True
    assert coverage.broker_archive_is_complete is False
    assert coverage.broker_archive_status == "unasserted"


def test_overlapping_provider_windows_reuse_stable_event_and_decision_lineage(
    session: Session,
):
    account = _account(session)
    transaction = _source_transaction(account, tx_date=date(2025, 1, 15))
    _store_transaction(session, account, transaction)

    first = persist_provider_account_attestation(
        session,
        _capture(
            account,
            (transaction,),
            coverage_start=date(2025, 1, 1),
            coverage_end=date(2025, 1, 20),
        ),
    )
    session.flush()
    first_event_id = session.scalar(select(CashFlowSourceEvent.source_event_id))
    first_decision_key = session.scalar(select(CashFlowReconciliationDecision.decision_key))
    first_ledger = build_external_flow_ledger(
        session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        account_ids=frozenset({account.account_id}),
    )
    assert first_ledger.issues == ()
    assert len(first_ledger.entries) == 1

    second = persist_provider_account_attestation(
        session,
        _capture(
            account,
            (transaction,),
            captured_at=_CAPTURED + timedelta(days=1),
            coverage_start=date(2025, 1, 10),
            coverage_end=date(2025, 1, 31),
        ),
    )
    session.flush()

    assert first.attestation_key != second.attestation_key
    assert session.scalar(select(CashFlowSourceEvent.source_event_id)) == first_event_id
    assert session.scalar(select(CashFlowReconciliationDecision.decision_key)) == first_decision_key
    assert len(tuple(session.scalars(select(CashFlowSourceAttestation)))) == 2
    assert len(tuple(session.scalars(select(CashFlowSourceEvent)))) == 1
    assert len(tuple(session.scalars(select(CashFlowReconciliationDecision)))) == 1
    assert len(tuple(session.scalars(select(CashFlowSourceAttestationEventLink)))) == 2
    ledger = build_external_flow_ledger(
        session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        account_ids=frozenset({account.account_id}),
    )
    assert ledger.issues == ()
    assert len(ledger.entries) == 1
    assert ledger.entries[0].source_event_ids == (first_event_id,)
    assert ledger.entries[0].flow_id == first_ledger.entries[0].flow_id
    assert ledger.entries[0].active_decision_keys == first_ledger.entries[0].active_decision_keys
    assert (
        ledger.entries[0].source_attestation_keys == first_ledger.entries[0].source_attestation_keys
    )


@pytest.mark.parametrize(
    ("tx_type", "subtype"),
    [
        ("transfer", "transfer"),
        ("cash", "external_asset_transfer_in"),
        ("broker_specific_event", "broker_specific_event"),
    ],
)
def test_ambiguous_in_kind_and_unknown_events_create_localized_gap(
    session: Session, tx_type: str, subtype: str
):
    account = _account(session, tx_type)
    transaction = _source_transaction(
        account,
        tx_type=tx_type,
        subtype=subtype,
        amount=Decimal(0),
        quantity=Decimal(5),
    )
    _store_transaction(session, account, transaction)

    result = persist_provider_account_attestation(session, _capture(account, (transaction,)))
    session.flush()

    assert result.provider_attestation_is_certifying is False
    gap = session.scalar(select(CashFlowSourceGap))
    decision = session.scalar(select(CashFlowReconciliationDecision))
    assert gap is not None
    assert gap.gap_start == transaction.date
    assert gap.gap_end == transaction.date
    assert gap.reason_code == "unresolved_classification"
    assert decision is not None
    assert decision.resolution_kind == "unresolved"
    assert decision.target_transaction_id is None

    coverage = assess_cashflow_source_coverage(
        session,
        _START,
        _END,
        account_ids=frozenset({account.account_id}),
    )
    assert coverage.status == "partial"
    assert coverage.accounts[0].uncovered_ranges == ((transaction.date, transaction.date),)
    assert coverage.attestations[0].validation_reason_codes == ()


def test_statement_attestation_can_cover_provider_gap(session: Session):
    account = _account(session)
    transaction = _source_transaction(
        account,
        tx_type="transfer",
        subtype="transfer",
        amount=Decimal(0),
        quantity=Decimal(5),
    )
    _store_transaction(session, account, transaction)
    persist_provider_account_attestation(session, _capture(account, (transaction,)))
    session.flush()

    empty_event_set = _digest([])
    session.add(
        CashFlowSourceAttestation(
            attestation_key="statement-cover-gap",
            account_id=account.account_id,
            coverage_start=transaction.date,
            coverage_end=transaction.date,
            source_type="brokerage_statement",
            source_reference="private:brokerage_statement:test",
            source_sha256="a" * 64,
            captured_at=_CAPTURED,
            approved_at=_CAPTURED,
            methodology_version="1",
            account_identity_sha256="b" * 64,
            account_mapping_basis="statement_account_identifier",
            account_mapping_confidence="exact",
            source_format="robinhood_activity_csv",
            parser_version="robinhood_activity_csv.v4",
            source_timezone="America/New_York",
            source_row_count=0,
            cashflow_candidate_count=0,
            source_event_set_sha256=empty_event_set,
            manifest_sha256="c" * 64,
        )
    )
    session.flush()

    coverage = assess_cashflow_source_coverage(
        session,
        _START,
        _END,
        account_ids=frozenset({account.account_id}),
    )
    assert coverage.is_complete is True


def test_provider_attestation_retry_is_idempotent(session: Session):
    account = _account(session)
    transaction = _source_transaction(account)
    _store_transaction(session, account, transaction)
    first = persist_provider_account_attestation(session, _capture(account, (transaction,)))
    session.flush()

    second = persist_provider_account_attestation(
        session,
        _capture(account, (transaction,), captured_at=_CAPTURED + timedelta(hours=1)),
    )
    session.flush()

    assert first.attestation_key == second.attestation_key
    assert second.created is False
    assert len(tuple(session.scalars(select(CashFlowSourceAttestation)))) == 1
    assert len(tuple(session.scalars(select(CashFlowSourceEvent)))) == 1
    assert len(tuple(session.scalars(select(CashFlowReconciliationDecision)))) == 1


def test_exact_provider_recapture_preserves_schema_v4_owner_resolution(session: Session):
    account = _account(session, "owner-resolution-retry")
    transaction = _source_transaction(
        account,
        tx_id="owner-resolved-provider-tx",
        subtype="provider_specific_cash",
        amount=Decimal("100"),
    )
    _store_transaction(session, account, transaction)
    first = persist_provider_account_attestation(session, _capture(account, (transaction,)))
    session.flush()
    event = session.scalar(select(CashFlowSourceEvent))
    original = session.scalar(
        select(CashFlowReconciliationDecision).where(
            CashFlowReconciliationDecision.superseded_at.is_(None)
        )
    )
    assert event is not None and original is not None
    assert original.resolution_kind == "unresolved"
    replacement = _supersede_provider_unresolved_with_owner_resolution(
        session, event, original, transaction
    )

    exact_retry = persist_provider_account_attestation(
        session,
        _capture(account, (transaction,), captured_at=_CAPTURED + timedelta(hours=2)),
    )
    overlapping_retry = persist_provider_account_attestation(
        session,
        _capture(
            account,
            (transaction,),
            captured_at=_CAPTURED + timedelta(hours=3),
            coverage_start=date(2025, 1, 5),
            coverage_end=date(2025, 2, 5),
        ),
    )
    session.flush()

    assert exact_retry.attestation_key == first.attestation_key
    assert exact_retry.created is False
    assert overlapping_retry.created is True
    current = session.scalar(
        select(CashFlowReconciliationDecision).where(
            CashFlowReconciliationDecision.superseded_at.is_(None)
        )
    )
    assert current is not None
    assert current.decision_key == replacement.decision_key
    assert len(tuple(session.scalars(select(CashFlowReconciliationDecision)))) == 2
    assert len(tuple(session.scalars(select(CashFlowSourceAttestationEventLink)))) == 2


def test_provider_recapture_rejects_broken_owner_supersession_lineage(session: Session):
    account = _account(session, "owner-resolution-drift")
    transaction = _source_transaction(
        account,
        tx_id="owner-resolved-provider-drift",
        subtype="provider_specific_cash",
        amount=Decimal("100"),
    )
    _store_transaction(session, account, transaction)
    persist_provider_account_attestation(session, _capture(account, (transaction,)))
    session.flush()
    event = session.scalar(select(CashFlowSourceEvent))
    original = session.scalar(
        select(CashFlowReconciliationDecision).where(
            CashFlowReconciliationDecision.superseded_at.is_(None)
        )
    )
    assert event is not None and original is not None
    replacement = _supersede_provider_unresolved_with_owner_resolution(
        session, event, original, transaction
    )
    assert replacement.approved_at is not None
    original.superseded_at = replacement.approved_at + timedelta(seconds=1)
    session.flush()

    with pytest.raises(
        ProviderTransactionConflictError,
        match="stored provider attestation has drifted",
    ):
        persist_provider_account_attestation(
            session,
            _capture(account, (transaction,), captured_at=_CAPTURED + timedelta(hours=2)),
        )


def test_provider_disclosed_history_gap_prevents_source_coverage_certification(
    session: Session,
):
    account = _account(session)
    gap = ProviderHistoryGap(_START, date(2025, 1, 5))

    result = persist_provider_account_attestation(
        session,
        _capture(account, (), provider_history_gaps=(gap,)),
    )
    session.flush()

    assert result.provider_response_has_no_known_gaps is False
    assert result.provider_history_gap_count == 1
    stored_gap = session.scalar(select(CashFlowSourceGap))
    assert stored_gap is not None
    assert (stored_gap.gap_start, stored_gap.gap_end, stored_gap.reason_code) == (
        gap.start,
        gap.end,
        "provider_history_unavailable",
    )
    coverage = assess_cashflow_source_coverage(
        session,
        _START,
        _END,
        account_ids=frozenset({account.account_id}),
    )
    assert coverage.is_complete is False
    # Opening-day flow coverage begins on start + 1, so the uncovered slice
    # starts there even though the provider gap includes the boundary date.
    assert coverage.accounts[0].uncovered_ranges == (
        (_START + timedelta(days=1), date(2025, 1, 5)),
    )


def test_provider_name_rule_prevents_dividend_from_becoming_withdrawal(session: Session):
    account = _account(session)
    transaction = _source_transaction(
        account,
        subtype="withdrawal",
        amount=Decimal("25"),
        name="Cash dividend payment",
    )
    _store_transaction(session, account, transaction)

    persist_provider_account_attestation(session, _capture(account, (transaction,)))
    session.flush()

    decision = session.scalar(select(CashFlowReconciliationDecision))
    assert decision is not None
    assert decision.resolution_kind == "internal"
    assert decision.classification == "internal"
    assert decision.signed_external_amount == 0
    assert decision.confidence == "high"
    assert decision.assumption_code == "provider_name_rule_internal"


def test_zero_external_cash_amount_is_explicitly_unresolved(session: Session):
    account = _account(session)
    transaction = _source_transaction(account, amount=Decimal(0))
    _store_transaction(session, account, transaction)

    result = persist_provider_account_attestation(session, _capture(account, (transaction,)))
    session.flush()

    assert result.provider_response_has_no_known_gaps is False
    decision = session.scalar(select(CashFlowReconciliationDecision))
    assert decision is not None
    assert decision.resolution_kind == "unresolved"
    assert decision.assumption_code == "provider_external_cash_amount_zero"


def test_unrepresentable_provider_economics_fail_before_certification(session: Session):
    account = _account(session)
    transaction = _source_transaction(account, amount=Decimal("1.0000001"))
    _store_transaction(session, account, transaction)

    with pytest.raises(
        ProviderTransactionConflictError,
        match="normalized money storage precision",
    ):
        persist_provider_account_attestation(session, _capture(account, (transaction,)))

    assert session.scalar(select(CashFlowSourceAttestation)) is None


def test_new_complete_capture_supersedes_prior_same_provider_range(session: Session):
    account = _account(session)
    buy = _source_transaction(
        account,
        tx_id="buy-1",
        tx_type="buy",
        subtype="buy",
        amount=Decimal("-50"),
        quantity=Decimal(1),
    )
    _store_transaction(session, account, buy)
    first = persist_provider_account_attestation(session, _capture(account, (buy,)))
    session.flush()

    deposit = _source_transaction(account, tx_id="deposit-1")
    _store_transaction(session, account, deposit)
    second = persist_provider_account_attestation(
        session,
        _capture(account, (buy, deposit), captured_at=_CAPTURED + timedelta(hours=1)),
    )
    session.flush()

    prior = session.scalar(
        select(CashFlowSourceAttestation).where(
            CashFlowSourceAttestation.attestation_key == first.attestation_key
        )
    )
    current = session.scalar(
        select(CashFlowSourceAttestation).where(
            CashFlowSourceAttestation.attestation_key == second.attestation_key
        )
    )
    assert prior is not None and current is not None
    assert prior.superseded_by_attestation_id == current.attestation_id
    assert prior.superseded_at is not None
    assert current.superseded_at is None


def test_changed_existing_transaction_payload_fails_closed(session: Session):
    account = _account(session)
    stored = _source_transaction(account, amount=Decimal("100"))
    delivered = stored.model_copy(update={"amount": Decimal("200")})
    _store_transaction(session, account, stored)

    with pytest.raises(ProviderTransactionConflictError) as exc_info:
        persist_provider_account_attestation(session, _capture(account, (delivered,)))
    assert stored.plaid_investment_transaction_id not in str(exc_info.value)
    assert session.scalar(select(CashFlowSourceAttestation)) is None


def test_autoflush_false_materializes_transaction_before_provenance(engine):
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    with Session(engine, autoflush=False) as session:
        account = _account(session)
        transaction = _source_transaction(account)
        _store_transaction(session, account, transaction)

        persist_provider_account_attestation(session, _capture(account, (transaction,)))
        session.flush()

        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
