from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

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
    CashFlowReconciliationDecision,
    CashFlowReconciliationRun,
    CashFlowReconciliationRunDecision,
    CashFlowReconciliationRunTransactionMutation,
    CashFlowSourceAttestation,
    CashFlowSourceEvent,
    CashFlowSourceGap,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
    TransactionOverride,
)
from portfolio_tracker.services.cashflow_source_coverage import (
    assess_cashflow_source_coverage,
    canonical_decision_payload_sha256,
    canonical_source_event_set_sha256,
)
from portfolio_tracker.services.external_flow_ledger import build_external_flow_ledger


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
    *,
    requested_return_start: str = "2024-09-03",
    requested_return_end: str = "2026-09-03",
) -> ManifestSource:
    source_path = tmp_path / "statement.csv"
    max_ordinal = max(int(event["source_row_ordinal"]) for event in events)
    rows = [
        ["09/03/2024", "09/03/2024", "09/03/2024", "", "filler", "OTHER", "", "", "$0.00"]
        for _ in range(max_ordinal)
    ]
    for event in events:
        ordinal = int(event["source_row_ordinal"])
        event_date = datetime.strptime(str(event["date"]), "%Y-%m-%d").strftime("%m/%d/%Y")
        rows[ordinal - 1] = [
            event_date,
            event_date,
            event_date,
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
    headers = (
        "Activity Date",
        "Process Date",
        "Settle Date",
        "Instrument",
        "Description",
        "Trans Code",
        "Quantity",
        "Price",
        "Amount",
    )
    normalized_events: list[dict[str, object]] = []
    row_hashes: list[str] = []
    for event in events:
        normalized = dict(event)
        ordinal = int(event["source_row_ordinal"])
        row = rows[ordinal - 1]
        row_hash = _sha256(
            json.dumps(
                dict(zip(headers, (value.strip() for value in row), strict=True)),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
        )
        resolution = normalized["resolution"]
        assert isinstance(resolution, dict)
        disposition = (
            "provider_exact"
            if resolution["kind"] == "existing_transaction"
            else "statement_supplement"
        )
        normalized.update(
            {
                "source_row_sha256": row_hash,
                "activity_date": normalized["date"],
                "process_date": normalized["date"],
                "settlement_date": normalized["date"],
                "source_amount": normalized["signed_external_amount"],
                "source_amount_sign_basis": "statement_printed",
                "disposition": disposition,
                "ledger_effective_date": normalized["date"],
                "effective_date_basis": ("source_activity"),
                "effective_timezone": "America/New_York",
                "confidence": "high",
                "assumption_code": "statement_activity_date_used",
                "decision_authority": "owner_approved",
            }
        )
        normalized_events.append(normalized)
        row_hashes.append(row_hash)
    source_event_set_sha256 = _sha256(
        json.dumps(
            sorted(row_hashes),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "3",
                "account_id": account.account_id,
                "account_identity_sha256": _sha256(account.plaid_account_id.encode("utf-8")),
                "account_mapping_basis": "provider_account_id",
                "account_mapping_confidence": "exact",
                "coverage_start": "2024-09-03",
                "coverage_end": "2026-07-31",
                "requested_return_start": requested_return_start,
                "requested_return_end": requested_return_end,
                "source_type": "brokerage_statement",
                "source_reference": f"private:brokerage_statement:{source_sha256}",
                "source_document_sha256": source_sha256,
                "captured_at": "2026-09-01T12:00:00+00:00",
                "methodology_version": "1",
                "source_format": "robinhood_activity_csv",
                "parser_version": "robinhood_activity_csv.v2",
                "source_timezone": "America/New_York",
                "source_row_count": len(rows),
                "cashflow_candidate_count": len(normalized_events),
                "source_event_set_sha256": source_event_set_sha256,
                "gaps": [],
                "events": normalized_events,
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


def _provider_unresolved(
    session: Session,
    account: Account,
    transaction: InvestmentTransaction,
) -> tuple[CashFlowSourceEvent, CashFlowReconciliationDecision]:
    captured = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    attestation = CashFlowSourceAttestation(
        attestation_key="provider-unresolved-attestation",
        account_id=account.account_id,
        coverage_start=date(2024, 9, 3),
        coverage_end=date(2026, 7, 31),
        source_type="provider_api",
        source_reference="provider_api:snaptrade:count_verified_response",
        broker_archive_coverage="unasserted",
        source_sha256="a" * 64,
        captured_at=captured,
        approved_at=captured,
        methodology_version="provider-api-v1",
        account_identity_sha256="b" * 64,
        account_mapping_basis="provider_account_id",
        account_mapping_confidence="exact",
        source_format="snaptrade_account_activities_api",
        parser_version="snaptrade_account_activity.v3",
        source_timezone="provider-date",
        source_row_count=1,
        cashflow_candidate_count=1,
        source_event_set_sha256="c" * 64,
        manifest_sha256="d" * 64,
    )
    session.add(attestation)
    session.flush()
    event = CashFlowSourceEvent(
        source_event_id="e" * 64,
        attestation_id=attestation.attestation_id,
        source_record_id=transaction.plaid_investment_transaction_id,
        source_locator_kind="provider_record",
        source_locator=transaction.plaid_investment_transaction_id,
        source_row_sha256="f" * 64,
        activity_date=transaction.date,
        source_amount=transaction.amount,
        source_amount_sign_basis="provider_reported",
        currency="USD",
        source_code="cash",
    )
    decision = CashFlowReconciliationDecision(
        decision_key="1" * 64,
        source_event_id=event.source_event_id,
        target_transaction_id=None,
        resolution_kind="unresolved",
        classification=None,
        signed_external_amount=None,
        effective_date=None,
        effective_date_basis=None,
        effective_timezone=None,
        decision_authority="provider",
        confidence="provisional",
        assumption_code="provider_cash_classification_unresolved",
        methodology_version="provider-api-v1",
        decision_payload_sha256="2" * 64,
        approved_at=captured,
    )
    decision.decision_payload_sha256 = canonical_decision_payload_sha256(decision)
    attestation.source_event_set_sha256 = canonical_source_event_set_sha256((event,))
    session.add_all(
        [
            event,
            decision,
            CashFlowSourceGap(
                attestation_id=attestation.attestation_id,
                gap_start=transaction.date,
                gap_end=transaction.date,
                reason_code="unresolved_classification",
            ),
        ]
    )
    session.commit()
    return event, decision


def _add_provider_unresolved_resolution(
    source: ManifestSource,
    provider_event: CashFlowSourceEvent,
    provider_decision: CashFlowReconciliationDecision,
) -> None:
    payload = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "4"
    payload["parser_version"] = "robinhood_activity_csv.v4"
    payload["provider_unresolved_resolutions"] = [
        {
            "evidence_source_row_ordinal": 1,
            "provider_source_event_id": provider_event.source_event_id,
            "expected_provider_source_row_sha256": provider_event.source_row_sha256,
            "expected_current_decision_key": provider_decision.decision_key,
            "expected_current_decision_payload_sha256": (provider_decision.decision_payload_sha256),
        }
    ]
    source.manifest_path.write_text(json.dumps(payload), encoding="utf-8")


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


def test_manifest_set_requires_one_requested_return_window(session, tmp_path):
    account = _account(session)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _manifest_source(
        first_dir,
        account,
        [_event(1, "2025-01-02", "100.00", "external_in")],
    )
    second = _manifest_source(
        second_dir,
        account,
        [_event(1, "2025-01-03", "25.00", "external_in")],
        requested_return_start="2024-10-01",
    )

    with pytest.raises(ManifestValidationError, match="requested return window"):
        build_reconciliation_plan(session, [first, second])


def test_requested_return_window_changes_plan_digest(session, tmp_path):
    account = _account(session)
    source = _manifest_source(
        tmp_path,
        account,
        [_event(1, "2025-01-02", "100.00", "external_in")],
    )
    first = build_reconciliation_plan(session, [source])
    payload = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    payload["requested_return_start"] = "2024-10-01"
    source.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    second = build_reconciliation_plan(session, [source])

    assert first.requested_return_start == date(2024, 9, 3)
    assert second.requested_return_start == date(2024, 10, 1)
    assert first.plan_digest != second.plan_digest


def test_v3_manifest_independently_rejects_in_period_in_kind_transfer(session, tmp_path):
    account = _account(session)
    source = _manifest_source(
        tmp_path,
        account,
        [_event(1, "2025-01-02", "100.00", "external_in")],
        requested_return_end="2026-07-31",
    )
    with source.source_path.open("a", encoding="utf-8") as handle:
        handle.write("07/31/2026,07/31/2026,07/31/2026,SYN,in-kind transfer,ACATI,2,,--\n")
    payload = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    source_sha256 = _sha256(source.source_path.read_bytes())
    payload["source_document_sha256"] = source_sha256
    payload["source_reference"] = f"private:brokerage_statement:{source_sha256}"
    source.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="in-kind transfer"):
        build_reconciliation_plan(session, [source])


def test_v3_manifest_requires_gap_for_opening_boundary_in_kind_transfer(session, tmp_path):
    account = _account(session)
    source = _manifest_source(
        tmp_path,
        account,
        [_event(1, "2025-01-02", "100.00", "external_in")],
    )
    with source.source_path.open("a", encoding="utf-8") as handle:
        handle.write("09/03/2024,09/03/2024,09/03/2024,SYN,in-kind transfer,ACATI,2,,--\n")
    payload = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    source_sha256 = _sha256(source.source_path.read_bytes())
    payload["source_document_sha256"] = source_sha256
    payload["source_reference"] = f"private:brokerage_statement:{source_sha256}"
    payload["source_row_count"] = 2
    payload["gaps"] = [
        {
            "gap_start": "2024-09-03",
            "gap_end": "2024-09-03",
            "reason_code": "unreconciled_difference",
        }
    ]
    source.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    plan = build_reconciliation_plan(session, [source])
    assert plan.conflict_count == 0
    payload["gaps"] = []
    source.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="explicit source gap"):
        build_reconciliation_plan(session, [source])


def test_legacy_v2_manifest_retains_implicit_scope_compatibility(session, tmp_path):
    account = _account(session)
    source = _manifest_source(
        tmp_path,
        account,
        [_event(1, "2025-01-02", "100.00", "external_in")],
    )
    payload = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "2"
    payload.pop("requested_return_start")
    payload.pop("requested_return_end")
    source.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    plan = build_reconciliation_plan(session, [source])

    assert plan.requested_return_start is None
    assert plan.requested_return_end is None


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
    assert plan.planned_mutation_count == 11
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
        software_revision="a" * 40,
        backup_reference="private:backup:before",
        preview_reference="private:preview:approved",
    )

    assert result.committed is True
    assert result.applied_mutation_count == 9
    assert session.get(TransactionOverride, "needs-override").classification == "external_out"
    assert session.scalar(select(func.count()).select_from(InvestmentTransaction)) == 2
    assert session.scalar(select(func.count()).select_from(TransactionOverride)) == 2
    assert session.scalar(select(func.count()).select_from(CashFlowSourceAttestation)) == 1

    rerun = build_reconciliation_plan(session, [source])
    assert rerun.status_counts["existing_exact"] == 2
    assert rerun.status_counts["conflict"] == 0
    assert rerun.planned_mutation_count == 0
    before_receipts = session.scalar(select(func.count()).select_from(CashFlowReconciliationRun))
    before_memberships = session.scalar(
        select(func.count()).select_from(CashFlowReconciliationRunDecision)
    )
    rerun_result = apply_reconciliation_plan(
        session,
        rerun,
        expected_plan_digest=rerun.plan_digest,
        approved_at=datetime(2026, 9, 3, 12, 1, tzinfo=UTC),
        software_revision="a" * 40,
        backup_reference="private:backup:before",
        preview_reference="private:preview:approved",
    )
    assert rerun_result.applied_mutation_count == 0
    assert (
        session.scalar(select(func.count()).select_from(CashFlowReconciliationRun))
        == before_receipts
    )
    assert (
        session.scalar(select(func.count()).select_from(CashFlowReconciliationRunDecision))
        == before_memberships
    )


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
            software_revision="a" * 40,
            backup_reference="private:backup:before",
            preview_reference="private:preview:approved",
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
    assert payload["requested_return_start"] == "2024-09-03"
    assert payload["requested_return_end"] == "2026-09-03"
    assert destination.stat().st_mode & 0o777 == 0o600


def test_manifest_round_trips_source_dates_row_hash_and_decision_metadata(
    session,
    tmp_path,
):
    account = _account(session)
    source = _manifest_source(
        tmp_path,
        account,
        [_event(1, "2025-01-02", "100.00", "external_in")],
    )

    plan = build_reconciliation_plan(session, [source])
    parsed = plan.entries[0].event

    assert parsed.activity_date == date(2025, 1, 2)
    assert parsed.process_date == date(2025, 1, 2)
    assert parsed.settlement_date == date(2025, 1, 2)
    assert len(parsed.source_row_sha256) == 64
    assert parsed.ledger_effective_date == date(2025, 1, 2)
    assert parsed.effective_timezone == "America/New_York"
    assert parsed.assumption_code == "statement_activity_date_used"


def test_apply_persists_source_event_decision_and_run_receipt(session, tmp_path):
    account = _account(session)
    source = _manifest_source(
        tmp_path,
        account,
        [_event(1, "2025-01-02", "100.00", "external_in")],
    )
    plan = build_reconciliation_plan(session, [source])

    result = apply_reconciliation_plan(
        session,
        plan,
        expected_plan_digest=plan.plan_digest,
        approved_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        software_revision="a" * 40,
        backup_reference="private:backup:before",
        preview_reference="private:preview:approved",
    )

    source_event = session.scalar(select(CashFlowSourceEvent))
    decision = session.scalar(select(CashFlowReconciliationDecision))
    run = session.scalar(select(CashFlowReconciliationRun))
    assert result.committed is True
    assert source_event is not None
    assert source_event.source_row_ordinal == 1
    assert len(source_event.source_row_sha256) == 64
    assert source_event.process_date == date(2025, 1, 2)
    assert decision is not None
    assert decision.source_event_id == source_event.source_event_id
    assert decision.target_transaction_id is not None
    assert decision.resolution_kind == "statement_supplement"
    assert decision.effective_date == date(2025, 1, 2)
    assert decision.assumption_code == "statement_activity_date_used"
    assert run is not None
    assert run.plan_digest == plan.plan_digest
    assert run.backup_reference == "private:backup:before"
    assert run.preview_reference == "private:preview:approved"
    assert run.requested_return_start == date(2024, 9, 3)
    assert run.requested_return_end == date(2026, 9, 3)
    memberships = tuple(session.scalars(select(CashFlowReconciliationRunDecision)))
    transaction_mutations = tuple(
        session.scalars(select(CashFlowReconciliationRunTransactionMutation))
    )
    assert [(row.decision_key, row.membership_kind) for row in memberships] == [
        (decision.decision_key, "created")
    ]
    assert {row.mutation_kind for row in transaction_mutations} == {
        "transaction_insert",
        "override_insert",
    }
    assert all(len(row.after_payload_sha256) == 64 for row in transaction_mutations)


def test_apply_orders_source_event_before_decision_with_production_session(
    engine,
    tmp_path,
):
    with Session(engine, autoflush=False) as production_session:
        production_session.execute(text("PRAGMA foreign_keys=ON"))
        assert production_session.scalar(text("PRAGMA foreign_keys")) == 1
        account = _account(production_session)
        source = _manifest_source(
            tmp_path,
            account,
            [_event(1, "2025-01-02", "100.00", "external_in")],
        )
        plan = build_reconciliation_plan(production_session, [source])

        result = apply_reconciliation_plan(
            production_session,
            plan,
            expected_plan_digest=plan.plan_digest,
            approved_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
            software_revision="a" * 40,
            backup_reference="private:backup:before",
            preview_reference="private:preview:approved",
        )

        assert result.committed is True
        assert (
            production_session.scalar(select(func.count(CashFlowSourceEvent.source_event_id))) == 1
        )
        assert (
            production_session.scalar(
                select(func.count(CashFlowReconciliationDecision.decision_key))
            )
            == 1
        )
        assert production_session.execute(text("PRAGMA foreign_key_check")).all() == []


def test_statement_effective_date_cannot_depart_from_activity_date(session, tmp_path):
    account = _account(session)
    source = _manifest_source(
        tmp_path,
        account,
        [_event(1, "2025-01-02", "100.00", "external_in")],
    )
    payload = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    payload["events"][0]["ledger_effective_date"] = "2025-01-03"
    payload["events"][0]["effective_date_basis"] = "owner_resolved"
    payload["events"][0]["assumption_code"] = "owner_timing_choice"
    source.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="source activity date"):
        build_reconciliation_plan(session, [source])


def test_multiple_source_events_may_corroborate_one_provider_transaction(session, tmp_path):
    account = _account(session)
    provider = _transaction(
        account.account_id,
        "shared-provider-target",
        date(2025, 1, 2),
        "100.00",
        "deposit",
    )
    session.add(provider)
    session.commit()
    resolution = _existing_resolution(provider)
    source = _manifest_source(
        tmp_path,
        account,
        [
            _event(1, "2025-01-02", "100.00", "external_in", resolution=resolution),
            _event(2, "2025-01-02", "100.00", "external_in", resolution=resolution),
        ],
    )

    plan = build_reconciliation_plan(session, [source])
    assert plan.conflict_count == 0
    assert [entry.resolved_transaction_id for entry in plan.entries] == [
        provider.plaid_investment_transaction_id,
        provider.plaid_investment_transaction_id,
    ]


def test_later_provider_transaction_supersedes_statement_supplement(session, tmp_path):
    account = _account(session)
    source = _manifest_source(
        tmp_path,
        account,
        [_event(1, "2025-01-02", "100.00", "external_in")],
    )
    first_plan = build_reconciliation_plan(session, [source])
    apply_reconciliation_plan(
        session,
        first_plan,
        expected_plan_digest=first_plan.plan_digest,
        approved_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        software_revision="a" * 40,
        backup_reference="private:backup:before",
        preview_reference="private:preview:first",
    )
    original_decision = session.scalar(
        select(CashFlowReconciliationDecision).where(
            CashFlowReconciliationDecision.superseded_at.is_(None)
        )
    )
    assert original_decision is not None
    original_target = original_decision.target_transaction_id
    assert original_target is not None

    provider = _transaction(
        account.account_id,
        "late-provider-target",
        date(2025, 1, 5),
        "100.00",
        "deposit",
    )
    session.add(provider)
    session.commit()
    payload = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    payload["events"][0]["disposition"] = "provider_supersedes_supplement"
    payload["events"][0]["resolution"] = _existing_resolution(provider)
    source.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    replacement_plan = build_reconciliation_plan(session, [source])
    assert replacement_plan.conflict_count == 0
    assert replacement_plan.planned_mutation_count == 4
    apply_reconciliation_plan(
        session,
        replacement_plan,
        expected_plan_digest=replacement_plan.plan_digest,
        approved_at=datetime(2026, 9, 3, 12, 5, tzinfo=UTC),
        software_revision="b" * 40,
        backup_reference="private:backup:second",
        preview_reference="private:preview:second",
    )

    decisions = tuple(
        session.scalars(
            select(CashFlowReconciliationDecision).order_by(
                CashFlowReconciliationDecision.created_at,
                CashFlowReconciliationDecision.decision_key,
            )
        )
    )
    current = next(row for row in decisions if row.superseded_at is None)
    previous = next(row for row in decisions if row.superseded_at is not None)
    assert previous.superseded_by_decision_key == current.decision_key
    assert current.target_transaction_id == provider.plaid_investment_transaction_id
    assert session.get(TransactionOverride, original_target).classification == "internal"
    second_run = session.scalar(
        select(CashFlowReconciliationRun).where(
            CashFlowReconciliationRun.plan_digest == replacement_plan.plan_digest
        )
    )
    assert second_run is not None
    memberships = tuple(
        session.scalars(
            select(CashFlowReconciliationRunDecision).where(
                CashFlowReconciliationRunDecision.run_id == second_run.run_id
            )
        )
    )
    assert {row.membership_kind for row in memberships} == {"created", "superseded"}


def test_statement_evidence_supersedes_provider_unresolved_without_double_counting(
    session,
    tmp_path,
):
    account = _account(session)
    flow_date = date(2025, 1, 2)
    provider_transaction = _transaction(
        account.account_id,
        "ambiguous-provider-target",
        flow_date,
        "100.00",
        "provider_specific_cash",
    )
    session.add(provider_transaction)
    session.commit()
    provider_event, provider_decision = _provider_unresolved(session, account, provider_transaction)
    source = _manifest_source(
        tmp_path,
        account,
        [
            _event(
                1,
                flow_date.isoformat(),
                "100.00",
                "external_in",
                resolution=_existing_resolution(provider_transaction),
            )
        ],
    )
    _add_provider_unresolved_resolution(source, provider_event, provider_decision)

    plan = build_reconciliation_plan(session, [source])

    assert plan.conflict_count == 0
    assert len(plan.provider_unresolved_resolutions) == 1
    assert plan.provider_unresolved_resolutions[0].status == "supersession_required"
    apply_reconciliation_plan(
        session,
        plan,
        expected_plan_digest=plan.plan_digest,
        approved_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        software_revision="a" * 40,
        backup_reference="private:backup:before",
        preview_reference="private:preview:approved",
    )

    current_provider_decision = session.scalar(
        select(CashFlowReconciliationDecision).where(
            CashFlowReconciliationDecision.source_event_id == provider_event.source_event_id,
            CashFlowReconciliationDecision.superseded_at.is_(None),
        )
    )
    assert current_provider_decision is not None
    assert provider_decision.superseded_by_decision_key == current_provider_decision.decision_key
    assert current_provider_decision.decision_authority == "owner_approved"
    assert current_provider_decision.resolution_kind == "provider_exact"
    assert current_provider_decision.target_transaction_id == (
        provider_transaction.plaid_investment_transaction_id
    )
    assert current_provider_decision.effective_date_basis == "owner_resolved"

    ledger = build_external_flow_ledger(
        session,
        flow_date - timedelta(days=1),
        flow_date,
        account_ids=frozenset({account.account_id}),
    )
    assert ledger.issues == ()
    assert ledger.net_external_cashflow_in == Decimal("100.00")
    assert len(ledger.entries) == 1
    assert set(ledger.entries[0].source_event_ids) == {
        provider_event.source_event_id,
        plan.entries[0].source_event_id,
    }

    coverage = assess_cashflow_source_coverage(
        session,
        flow_date - timedelta(days=1),
        flow_date,
        account_ids=frozenset({account.account_id}),
    )
    assert coverage.is_complete is True
    applied_run = session.scalar(
        select(CashFlowReconciliationRun).where(
            CashFlowReconciliationRun.plan_digest == plan.plan_digest
        )
    )
    assert applied_run is not None
    memberships = tuple(
        session.scalars(
            select(CashFlowReconciliationRunDecision).where(
                CashFlowReconciliationRunDecision.run_id == applied_run.run_id
            )
        )
    )
    assert {row.membership_kind for row in memberships} == {"created", "superseded"}
    assert len(memberships) == 3

    rerun = build_reconciliation_plan(session, [source])
    assert rerun.conflict_count == 0
    assert rerun.provider_unresolved_resolutions[0].status == "existing_exact"
    assert rerun.planned_mutation_count == 0


def test_provider_unresolved_resolution_rejects_non_provider_current_decision(
    session,
    tmp_path,
):
    account = _account(session)
    provider_transaction = _transaction(
        account.account_id,
        "not-provider-owned",
        date(2025, 1, 2),
        "100.00",
        "provider_specific_cash",
    )
    session.add(provider_transaction)
    session.commit()
    provider_event, provider_decision = _provider_unresolved(session, account, provider_transaction)
    provider_decision.decision_authority = "owner_approved"
    provider_decision.decision_payload_sha256 = canonical_decision_payload_sha256(provider_decision)
    session.commit()
    source = _manifest_source(
        tmp_path,
        account,
        [
            _event(
                1,
                "2025-01-02",
                "100.00",
                "external_in",
                resolution=_existing_resolution(provider_transaction),
            )
        ],
    )
    _add_provider_unresolved_resolution(source, provider_event, provider_decision)

    plan = build_reconciliation_plan(session, [source])

    assert plan.conflict_count == 1
    assert plan.provider_unresolved_resolutions[0].reason_code == (
        "provider_unresolved_current_decision_not_provider_created"
    )
    assert session.scalar(select(func.count()).select_from(CashFlowReconciliationRun)) == 0


def test_provider_unresolved_resolution_rejects_conflicting_statement_evidence(
    session,
    tmp_path,
):
    account = _account(session)
    provider_transaction = _transaction(
        account.account_id,
        "conflicting-statement-target",
        date(2025, 1, 2),
        "100.00",
        "provider_specific_cash",
    )
    session.add(provider_transaction)
    session.commit()
    provider_event, provider_decision = _provider_unresolved(session, account, provider_transaction)
    source = _manifest_source(
        tmp_path,
        account,
        [
            _event(
                1,
                "2025-01-02",
                "100.00",
                "external_in",
                resolution=_existing_resolution(provider_transaction),
            ),
            _event(
                2,
                "2025-01-02",
                "-100.00",
                "external_out",
                resolution=_existing_resolution(provider_transaction),
            ),
        ],
    )
    _add_provider_unresolved_resolution(source, provider_event, provider_decision)

    plan = build_reconciliation_plan(session, [source])

    assert plan.conflict_count == 3
    assert all(
        entry.reason_code == "shared_transaction_classification_conflict" for entry in plan.entries
    )
    assert plan.provider_unresolved_resolutions[0].status == "conflict"
    assert plan.provider_unresolved_resolutions[0].reason_code == (
        "provider_resolution_evidence_target_unavailable"
    )
    assert session.scalar(select(func.count()).select_from(CashFlowReconciliationRun)) == 0


@pytest.mark.parametrize(
    ("field", "reason_code"),
    [
        (
            "expected_provider_source_row_sha256",
            "provider_unresolved_source_row_digest_mismatch",
        ),
        (
            "expected_current_decision_payload_sha256",
            "provider_unresolved_current_decision_digest_mismatch",
        ),
    ],
)
def test_provider_unresolved_resolution_is_bound_to_existing_digests(
    session,
    tmp_path,
    field,
    reason_code,
):
    account = _account(session)
    provider_transaction = _transaction(
        account.account_id,
        "digest-bound-provider-target",
        date(2025, 1, 2),
        "100.00",
        "provider_specific_cash",
    )
    session.add(provider_transaction)
    session.commit()
    provider_event, provider_decision = _provider_unresolved(session, account, provider_transaction)
    source = _manifest_source(
        tmp_path,
        account,
        [
            _event(
                1,
                "2025-01-02",
                "100.00",
                "external_in",
                resolution=_existing_resolution(provider_transaction),
            )
        ],
    )
    _add_provider_unresolved_resolution(source, provider_event, provider_decision)
    payload = json.loads(source.manifest_path.read_text(encoding="utf-8"))
    payload["provider_unresolved_resolutions"][0][field] = "0" * 64
    source.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    plan = build_reconciliation_plan(session, [source])

    assert plan.conflict_count == 1
    assert plan.provider_unresolved_resolutions[0].reason_code == reason_code
    assert session.scalar(select(func.count()).select_from(CashFlowReconciliationRun)) == 0
