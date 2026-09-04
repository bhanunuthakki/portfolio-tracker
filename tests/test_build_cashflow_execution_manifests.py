from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from portfolio_tracker.jobs.reconcile_cashflow_manifest import apply_reconciliation_plan
from portfolio_tracker.models import (
    Account,
    Base,
    CashFlowReconciliationDecision,
    CashFlowSourceAttestation,
    CashFlowSourceEvent,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
)
from portfolio_tracker.services.cashflow_source_coverage import (
    canonical_decision_payload_sha256,
)
from portfolio_tracker.services.external_flow_ledger import build_external_flow_ledger

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import build_cashflow_execution_manifests as builder  # noqa: E402

_RETURN_START = date(2025, 1, 1)
_RETURN_END = date(2025, 12, 31)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database(path: Path, events: list[dict[str, object]]) -> tuple[int, str]:
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0031')")
    raw_account_identity = "private-provider-account-id"
    with Session(engine) as session:
        item = Item(
            source="plaid",
            plaid_item_id="private-provider-item-id",
            plaid_access_token_encrypted="private-credential",
            institution_name="Private Broker",
            is_data_active=True,
        )
        session.add(item)
        session.flush()
        account = Account(
            item_id=item.item_id,
            plaid_account_id=raw_account_identity,
            name="Private Account Name",
            type="investment",
            subtype="brokerage",
            currency="USD",
        )
        security = Security(
            plaid_security_id="private-security-id",
            ticker="SYN",
            name="Synthetic",
            type="equity",
            currency="USD",
        )
        session.add_all([account, security])
        session.flush()
        session.add(
            HoldingSnapshot(
                snapshot_date=date(2025, 1, 1),
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_price=Decimal(100),
                institution_value=Decimal(100),
                cost_basis=Decimal(80),
                currency="USD",
                origin="broker",
            )
        )
        for index, event in enumerate(events[:66]):
            session.add(
                InvestmentTransaction(
                    plaid_investment_transaction_id=f"private-provider-transaction-{index}",
                    account_id=account.account_id,
                    security_id=None,
                    date=date.fromisoformat(str(event["date"])),
                    name="private transaction description",
                    quantity=Decimal(0),
                    amount=Decimal(str(event["signed_external_amount"])),
                    price=None,
                    fees=None,
                    type="cash",
                    subtype="deposit",
                    currency="USD",
                    origin="broker",
                )
            )
        if len(events) > 66:
            mismatch = events[66]
            session.add(
                InvestmentTransaction(
                    plaid_investment_transaction_id="private-provider-mismatch-id",
                    account_id=account.account_id,
                    security_id=None,
                    date=date.fromisoformat(str(mismatch["date"])),
                    name="private mismatch description",
                    quantity=Decimal(0),
                    amount=abs(Decimal(str(mismatch["signed_external_amount"]))),
                    price=None,
                    fees=None,
                    type="cash",
                    subtype="deposit",
                    currency="USD",
                    origin="broker",
                )
            )
        session.commit()
        account_id = account.account_id
    engine.dispose()
    return account_id, hashlib.sha256(raw_account_identity.encode()).hexdigest()


def _events(count: int = 68) -> list[dict[str, object]]:
    start = date(2025, 1, 2)
    events: list[dict[str, object]] = [
        {
            "source_row_ordinal": index + 1,
            "date": (start + timedelta(days=index)).isoformat(),
            "signed_external_amount": f"{index + 1}.00",
            "classification": "external_in",
            "source_code": "ACH",
        }
        for index in range(count)
    ]
    if count >= 67:
        events[66]["signed_external_amount"] = "-67.00"
        events[66]["classification"] = "external_out"
    return events


def _inputs(
    tmp_path: Path,
    events: list[dict[str, object]],
    *,
    duplicate_candidate: bool = False,
) -> tuple[Path, Path, Path]:
    database = tmp_path / "restored.db"
    account_id, fingerprint = _database(database, events)
    if duplicate_candidate:
        engine = create_engine(f"sqlite:///{database}", future=True)
        with Session(engine) as session:
            first = events[0]
            session.add(
                InvestmentTransaction(
                    plaid_investment_transaction_id="private-duplicate-id",
                    account_id=account_id,
                    security_id=None,
                    date=date.fromisoformat(str(first["date"])),
                    name="private duplicate",
                    quantity=Decimal(0),
                    amount=Decimal(str(first["signed_external_amount"])),
                    price=None,
                    fees=None,
                    type="cash",
                    subtype="deposit",
                    currency="USD",
                    origin="broker",
                )
            )
            session.commit()
        engine.dispose()

    csv_path = tmp_path / "private-source.csv"
    header = (
        "Activity Date,Process Date,Settle Date,Instrument,Description,"
        "Trans Code,Quantity,Price,Amount\n"
    )
    rows: list[str] = []
    for event in events:
        event_date = date.fromisoformat(str(event["date"]))
        rows.append(
            f"{event_date:%m/%d/%Y},{event_date:%m/%d/%Y},{event_date:%m/%d/%Y},,"
            f"private description,{event['source_code']},,,"
            f"${event['signed_external_amount']}\n"
        )
    csv_path.write_text(header + "".join(rows), encoding="utf-8")
    source_document_sha256 = _sha256(csv_path)
    account_mapping_basis = "provider_account_id"
    account_mapping_confidence = "exact"
    inventory = tmp_path / "private-inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "account_id": account_id,
                "account_identity_sha256": fingerprint,
                "account_mapping_basis": account_mapping_basis,
                "account_mapping_confidence": account_mapping_confidence,
                "account_mapping_evidence_sha256": builder._account_mapping_evidence_sha256(
                    account_identity_sha256=fingerprint,
                    account_mapping_basis=account_mapping_basis,
                    account_mapping_confidence=account_mapping_confidence,
                    source_document_sha256=source_document_sha256,
                ),
                "coverage_start": "2025-01-01",
                "coverage_end": "2025-12-31",
                "source_type": "brokerage_statement",
                "source_document_sha256": source_document_sha256,
                "captured_at": "2026-01-01T00:00:00+00:00",
                "events": events,
                "gaps": [],
            }
        ),
        encoding="utf-8",
    )
    return database, inventory, csv_path


def _build(
    database: Path,
    inventory: Path,
    csv_path: Path,
    output: Path,
    *,
    return_start: date = _RETURN_START,
    return_end: date = _RETURN_END,
) -> builder.BuildResult:
    return builder.build_execution_manifests(
        database,
        [inventory],
        [csv_path],
        output,
        requested_return_start=return_start,
        requested_return_end=return_end,
    )


def _refresh_source_mapping_evidence(payload: dict[str, object], csv_path: Path) -> None:
    payload["source_document_sha256"] = _sha256(csv_path)
    payload["account_mapping_evidence_sha256"] = builder._account_mapping_evidence_sha256(
        account_identity_sha256=str(payload["account_identity_sha256"]),
        account_mapping_basis=str(payload["account_mapping_basis"]),
        account_mapping_confidence=str(payload["account_mapping_confidence"]),
        source_document_sha256=str(payload["source_document_sha256"]),
    )


def _add_provider_unresolved(
    database: Path,
    *,
    source_event_id: str = "e" * 64,
    decision_key: str = "d" * 64,
) -> tuple[str, str, str]:
    engine = create_engine(f"sqlite:///{database}", future=True)
    with Session(engine) as session:
        transaction = session.get(
            InvestmentTransaction,
            "private-provider-transaction-0",
        )
        assert transaction is not None
        captured_at = datetime(2026, 1, 1, tzinfo=UTC)
        source_row_sha256 = hashlib.sha256(source_event_id.encode()).hexdigest()
        attestation = CashFlowSourceAttestation(
            attestation_key=hashlib.sha256(f"attestation:{source_event_id}".encode()).hexdigest(),
            account_id=transaction.account_id,
            coverage_start=transaction.date,
            coverage_end=transaction.date,
            source_type="provider_api",
            broker_archive_coverage="unasserted",
            source_reference=f"private:provider:{source_event_id}",
            source_sha256=hashlib.sha256(f"source:{source_event_id}".encode()).hexdigest(),
            captured_at=captured_at,
            approved_at=captured_at,
            methodology_version="provider-api-v1",
        )
        session.add(attestation)
        session.flush()
        provider_event = CashFlowSourceEvent(
            source_event_id=source_event_id,
            attestation_id=attestation.attestation_id,
            source_record_id=transaction.plaid_investment_transaction_id,
            source_locator_kind="provider_record",
            source_locator=transaction.plaid_investment_transaction_id,
            source_row_ordinal=None,
            source_page=None,
            source_line=None,
            source_row_sha256=source_row_sha256,
            activity_date=transaction.date,
            process_date=None,
            settlement_date=None,
            source_amount=transaction.amount,
            source_amount_sign_basis="provider_reported",
            currency=transaction.currency,
            source_code=transaction.type,
        )
        decision = CashFlowReconciliationDecision(
            decision_key=decision_key,
            source_event_id=source_event_id,
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
            decision_payload_sha256="0" * 64,
            approved_at=captured_at,
        )
        decision.decision_payload_sha256 = canonical_decision_payload_sha256(decision)
        session.add_all([provider_event, decision])
        session.commit()
        decision_payload_sha256 = decision.decision_payload_sha256
    engine.dispose()
    return source_row_sha256, decision_key, decision_payload_sha256


def _mark_provider_decision_resolved(database: Path) -> None:
    engine = create_engine(f"sqlite:///{database}", future=True)
    with Session(engine) as session:
        transaction = session.get(
            InvestmentTransaction,
            "private-provider-transaction-0",
        )
        decision = session.get(CashFlowReconciliationDecision, "d" * 64)
        assert transaction is not None
        assert decision is not None
        decision.target_transaction_id = transaction.plaid_investment_transaction_id
        decision.resolution_kind = "provider_exact"
        decision.classification = "external_in"
        decision.signed_external_amount = abs(transaction.amount)
        decision.effective_date = transaction.date
        decision.effective_date_basis = "provider_posting"
        decision.effective_timezone = "provider-date"
        decision.decision_authority = "provider"
        decision.confidence = "exact"
        decision.assumption_code = "provider_cash_deposit"
        decision.decision_payload_sha256 = canonical_decision_payload_sha256(decision)
        session.commit()
    engine.dispose()


def test_builds_68_explicit_one_to_one_resolutions_without_mutating_database(
    tmp_path: Path,
) -> None:
    events = _events()
    database, inventory, csv_path = _inputs(tmp_path, events)
    output = tmp_path / "private-execution-manifests"
    before = _sha256(database)

    result = _build(database, inventory, csv_path, output)

    assert _sha256(database) == before
    assert result.event_count == 68
    assert result.status_counts == {
        "provider_exact": 67,
        "statement_supplement": 1,
        "internal": 0,
        "excluded": 0,
        "unresolved": 0,
    }
    assert result.conflict_count == 0
    manifests = list(output.glob("*.json"))
    assert len(manifests) == 1
    assert os.stat(manifests[0]).st_mode & 0o777 == 0o600
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["schema_version"] == "4"
    assert payload["provider_unresolved_resolutions"] == []
    assert payload["requested_return_start"] == _RETURN_START.isoformat()
    assert payload["requested_return_end"] == _RETURN_END.isoformat()
    assert payload["coverage_start"] == "2025-01-01"
    assert payload["coverage_end"] == "2025-12-31"
    assert len(payload["events"]) == 68
    assert all("resolution" in event for event in payload["events"])
    assert payload["events"][67]["disposition"] == "statement_supplement"
    assert payload["events"][67]["resolution"] == {"kind": "manual_transaction"}
    assert payload["events"][0]["resolution"]["transaction_identity_sha256"]
    serialized = manifests[0].read_text(encoding="utf-8")
    assert "private-provider-transaction" not in serialized
    assert "Private Account Name" not in serialized
    assert "private-credential" not in serialized


def test_emits_digest_bound_provider_unresolved_resolution_deterministically(
    tmp_path: Path,
) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events(1))
    source_row_sha256, decision_key, decision_payload_sha256 = _add_provider_unresolved(database)
    before = _sha256(database)

    first_output = tmp_path / "first-output"
    second_output = tmp_path / "second-output"
    first_result = _build(database, inventory, csv_path, first_output)
    second_result = _build(database, inventory, csv_path, second_output)

    assert _sha256(database) == before
    assert first_result.output_digest == second_result.output_digest
    first = json.loads(next(first_output.glob("*.json")).read_text(encoding="utf-8"))
    second = json.loads(next(second_output.glob("*.json")).read_text(encoding="utf-8"))
    assert first == second
    assert first["schema_version"] == "4"
    assert first["provider_unresolved_resolutions"] == [
        {
            "evidence_source_row_ordinal": 1,
            "provider_source_event_id": "e" * 64,
            "expected_provider_source_row_sha256": source_row_sha256,
            "expected_current_decision_key": decision_key,
            "expected_current_decision_payload_sha256": decision_payload_sha256,
        }
    ]


def test_ambiguous_provider_source_event_lineage_fails_closed(tmp_path: Path) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events(1))
    _add_provider_unresolved(database)
    _add_provider_unresolved(database, source_event_id="f" * 64, decision_key="c" * 64)

    with pytest.raises(builder.BuildError, match="provider_source_event_ambiguous"):
        _build(database, inventory, csv_path, tmp_path / "output")

    assert not (tmp_path / "output").exists()


def test_corrupt_provider_current_decision_lineage_fails_closed(tmp_path: Path) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events(1))
    _add_provider_unresolved(database)
    engine = create_engine(f"sqlite:///{database}", future=True)
    with Session(engine) as session:
        decision = session.get(CashFlowReconciliationDecision, "d" * 64)
        assert decision is not None
        decision.decision_payload_sha256 = "0" * 64
        session.commit()
    engine.dispose()

    with pytest.raises(builder.BuildError, match="provider_current_decision_digest_mismatch"):
        _build(database, inventory, csv_path, tmp_path / "output")

    assert not (tmp_path / "output").exists()


def test_shifted_statement_corroboration_applies_as_exactly_one_provider_dated_flow(
    tmp_path: Path,
) -> None:
    events = _events(1)
    database, inventory, csv_path = _inputs(tmp_path, events)
    provider_date = date.fromisoformat(str(events[0]["date"])) + timedelta(days=7)
    engine = create_engine(f"sqlite:///{database}", future=True)
    with Session(engine) as session:
        transaction = session.get(
            InvestmentTransaction,
            "private-provider-transaction-0",
        )
        assert transaction is not None
        transaction.date = provider_date
        session.commit()
    engine.dispose()
    _add_provider_unresolved(database)
    _mark_provider_decision_resolved(database)
    output = tmp_path / "output"

    _build(database, inventory, csv_path, output)

    manifest_path = next(output.glob("*.json"))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["provider_unresolved_resolutions"] == []
    assert payload["events"][0]["ledger_effective_date"] == provider_date.isoformat()
    assert payload["events"][0]["effective_date_basis"] == "owner_resolved"
    assert payload["events"][0]["decision_authority"] == "owner_approved"
    assert (
        payload["events"][0]["assumption_code"]
        == "corroborating_evidence_confirms_provider_resolved"
    )

    engine = create_engine(f"sqlite:///{database}", future=True)
    with Session(engine) as session:
        source = builder.ManifestSource(manifest_path, csv_path)
        plan = builder.build_reconciliation_plan(session, [source])
        assert plan.conflict_count == 0
        backup_path = tmp_path / "before-reconciliation.db"
        backup_path.write_bytes(database.read_bytes())
        builder_result = apply_reconciliation_plan(
            session,
            plan,
            expected_plan_digest=plan.plan_digest,
            approved_at=datetime(2026, 1, 2, tzinfo=UTC),
            software_revision="a" * 40,
            backup_path=backup_path,
            backup_reference=f"sha256:{_sha256(backup_path)}",
            preview_reference="private:preview:test",
        )
        assert builder_result.committed is True
        ledger = build_external_flow_ledger(
            session,
            _RETURN_START,
            _RETURN_END,
            account_ids=frozenset({plan.entries[0].account_id}),
        )
        assert ledger.issues == ()
        assert len(ledger.entries) == 1
        assert ledger.entries[0].date == provider_date
        assert ledger.entries[0].signed_external_amount == Decimal("1.00")
        assert len(ledger.entries[0].source_event_ids) == 2
    engine.dispose()


def test_provider_resolved_economics_disagreement_fails_closed(tmp_path: Path) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events(1))
    _add_provider_unresolved(database)
    _mark_provider_decision_resolved(database)
    engine = create_engine(f"sqlite:///{database}", future=True)
    with Session(engine) as session:
        decision = session.get(CashFlowReconciliationDecision, "d" * 64)
        assert decision is not None
        decision.signed_external_amount = Decimal("2.00")
        decision.decision_payload_sha256 = canonical_decision_payload_sha256(decision)
        session.commit()
    engine.dispose()

    with pytest.raises(builder.BuildError, match="provider_resolved_economics_mismatch"):
        _build(database, inventory, csv_path, tmp_path / "output")

    assert not (tmp_path / "output").exists()


def test_provider_corroboration_date_must_equal_exact_target_date(tmp_path: Path) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events(1))
    _add_provider_unresolved(database)
    _mark_provider_decision_resolved(database)
    output = tmp_path / "output"
    _build(database, inventory, csv_path, output)
    manifest_path = next(output.glob("*.json"))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["events"][0]["ledger_effective_date"] = "2025-01-03"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    engine = create_engine(f"sqlite:///{database}", future=True)
    with Session(engine) as session:
        plan = builder.build_reconciliation_plan(
            session,
            [builder.ManifestSource(manifest_path, csv_path)],
        )
        assert plan.conflict_count == 1
        assert plan.entries[0].reason_code == (
            "provider_corroboration_date_does_not_match_target_date"
        )
    engine.dispose()


def test_multiple_same_account_candidates_fail_closed_without_output(tmp_path: Path) -> None:
    events = _events()
    database, inventory, csv_path = _inputs(tmp_path, events, duplicate_candidate=True)
    output = tmp_path / "private-execution-manifests"
    before = _sha256(database)

    with pytest.raises(builder.BuildError, match="transaction_match_ambiguous"):
        _build(database, inventory, csv_path, output)

    assert _sha256(database) == before
    assert not output.exists()


def test_inventory_derives_source_code_and_capture_time(
    tmp_path: Path,
) -> None:
    events = _events(2)
    database, inventory, csv_path = _inputs(tmp_path, events)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload.pop("captured_at")
    for event in payload["events"]:
        event.pop("source_code")
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "private-execution-manifests"

    result = _build(database, inventory, csv_path, output)

    assert result.event_count == 2
    manifest = json.loads(next(output.glob("*.json")).read_text(encoding="utf-8"))
    assert len(manifest["account_identity_sha256"]) == 64
    assert manifest["captured_at"].endswith("+00:00")
    assert [event["source_code"] for event in manifest["events"]] == ["ACH", "ACH"]


@pytest.mark.parametrize(
    "field",
    (
        "account_identity_sha256",
        "account_mapping_basis",
        "account_mapping_confidence",
        "account_mapping_evidence_sha256",
    ),
)
def test_inventory_requires_explicit_account_mapping_provenance(
    tmp_path: Path,
    field: str,
) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events(1))
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload.pop(field)
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "output"

    with pytest.raises(builder.BuildError, match="inventory_invalid"):
        _build(database, inventory, csv_path, output)

    assert not output.exists()


def test_account_mapping_basis_confidence_and_evidence_propagate_without_defaulting_exact(
    tmp_path: Path,
) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events(1))
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["account_mapping_basis"] = "statement_account_identifier"
    payload["account_mapping_confidence"] = "high"
    payload["account_mapping_evidence_sha256"] = builder._account_mapping_evidence_sha256(
        account_identity_sha256=payload["account_identity_sha256"],
        account_mapping_basis=payload["account_mapping_basis"],
        account_mapping_confidence=payload["account_mapping_confidence"],
        source_document_sha256=payload["source_document_sha256"],
    )
    inventory.write_text(json.dumps(payload), encoding="utf-8")

    _build(database, inventory, csv_path, tmp_path / "output")

    manifest = json.loads(next((tmp_path / "output").glob("*.json")).read_text())
    assert manifest["account_mapping_basis"] == "statement_account_identifier"
    assert manifest["account_mapping_confidence"] == "high"


def test_account_mapping_evidence_digest_mismatch_fails_closed(tmp_path: Path) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events(1))
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["account_mapping_evidence_sha256"] = "0" * 64
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "output"

    with pytest.raises(builder.BuildError, match="account_mapping_evidence_mismatch"):
        _build(database, inventory, csv_path, output)

    assert not output.exists()


@pytest.mark.parametrize("changed", ["source", "account"])
def test_source_or_account_identity_mismatch_fails_closed(tmp_path: Path, changed: str) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events())
    if changed == "source":
        csv_path.write_text(csv_path.read_text() + "\n", encoding="utf-8")
        expected = "source_hash_mismatch"
    else:
        payload = json.loads(inventory.read_text())
        payload["account_identity_sha256"] = "0" * 64
        inventory.write_text(json.dumps(payload), encoding="utf-8")
        expected = "account_identity_mismatch"

    with pytest.raises(builder.BuildError, match=expected):
        _build(database, inventory, csv_path, tmp_path / "output")


def test_inventory_must_disposition_every_parsed_cashflow_candidate(tmp_path: Path) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events(2))
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["events"] = payload["events"][:1]
    inventory.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(builder.BuildError, match="cashflow_candidate_omitted"):
        _build(database, inventory, csv_path, tmp_path / "output")


def test_only_supported_external_cash_codes_are_candidates(tmp_path: Path) -> None:
    events = _events(5)
    for event, source_code in zip(
        events,
        ("ACH", "MTCH", "ACATI", "DRFRO", "CFIR"),
        strict=True,
    ):
        event["source_code"] = source_code
    database, inventory, csv_path = _inputs(tmp_path, events)
    non_external_rows = (
        "01/01/2025,01/01/2025,01/01/2025,SYN,private description,ACATI,2,,--\n"
        "02/01/2025,02/01/2025,02/01/2025,,private description,SLIP,,,$7.00\n"
        "02/02/2025,02/02/2025,02/02/2025,,private description,FEE,,,($2.00)\n"
        "02/03/2025,02/03/2025,02/03/2025,,private description,GOLD,,,$5.00\n"
        "02/04/2025,02/04/2025,02/04/2025,,private description,REC,,,--\n"
    )
    with csv_path.open("a", encoding="utf-8") as handle:
        handle.write(non_external_rows)
    inventory_payload = json.loads(inventory.read_text(encoding="utf-8"))
    _refresh_source_mapping_evidence(inventory_payload, csv_path)
    inventory.write_text(json.dumps(inventory_payload), encoding="utf-8")

    result = _build(database, inventory, csv_path, tmp_path / "output")

    assert result.event_count == 5
    manifest = json.loads(next((tmp_path / "output").glob("*.json")).read_text())
    assert manifest["cashflow_candidate_count"] == 5
    assert manifest["source_row_count"] == 10
    assert manifest["parser_version"] == "robinhood_activity_csv.v4"
    assert [event["source_code"] for event in manifest["events"]] == [
        "ACH",
        "MTCH",
        "ACATI",
        "DRFRO",
        "CFIR",
    ]


def test_nonnumeric_amount_fails_closed_for_supported_external_cash_code(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "invalid-external-amount.csv"
    csv_path.write_text(
        "Activity Date,Process Date,Settle Date,Instrument,Description,"
        "Trans Code,Quantity,Price,Amount\n"
        "02/01/2025,02/01/2025,02/01/2025,,private description,ACH,,,--\n",
        encoding="utf-8",
    )

    with pytest.raises(builder.BuildError, match="csv_amount_invalid"):
        builder._parse_csv(csv_path)


@pytest.mark.parametrize(
    ("source_code", "instrument", "quantity"),
    (
        ("ACATI", "", "2"),
        ("ACATI", "SYN", ""),
        ("ACH", "SYN", "2"),
    ),
)
def test_nonnumeric_allowlisted_row_requires_sufficient_in_kind_evidence(
    tmp_path: Path,
    source_code: str,
    instrument: str,
    quantity: str,
) -> None:
    csv_path = tmp_path / "insufficient-in-kind.csv"
    csv_path.write_text(
        "Activity Date,Process Date,Settle Date,Instrument,Description,"
        "Trans Code,Quantity,Price,Amount\n"
        f"02/01/2025,02/01/2025,02/01/2025,{instrument},private description,"
        f"{source_code},{quantity},,--\n",
        encoding="utf-8",
    )

    with pytest.raises(builder.BuildError, match="csv_amount_invalid"):
        builder._parse_csv(csv_path)


def test_in_kind_transfer_inside_requested_window_fails_closed(tmp_path: Path) -> None:
    events = _events(1)
    database, inventory, csv_path = _inputs(tmp_path, events)
    with csv_path.open("a", encoding="utf-8") as handle:
        handle.write("02/01/2025,02/01/2025,02/01/2025,SYN,private description,ACATI,2,,--\n")
    inventory_payload = json.loads(inventory.read_text(encoding="utf-8"))
    _refresh_source_mapping_evidence(inventory_payload, csv_path)
    inventory.write_text(json.dumps(inventory_payload), encoding="utf-8")

    with pytest.raises(builder.BuildError, match="in_kind_transfer_inside_requested_window"):
        _build(database, inventory, csv_path, tmp_path / "output")


@pytest.mark.parametrize(
    ("in_kind_date", "return_start"),
    (
        (date(2025, 1, 1), date(2025, 1, 2)),
        (date(2025, 1, 1), date(2025, 1, 1)),
    ),
)
def test_in_kind_transfer_before_or_on_opening_boundary_is_gap_not_cashflow(
    tmp_path: Path,
    in_kind_date: date,
    return_start: date,
) -> None:
    events = _events(1)
    database, inventory, csv_path = _inputs(tmp_path, events)
    row_date = in_kind_date.strftime("%m/%d/%Y")
    with csv_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{row_date},{row_date},{row_date},SYN,private description,ACATI,2,,--\n")
    inventory_payload = json.loads(inventory.read_text(encoding="utf-8"))
    _refresh_source_mapping_evidence(inventory_payload, csv_path)
    inventory.write_text(json.dumps(inventory_payload), encoding="utf-8")

    result = _build(
        database,
        inventory,
        csv_path,
        tmp_path / "output",
        return_start=return_start,
    )

    assert result.event_count == 1
    manifest = json.loads(next((tmp_path / "output").glob("*.json")).read_text())
    assert manifest["cashflow_candidate_count"] == 1
    assert {
        "gap_start": in_kind_date.isoformat(),
        "gap_end": in_kind_date.isoformat(),
        "reason_code": "unreconciled_difference",
    } in manifest["gaps"]


@pytest.mark.parametrize(
    ("return_start", "return_end"),
    (
        (date(2025, 1, 1), date(2025, 1, 1)),
        (date(2025, 1, 2), date(2025, 1, 1)),
    ),
)
def test_requested_return_window_must_be_nonempty_and_ordered(
    tmp_path: Path,
    return_start: date,
    return_end: date,
) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events(1))

    with pytest.raises(builder.BuildError, match="requested_return_window_invalid"):
        _build(
            database,
            inventory,
            csv_path,
            tmp_path / "output",
            return_start=return_start,
            return_end=return_end,
        )


def test_blank_amount_is_accepted_for_proven_acati_in_kind_row_outside_window(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "blank-in-kind-amount.csv"
    csv_path.write_text(
        "Activity Date,Process Date,Settle Date,Instrument,Description,"
        "Trans Code,Quantity,Price,Amount\n"
        "12/31/2024,12/31/2024,12/31/2024,SYN,private description,ACATI,2,,\n",
        encoding="utf-8",
    )

    _source_hash, rows = builder._parse_csv(csv_path)

    assert len(rows) == 1
    assert rows[0].is_in_kind_transfer is True
    assert rows[0].is_cashflow_candidate is False
    assert rows[0].signed_amount is None
    assert len(rows[0].source_row_sha256) == 64


def test_shifted_provider_date_is_deduplicated_and_recorded(tmp_path: Path) -> None:
    events = _events(1)
    database, inventory, csv_path = _inputs(tmp_path, events)
    engine = create_engine(f"sqlite:///{database}", future=True)
    with Session(engine) as session:
        transaction = session.get(
            InvestmentTransaction,
            "private-provider-transaction-0",
        )
        assert transaction is not None
        transaction.date = date.fromisoformat(str(events[0]["date"])) + timedelta(days=7)
        session.commit()
    engine.dispose()

    output = tmp_path / "private-execution-manifests"
    result = _build(database, inventory, csv_path, output)

    assert result.status_counts["provider_exact"] == 1
    assert result.status_counts["statement_supplement"] == 0
    manifest = json.loads(next(output.glob("*.json")).read_text(encoding="utf-8"))
    event = manifest["events"][0]
    assert event["disposition"] == "provider_exact"
    assert event["activity_date"] == str(events[0]["date"])
    assert event["process_date"] == str(events[0]["date"])
    assert event["settlement_date"] == str(events[0]["date"])
    assert event["ledger_effective_date"] == str(events[0]["date"])
    assert event["effective_date_basis"] == "source_activity"
    assert event["assumption_code"] == "provider_posting_date_shift"
    assert len(event["source_row_sha256"]) == 64
    assert event["resolution"]["kind"] == "existing_transaction"


def test_shifted_provider_match_fails_closed_when_bounded_candidates_are_ambiguous(
    tmp_path: Path,
) -> None:
    events = _events(1)
    database, inventory, csv_path = _inputs(tmp_path, events)
    engine = create_engine(f"sqlite:///{database}", future=True)
    with Session(engine) as session:
        transaction = session.get(
            InvestmentTransaction,
            "private-provider-transaction-0",
        )
        assert transaction is not None
        transaction.date = date.fromisoformat(str(events[0]["date"])) + timedelta(days=7)
        session.add(
            InvestmentTransaction(
                plaid_investment_transaction_id="private-provider-shifted-duplicate",
                account_id=transaction.account_id,
                security_id=None,
                date=date.fromisoformat(str(events[0]["date"])) + timedelta(days=6),
                name="private shifted duplicate",
                quantity=Decimal(0),
                amount=transaction.amount,
                price=None,
                fees=None,
                type="cash",
                subtype="deposit",
                currency="USD",
                origin="broker",
            )
        )
        session.commit()
    engine.dispose()

    with pytest.raises(builder.BuildError, match="transaction_match_ambiguous"):
        _build(database, inventory, csv_path, tmp_path / "output")


def test_cli_stdout_contains_only_sanitized_counts_and_digests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events())
    output = tmp_path / "private-execution-manifests"

    exit_code = builder.main(
        [
            "--db",
            str(database),
            "--inventory",
            str(inventory),
            "--csv",
            str(csv_path),
            "--output-dir",
            str(output),
            "--return-start",
            _RETURN_START.isoformat(),
            "--return-end",
            _RETURN_END.isoformat(),
        ]
    )

    assert exit_code == 0
    stdout = capsys.readouterr().out
    summary = json.loads(stdout)
    assert set(summary) == {
        "manifest_count",
        "event_count",
        "status_counts",
        "conflict_count",
        "output_digest",
    }
    assert summary["output_digest"].startswith("sha256:")
    for secret in (
        str(database),
        str(inventory),
        str(csv_path),
        "private-provider",
        "Private Account Name",
        "private-credential",
    ):
        assert secret not in stdout
