from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from portfolio_tracker.jobs import migrate_broker_to_snaptrade
from portfolio_tracker.models import (
    Account,
    AccountValuationObservation,
    CashFlowReconciliationDecision,
    CashFlowReconciliationRun,
    CashFlowReconciliationRunTransactionMutation,
    CashFlowSourceAttestation,
    CashFlowSourceEvent,
    InvestmentTransaction,
    Item,
    TransactionOverride,
)


def test_private_broker_migration_backup_includes_account_valuations(
    session, tmp_path, monkeypatch
):
    monkeypatch.setattr(migrate_broker_to_snaptrade, "_BACKUP_DIR", tmp_path)
    item = Item(source="plaid", plaid_item_id="item-1")
    account = Account(
        item=item,
        plaid_account_id="account-1",
        name="Brokerage",
        type="investment",
    )
    session.add(account)
    session.flush()
    session.add(
        AccountValuationObservation(
            observation_key="a" * 64,
            account_id=account.account_id,
            as_of_date=date(2026, 9, 3),
            total_value=Decimal("1000"),
            cash_value=Decimal("100"),
            currency="USD",
            source_kind="provider_api",
            source_provider="plaid",
            source_reference="investments/holdings/get.accounts[].balances",
            source_record_id="account-1",
            source_payload_sha256="b" * 64,
            normalization_version="1",
            fetched_at=datetime(2026, 9, 3, 22, tzinfo=UTC),
            is_complete=True,
            is_empty=False,
        )
    )
    session.flush()

    backup_path = migrate_broker_to_snaptrade._backup_plaid_data(
        session, item, [account], [account.account_id]
    )

    payload = json.loads(backup_path.read_text(encoding="utf-8"))
    valuations = payload["account_valuation_observations"]
    assert len(valuations) == 1
    assert valuations[0]["observation_key"] == "a" * 64
    assert valuations[0]["account_id"] == account.account_id
    assert valuations[0]["total_value"] == "1000.000000"
    assert valuations[0]["source_payload_sha256"] == "b" * 64
    assert backup_path.stat().st_mode & 0o777 == 0o600


def test_broker_migration_fails_closed_on_cashflow_provenance_dependencies(
    session, tmp_path, monkeypatch
):
    monkeypatch.setattr(migrate_broker_to_snaptrade, "_BACKUP_DIR", tmp_path)
    item = Item(source="plaid", plaid_item_id="item-1")
    account = Account(
        item=item,
        plaid_account_id="account-1",
        name="Brokerage",
        type="investment",
    )
    session.add(account)
    session.flush()
    transaction = InvestmentTransaction(
        plaid_investment_transaction_id="old-provider-transaction",
        account_id=account.account_id,
        date=date(2025, 1, 2),
        amount=Decimal("100"),
        quantity=Decimal("0"),
        type="cash",
        subtype="deposit",
    )
    session.add_all(
        [
            transaction,
            CashFlowSourceAttestation(
                attestation_key="a" * 64,
                account_id=account.account_id,
                coverage_start=date(2025, 1, 1),
                coverage_end=date(2025, 1, 31),
                source_type="brokerage_statement",
                source_reference="fixture statement",
                source_sha256="b" * 64,
                captured_at=datetime(2025, 2, 1, tzinfo=UTC),
                approved_at=datetime(2025, 2, 1, tzinfo=UTC),
                methodology_version="1",
            ),
            TransactionOverride(
                plaid_investment_transaction_id="old-provider-transaction",
                classification="external_in",
                notes="owner approved",
            ),
        ]
    )
    session.flush()

    blockers = migrate_broker_to_snaptrade._migration_provenance_blockers(
        session,
        account_ids=[account.account_id],
        transaction_ids=("old-provider-transaction",),
    )
    assert blockers.source_attestations == 1
    assert blockers.transaction_overrides == 1
    assert blockers.total == 2

    with pytest.raises(
        migrate_broker_to_snaptrade.MigrationProvenanceBlockerError,
        match="owner-approved mapping",
    ):
        migrate_broker_to_snaptrade._backup_plaid_data(
            session, item, [account], [account.account_id]
        )
    assert list(tmp_path.iterdir()) == []


def test_broker_migration_detects_decision_and_run_receipt_transaction_fks(session):
    item = Item(source="plaid", plaid_item_id="item-decisions")
    account = Account(
        item=item,
        plaid_account_id="account-decisions",
        name="Brokerage",
        type="investment",
    )
    session.add(account)
    session.flush()
    transaction_id = "old-provider-transaction-with-receipts"
    transaction = InvestmentTransaction(
        plaid_investment_transaction_id=transaction_id,
        account_id=account.account_id,
        date=date(2025, 1, 2),
        amount=Decimal("-100"),
        quantity=Decimal("0"),
        type="cash",
        subtype="deposit",
    )
    attestation = CashFlowSourceAttestation(
        attestation_key="c" * 64,
        account_id=account.account_id,
        coverage_start=date(2025, 1, 1),
        coverage_end=date(2025, 1, 31),
        source_type="provider_api",
        source_reference="provider record",
        source_sha256="d" * 64,
        captured_at=datetime(2025, 2, 1, tzinfo=UTC),
        approved_at=datetime(2025, 2, 1, tzinfo=UTC),
        methodology_version="1",
    )
    session.add_all([transaction, attestation])
    session.flush()
    event = CashFlowSourceEvent(
        source_event_id="e" * 64,
        attestation_id=attestation.attestation_id,
        source_locator_kind="provider_record",
        source_locator="provider transaction id",
        source_record_id=transaction_id,
        source_row_sha256="f" * 64,
        activity_date=date(2025, 1, 2),
        source_amount=Decimal("100"),
        source_amount_sign_basis="provider_reported",
        currency="USD",
        source_code="deposit",
    )
    run = CashFlowReconciliationRun(
        run_id="1" * 64,
        plan_digest="2" * 64,
        manifest_set_sha256="3" * 64,
        software_revision="test",
        backup_reference="test backup",
        preview_reference="test preview",
        affected_start=date(2025, 1, 2),
        affected_end=date(2025, 1, 2),
        affected_account_count=1,
        source_event_count=1,
        planned_mutation_count=1,
        applied_mutation_count=0,
        status="previewed",
    )
    session.add_all([event, run])
    session.flush()
    session.add_all(
        [
            CashFlowReconciliationDecision(
                decision_key="4" * 64,
                source_event_id=event.source_event_id,
                target_transaction_id=transaction_id,
                resolution_kind="provider_exact",
                classification="external_in",
                signed_external_amount=Decimal("100"),
                effective_date=date(2025, 1, 2),
                effective_date_basis="provider_posting",
                effective_timezone="UTC",
                decision_authority="provider",
                confidence="exact",
                methodology_version="1",
                decision_payload_sha256="5" * 64,
                approved_at=datetime(2025, 2, 1, tzinfo=UTC),
            ),
            CashFlowReconciliationRunTransactionMutation(
                run_id=run.run_id,
                target_transaction_id=transaction_id,
                mutation_kind="transaction_insert",
                before_payload_sha256=None,
                after_payload_sha256="6" * 64,
            ),
        ]
    )
    session.flush()

    blockers = migrate_broker_to_snaptrade._migration_provenance_blockers(
        session,
        account_ids=[account.account_id],
        transaction_ids=(transaction_id,),
    )

    assert blockers.source_attestations == 1
    assert blockers.reconciliation_decisions == 1
    assert blockers.reconciliation_run_mutations == 1
    assert blockers.total == 3


def test_broker_migration_dry_run_never_invokes_committing_snaptrade_sync(session, monkeypatch):
    plaid_item = Item(source="plaid", plaid_item_id="plaid-preview")
    snaptrade_item = Item(
        source="snaptrade",
        snaptrade_authorization_id="snap-preview",
    )
    session.add_all([plaid_item, snaptrade_item])
    session.flush()
    session.add_all(
        [
            Account(
                item_id=plaid_item.item_id,
                plaid_account_id="plaid-preview-account",
                name="Plaid preview",
                type="investment",
                mask="1234",
            ),
            Account(
                item_id=snaptrade_item.item_id,
                plaid_account_id="snap-preview-account",
                name="Snap preview",
                type="investment",
                mask="*****1234",
            ),
        ]
    )
    session.commit()
    monkeypatch.setattr(migrate_broker_to_snaptrade, "SessionLocal", lambda: session)

    from portfolio_tracker.api.routes import snaptrade as snaptrade_routes

    def forbidden_sync(*_args, **_kwargs):
        raise AssertionError("dry-run invoked committing SnapTrade sync")

    monkeypatch.setattr(snaptrade_routes, "sync", forbidden_sync)

    assert (
        migrate_broker_to_snaptrade.run(
            plaid_item_id=plaid_item.item_id,
            snaptrade_profile="primary",
            commit=False,
        )
        == 0
    )
