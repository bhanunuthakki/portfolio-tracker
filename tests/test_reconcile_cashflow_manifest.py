from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from portfolio_tracker.jobs.reconcile_cashflow_manifest import (
    ManifestSource,
    ManifestValidationError,
    ReconciliationConflictError,
    apply_reconciliation_plan,
    build_reconciliation_plan,
    transaction_payload_sha256,
)
from portfolio_tracker.models import (
    Account,
    CashFlowSourceAttestation,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
    TransactionOverride,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _account(session, *, account_key: str = "provider-account-1") -> Account:
    item = Item(
        source="plaid",
        plaid_item_id="item-1",
        plaid_access_token_encrypted="encrypted",
        institution_name="Broker",
    )
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id,
        plaid_account_id=account_key,
        name="Taxable",
        type="investment",
        subtype="brokerage",
        currency="USD",
    )
    session.add(account)
    session.flush()
    security = Security(
        plaid_security_id="security-1",
        ticker="TEST",
        name="Test Security",
        type="equity",
        currency="USD",
    )
    session.add(security)
    session.flush()
    session.add(
        HoldingSnapshot(
            snapshot_date=date(2024, 9, 3),
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal("1"),
            institution_price=Decimal("100"),
            institution_value=Decimal("100"),
            cost_basis=Decimal("80"),
            currency="USD",
            origin="broker",
        )
    )
    session.commit()
    return account


def _manifest_source(
    tmp_path: Path,
    account: Account,
    events: list[dict[str, object]],
) -> ManifestSource:
    source_path = tmp_path / "statement.csv"
    max_ordinal = max(int(event["source_row_ordinal"]) for event in events)
    rows = [
        ["09/03/2024", "", "", "", "filler", "OTHER", "", "", "$0.00"] for _ in range(max_ordinal)
    ]
    for event in events:
        ordinal = int(event["source_row_ordinal"])
        event_date = datetime.strptime(str(event["date"]), "%Y-%m-%d").strftime("%m/%d/%Y")
        rows[ordinal - 1] = [
            event_date,
            "",
            "",
            "",
            "authoritative cash movement",
            str(event["source_code"]),
            "",
            "",
            f"${event['signed_external_amount']}",
        ]
    header = (
        "Activity Date,Process Date,Settle Date,Instrument,Description,"
        "Trans Code,Quantity,Price,Amount\n"
    )
    source_path.write_text(
        header + "".join(",".join(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    source_sha256 = _sha256(source_path.read_bytes())
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "account_id": account.account_id,
                "account_identity_sha256": _sha256(account.plaid_account_id.encode("utf-8")),
                "coverage_start": "2024-09-03",
                "coverage_end": "2026-07-31",
                "source_type": "brokerage_statement",
                "source_reference": f"private:brokerage_statement:{source_sha256}",
                "source_document_sha256": source_sha256,
                "captured_at": "2026-09-01T12:00:00+00:00",
                "methodology_version": "1",
                "gaps": [],
                "events": events,
            }
        ),
        encoding="utf-8",
    )
    return ManifestSource(manifest_path=manifest_path, source_path=source_path)


def _event(
    ordinal: int,
    event_date: str,
    signed_amount: str,
    classification: str,
    *,
    resolution: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "source_row_ordinal": ordinal,
        "date": event_date,
        "signed_external_amount": signed_amount,
        "classification": classification,
        "source_code": "ACH",
        "resolution": resolution or {"kind": "manual_transaction"},
    }


def _existing_resolution(
    transaction: InvestmentTransaction,
    *,
    expected_override: str | None = None,
) -> dict[str, object]:
    return {
        "kind": "existing_transaction",
        "transaction_id": transaction.plaid_investment_transaction_id,
        "expected_transaction_payload_sha256": transaction_payload_sha256(transaction),
        "expected_current_override": expected_override,
    }


def _transaction(
    account_id: int,
    transaction_id: str,
    event_date: date,
    amount: str,
    subtype: str,
) -> InvestmentTransaction:
    return InvestmentTransaction(
        plaid_investment_transaction_id=transaction_id,
        account_id=account_id,
        security_id=None,
        date=event_date,
        name="cash movement",
        quantity=Decimal(0),
        amount=Decimal(amount),
        price=None,
        fees=None,
        type="cash",
        subtype=subtype,
        currency="USD",
        origin="broker",
    )


def test_manifest_requires_stable_unique_source_row_ordinals(session, tmp_path):
    account = _account(session)
    source = _manifest_source(
        tmp_path,
        account,
        [
            _event(10, "2025-01-02", "100.00", "external_in"),
            _event(10, "2025-01-02", "100.00", "external_in"),
        ],
    )

    with pytest.raises(ManifestValidationError, match="source_row_ordinal"):
        build_reconciliation_plan(session, [source])


def test_manifest_verifies_source_hash_and_account_identity(session, tmp_path):
    account = _account(session)
    source = _manifest_source(
        tmp_path,
        account,
        [_event(10, "2025-01-02", "100.00", "external_in")],
    )
    source.source_path.write_bytes(b"changed")

    with pytest.raises(ManifestValidationError, match="source hash"):
        build_reconciliation_plan(session, [source])

    source = _manifest_source(
        tmp_path,
        account,
        [_event(10, "2025-01-02", "100.00", "external_in")],
    )
    payload = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    payload["account_identity_sha256"] = "0" * 64
    source.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="account mapping"):
        build_reconciliation_plan(session, [source])


def test_plan_classifies_each_event_once_without_writing(session, tmp_path):
    account = _account(session)
    existing = _transaction(
        account.account_id,
        "existing-in",
        date(2025, 1, 2),
        "100.00",
        "deposit",
    )
    needs_override = _transaction(
        account.account_id,
        "needs-override",
        date(2025, 1, 3),
        "25.00",
        "deposit",
    )
    session.add_all([existing, needs_override])
    session.commit()
    source = _manifest_source(
        tmp_path,
        account,
        [
            _event(
                10,
                "2025-01-02",
                "100.00",
                "external_in",
                resolution=_existing_resolution(existing),
            ),
            _event(
                11,
                "2025-01-03",
                "-25.00",
                "external_out",
                resolution=_existing_resolution(needs_override),
            ),
            _event(12, "2025-01-04", "50.00", "external_in"),
        ],
    )

    plan = build_reconciliation_plan(session, [source])

    assert [entry.status for entry in plan.entries] == [
        "existing_exact",
        "override_required",
        "missing_insert",
    ]
    assert plan.status_counts == {
        "existing_exact": 1,
        "override_required": 1,
        "missing_insert": 1,
        "conflict": 0,
        "excluded": 0,
    }
    assert plan.planned_mutation_count == 4
    assert session.scalar(select(func.count()).select_from(TransactionOverride)) == 0
    assert session.scalar(select(func.count()).select_from(CashFlowSourceAttestation)) == 0


def test_commit_is_atomic_and_second_run_has_zero_writes(session, tmp_path):
    account = _account(session)
    needs_override = _transaction(
        account.account_id,
        "needs-override",
        date(2025, 1, 3),
        "25.00",
        "deposit",
    )
    session.add(needs_override)
    session.commit()
    source = _manifest_source(
        tmp_path,
        account,
        [
            _event(
                11,
                "2025-01-03",
                "-25.00",
                "external_out",
                resolution=_existing_resolution(needs_override),
            ),
            _event(12, "2025-01-04", "50.00", "external_in"),
        ],
    )
    plan = build_reconciliation_plan(session, [source])

    result = apply_reconciliation_plan(
        session,
        plan,
        expected_plan_digest=plan.plan_digest,
        approved_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    )

    assert result.committed is True
    assert result.applied_mutation_count == 4
    assert session.get(TransactionOverride, "needs-override").classification == "external_out"
    assert session.scalar(select(func.count()).select_from(InvestmentTransaction)) == 2
    assert session.scalar(select(func.count()).select_from(TransactionOverride)) == 2
    assert session.scalar(select(func.count()).select_from(CashFlowSourceAttestation)) == 1

    rerun = build_reconciliation_plan(session, [source])
    assert rerun.status_counts["existing_exact"] == 2
    assert rerun.status_counts["conflict"] == 0
    assert rerun.planned_mutation_count == 0


def test_changed_explicit_match_fails_closed_without_partial_writes(session, tmp_path):
    account = _account(session)
    candidate = _transaction(
        account.account_id,
        "candidate-1",
        date(2025, 1, 2),
        "100.00",
        "deposit",
    )
    session.add(candidate)
    session.commit()
    source = _manifest_source(
        tmp_path,
        account,
        [
            _event(
                10,
                "2025-01-02",
                "100.00",
                "external_in",
                resolution=_existing_resolution(candidate),
            ),
            _event(11, "2025-01-04", "50.00", "external_in"),
        ],
    )
    candidate.name = "changed after explicit reconciliation"
    session.commit()
    plan = build_reconciliation_plan(session, [source])
    before = session.scalar(select(func.count()).select_from(InvestmentTransaction))

    with pytest.raises(ReconciliationConflictError):
        apply_reconciliation_plan(
            session,
            plan,
            expected_plan_digest=plan.plan_digest,
            approved_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        )

    assert session.scalar(select(func.count()).select_from(InvestmentTransaction)) == before
    assert session.scalar(select(func.count()).select_from(TransactionOverride)) == 0
    assert session.scalar(select(func.count()).select_from(CashFlowSourceAttestation)) == 0


def test_console_summary_contains_only_sanitized_counts_and_digest(session, tmp_path):
    account = _account(session)
    source = _manifest_source(
        tmp_path,
        account,
        [_event(10, "2025-01-02", "100.00", "external_in")],
    )
    summary = build_reconciliation_plan(session, [source]).console_summary()

    assert set(summary) == {
        "committed",
        "manifest_count",
        "source_event_count",
        "status_counts",
        "planned_mutation_count",
        "conflict_count",
        "plan_digest",
    }
    rendered = json.dumps(summary)
    assert "provider-account" not in rendered
    assert "100.00" not in rendered
    assert "2025-01-02" not in rendered


def test_private_preview_includes_net_cashflow_by_status(session, tmp_path):
    from portfolio_tracker.jobs.reconcile_cashflow_manifest import (
        write_private_preview_artifact,
    )

    account = _account(session)
    source = _manifest_source(
        tmp_path,
        account,
        [
            _event(10, "2025-01-02", "100.00", "external_in"),
            _event(11, "2025-01-03", "-25.00", "external_out"),
        ],
    )
    plan = build_reconciliation_plan(session, [source])
    destination = tmp_path / "private-preview.json"

    write_private_preview_artifact(plan, destination)

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["private_totals_by_status"]["missing_insert"] == {
        "event_count": 2,
        "net_external_cashflow": "75",
    }
    assert payload["private_totals_by_status"]["conflict"] == {
        "event_count": 0,
        "net_external_cashflow": "0",
    }
    assert destination.stat().st_mode & 0o777 == 0o600
