from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    CashFlowReconciliationRun,
    InvestmentTransaction,
    Item,
    ProviderTransactionCorrectionReceipt,
    Security,
)
from portfolio_tracker.plaid_client import PlaidInvestmentTransaction
from portfolio_tracker.provider_delivery import build_provider_delivery_metadata
from portfolio_tracker.services.provider_transaction_corrections import (
    ProviderTransactionCorrectionApproval,
    ProviderTransactionCorrectionError,
    apply_provider_transaction_corrections,
    preview_provider_transaction_corrections,
    write_provider_transaction_correction_preview,
)
from portfolio_tracker.services.provider_transaction_provenance import (
    ProviderAccountTransactionCapture,
    ProviderTransactionConflictError,
    persist_provider_account_attestation,
    persist_provider_account_attestation_with_correction_approvals,
)

_CAPTURED = datetime(2025, 2, 1, tzinfo=UTC)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backup(session: Session, path: Path) -> str:
    session.commit()
    driver = session.connection().connection.driver_connection
    with sqlite3.connect(path) as destination:
        driver.backup(destination)
        assert destination.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    return _sha256(path)


def _fixture(session: Session):
    item = Item(source="plaid", plaid_item_id="correction-item")
    session.add(item)
    session.flush()
    old_account = Account(
        item_id=item.item_id,
        plaid_account_id="old-account",
        name="Old",
        type="investment",
    )
    provider_account = Account(
        item_id=item.item_id,
        plaid_account_id="provider-account",
        name="Current",
        type="investment",
    )
    old_security = Security(plaid_security_id="old-security", ticker="OLD")
    provider_security = Security(plaid_security_id="provider-security", ticker="NEW")
    session.add_all([old_account, provider_account, old_security, provider_security])
    session.flush()
    transaction_id = "provider-record-1"
    stored = InvestmentTransaction(
        plaid_investment_transaction_id=transaction_id,
        account_id=old_account.account_id,
        security_id=old_security.security_id,
        date=date(2025, 1, 9),
        name="Old name",
        quantity=Decimal("1"),
        amount=Decimal("10"),
        price=Decimal("10"),
        fees=Decimal("1"),
        type="buy",
        subtype="buy",
        currency="USD",
    )
    delivered = PlaidInvestmentTransaction(
        plaid_investment_transaction_id=transaction_id,
        plaid_account_id=provider_account.plaid_account_id,
        plaid_security_id=provider_security.plaid_security_id,
        date=date(2025, 1, 10),
        name="Current name",
        quantity=Decimal("2"),
        amount=Decimal("200"),
        price=Decimal("100"),
        fees=Decimal("2"),
        type="cash",
        subtype="deposit",
        currency="EUR",
    )
    session.add(stored)
    session.flush()
    delivery = build_provider_delivery_metadata(
        provider="plaid",
        source_format="plaid_investment_transactions_api",
        parser_version="plaid_investment_tx.v1",
        requested_start_date=date(2025, 1, 1),
        requested_end_date=date(2025, 1, 31),
        page_count=1,
        provider_reported_total=1,
        record_ids=[transaction_id],
        normalized_records=[delivered.model_dump(mode="json")],
    )
    capture = ProviderAccountTransactionCapture(
        account_id=provider_account.account_id,
        provider_account_id=provider_account.plaid_account_id,
        coverage_start=date(2025, 1, 1),
        coverage_end=date(2025, 1, 31),
        delivery=delivery,
        transactions=(delivered,),
        security_ids_by_provider_id={
            provider_security.plaid_security_id: provider_security.security_id
        },
        captured_at=_CAPTURED,
    )
    return stored, delivered, capture


def _approval(
    plan,
    *,
    backup: Path,
    backup_sha256: str,
    preview: Path,
    preview_sha256: str,
) -> ProviderTransactionCorrectionApproval:
    return ProviderTransactionCorrectionApproval(
        expected_plan_digest=plan.plan_digest,
        approved_at=_CAPTURED + timedelta(hours=1),
        software_revision="test-revision",
        backup_path=backup,
        backup_sha256=backup_sha256,
        preview_path=preview,
        preview_sha256=preview_sha256,
    )


def test_preview_preserves_every_changed_economic_field_without_mutation(session: Session):
    stored, _, capture = _fixture(session)

    plan = preview_provider_transaction_corrections(session, capture)

    assert len(plan.corrections) == 1
    correction = plan.corrections[0]
    assert correction.provider_record_id == stored.plaid_investment_transaction_id
    assert correction.source_provider == "plaid"
    assert correction.source_locator_kind == "provider_record"
    assert {change.field for change in correction.changed_fields} == {
        "account_id",
        "security_id",
        "date",
        "type",
        "subtype",
        "amount",
        "quantity",
        "price",
        "fees",
        "currency",
        "name",
    }
    assert correction.before_payload_sha256 != correction.after_payload_sha256
    assert stored.amount == Decimal("10")
    assert session.scalar(select(CashFlowReconciliationRun)) is None


def test_ordinary_provider_persist_exposes_private_preview_but_never_mutates(session: Session):
    stored, _, capture = _fixture(session)

    with pytest.raises(ProviderTransactionConflictError) as exc_info:
        persist_provider_account_attestation(session, capture)

    assert exc_info.value.correction_plan is not None
    assert exc_info.value.correction_plan.corrections[0].provider_record_id == (
        stored.plaid_investment_transaction_id
    )
    assert stored.amount == Decimal("10")
    assert session.scalar(select(ProviderTransactionCorrectionReceipt)) is None


def test_apply_requires_verified_backup_then_persists_append_only_receipt(
    session: Session,
    tmp_path: Path,
):
    stored, delivered, capture = _fixture(session)
    backup = tmp_path / "verified-backup.db"
    backup_sha256 = _backup(session, backup)
    plan = preview_provider_transaction_corrections(session, capture)
    preview = tmp_path / "private-preview.json"
    preview_sha256 = write_provider_transaction_correction_preview(plan, preview)

    bad_approval = _approval(
        plan,
        backup=backup,
        backup_sha256="0" * 64,
        preview=preview,
        preview_sha256=preview_sha256,
    )
    with pytest.raises(ProviderTransactionCorrectionError, match="backup_digest_mismatch"):
        apply_provider_transaction_corrections(session, capture, plan, bad_approval)
    assert stored.amount == Decimal("10")
    assert session.scalar(select(CashFlowReconciliationRun)) is None
    assert session.scalar(select(ProviderTransactionCorrectionReceipt)) is None

    approval = _approval(
        plan,
        backup=backup,
        backup_sha256=backup_sha256,
        preview=preview,
        preview_sha256=preview_sha256,
    )
    rerun_capture = replace(capture, captured_at=_CAPTURED + timedelta(hours=2))
    result = persist_provider_account_attestation_with_correction_approvals(
        session,
        rerun_capture,
        {plan.plan_digest: approval},
    )
    session.flush()

    assert result.created is True
    assert stored.account_id == capture.account_id
    assert stored.date == delivered.date
    assert stored.amount == delivered.amount
    assert stored.quantity == delivered.quantity
    assert stored.price == delivered.price
    assert stored.fees == delivered.fees
    assert stored.type == delivered.type
    assert stored.subtype == delivered.subtype
    assert stored.currency == delivered.currency
    assert stored.name == delivered.name
    receipt = session.scalar(select(ProviderTransactionCorrectionReceipt))
    run = session.scalar(select(CashFlowReconciliationRun))
    assert receipt is not None and run is not None
    assert receipt.run_id == run.run_id
    assert receipt.provider_record_id == delivered.plaid_investment_transaction_id
    assert receipt.decision_authority == "owner_approved"
    assert json.loads(receipt.changed_fields_json) == [
        change.field for change in plan.corrections[0].changed_fields
    ]
    assert json.loads(receipt.before_payload_json) == plan.corrections[0].before_payload
    assert json.loads(receipt.after_payload_json) == plan.corrections[0].after_payload
    assert receipt.before_payload_sha256 == plan.corrections[0].before_payload_sha256
    assert receipt.after_payload_sha256 == plan.corrections[0].after_payload_sha256
    assert receipt.backup_sha256 == backup_sha256
    assert receipt.preview_sha256 == preview_sha256


def test_stale_preview_refuses_apply_without_overwriting_intervening_state(
    session: Session,
    tmp_path: Path,
):
    stored, _, capture = _fixture(session)
    backup = tmp_path / "verified-backup.db"
    backup_sha256 = _backup(session, backup)
    plan = preview_provider_transaction_corrections(session, capture)
    preview = tmp_path / "private-preview.json"
    preview_sha256 = write_provider_transaction_correction_preview(plan, preview)
    approval = _approval(
        plan,
        backup=backup,
        backup_sha256=backup_sha256,
        preview=preview,
        preview_sha256=preview_sha256,
    )
    stored.name = "Intervening correction"
    session.flush()

    with pytest.raises(ProviderTransactionCorrectionError, match="correction_plan_stale"):
        apply_provider_transaction_corrections(session, capture, plan, approval)

    assert stored.name == "Intervening correction"
    assert stored.amount == Decimal("10")
    assert session.scalar(select(CashFlowReconciliationRun)) is None
    assert session.scalar(select(ProviderTransactionCorrectionReceipt)) is None
