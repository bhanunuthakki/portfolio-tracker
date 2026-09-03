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
    CashFlowReconciliationDecision,
    CashFlowReconciliationRun,
    CashFlowReconciliationRunDecision,
    CashFlowReconciliationRunTransactionMutation,
    CashFlowSourceAttestation,
    CashFlowSourceEvent,
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
                "schema_version": "2",
                "account_id": account.account_id,
                "account_identity_sha256": _sha256(account.plaid_account_id.encode("utf-8")),
                "account_mapping_basis": "provider_account_id",
                "account_mapping_confidence": "exact",
                "coverage_start": "2024-09-03",
                "coverage_end": "2026-07-31",
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
