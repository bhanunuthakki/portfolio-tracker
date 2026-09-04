from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from portfolio_tracker.jobs.import_account_valuation_manifest import (
    ValuationImportConflictError,
    ValuationManifestValidationError,
    apply_account_valuation_import_plan,
    build_account_valuation_import_plan,
    canonical_account_identity_sha256,
    canonical_manifest_value_sha256,
    write_account_valuation_import_preview,
)
from portfolio_tracker.models import (
    Account,
    AccountValuationObservation,
    AccountValuationSourceKind,
    Base,
    Item,
)
from portfolio_tracker.services.account_valuations import (
    NewAccountValuationObservation,
    record_account_valuation_observation,
)


def _database(
    tmp_path: Path, *, account_type: str = "investment"
) -> tuple[Session, Account, Item, Path]:
    database_path = tmp_path / "portfolio-test.db"
    engine = create_engine(f"sqlite:///{database_path}", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    item = Item(
        source="plaid",
        plaid_item_id="item-1",
        institution_name="Broker",
        is_data_active=True,
    )
    account = Account(
        item=item,
        plaid_account_id="provider-account-1",
        name="Taxable",
        type=account_type,
        currency="USD",
    )
    session.add(account)
    session.commit()
    return session, account, item, database_path


def _write_manifest(
    path: Path,
    *,
    account: Account,
    item: Item,
    source_path: Path,
    total_value: Decimal = Decimal("100000.00"),
    account_identity_sha256: str | None = None,
) -> None:
    document_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    locator = "page=1;field=ending_account_value"
    as_of_date = date(2025, 12, 31)
    as_of_at = datetime(2025, 12, 31, 21, tzinfo=UTC)
    cash_value = Decimal("5000.00")
    source_value_sha = canonical_manifest_value_sha256(
        source_document_sha256=document_sha,
        source_locator=locator,
        as_of_date=as_of_date,
        as_of_at=as_of_at,
        total_value=total_value,
        cash_value=cash_value,
        currency="USD",
        is_complete=True,
        is_empty=False,
    )
    payload = {
        "schema_version": "account_valuation_manifest.v1",
        "account_id": account.account_id,
        "account_identity_sha256": account_identity_sha256
        or canonical_account_identity_sha256(account, item),
        "account_mapping_basis": "provider_account_id",
        "account_mapping_confidence": "exact",
        "source_kind": "brokerage_statement",
        "source_provider": "broker",
        "source_reference": "2025-12 monthly statement",
        "source_document_sha256": document_sha,
        "captured_at": "2026-09-03T19:00:00Z",
        "source_timezone": "America/New_York",
        "source_row_count": 1,
        "rows": [
            {
                "source_locator": locator,
                "source_value_sha256": source_value_sha,
                "as_of_date": as_of_date.isoformat(),
                "as_of_at": "2025-12-31T21:00:00Z",
                "total_value": format(total_value, "f"),
                "cash_value": "5000.00",
                "currency": "USD",
                "is_complete": True,
                "is_empty": False,
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _backup(database_path: Path, backup_path: Path) -> None:
    # The test session is committed before each backup, so a byte copy is a
    # complete SQLite recovery point without touching any live/user database.
    shutil.copyfile(database_path, backup_path)


def test_preview_then_apply_is_hash_bound_append_only_and_replay_safe(tmp_path: Path):
    session, account, item, database_path = _database(tmp_path)
    source_path = tmp_path / "statement.pdf"
    source_path.write_bytes(b"synthetic statement evidence")
    manifest_path = tmp_path / "manifest.json"
    preview_path = tmp_path / "preview.json"
    backup_path = tmp_path / "before.db"
    _write_manifest(
        manifest_path,
        account=account,
        item=item,
        source_path=source_path,
    )

    plan = build_account_valuation_import_plan(
        session, manifest_path=manifest_path, source_path=source_path
    )
    assert plan.missing_insert_count == 1
    assert plan.existing_exact_count == 0
    assert plan.conflict_count == 0
    assert session.scalar(select(func.count()).select_from(AccountValuationObservation)) == 0
    write_account_valuation_import_preview(plan, preview_path)
    _backup(database_path, backup_path)

    created = apply_account_valuation_import_plan(
        session,
        manifest_path=manifest_path,
        source_path=source_path,
        preview_path=preview_path,
        backup_path=backup_path,
        expected_plan_digest=plan.plan_digest,
    )

    assert created == 1
    row = session.scalar(select(AccountValuationObservation))
    assert row is not None
    assert row.total_value == Decimal("100000")
    assert row.cash_value == Decimal("5000")
    assert row.source_kind == "brokerage_statement"
    assert "page=1;field=ending_account_value" in row.source_reference
    assert row.source_payload_sha256 == plan.entries[0].source_value_sha256

    replay = build_account_valuation_import_plan(
        session, manifest_path=manifest_path, source_path=source_path
    )
    assert replay.existing_exact_count == 1
    assert replay.missing_insert_count == 0
    session.close()


def test_preview_accepts_legacy_brokerage_account_type(tmp_path: Path) -> None:
    session, account, item, _ = _database(tmp_path, account_type="brokerage")
    source_path = tmp_path / "statement.pdf"
    source_path.write_bytes(b"synthetic brokerage statement evidence")
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(
        manifest_path,
        account=account,
        item=item,
        source_path=source_path,
    )

    plan = build_account_valuation_import_plan(
        session, manifest_path=manifest_path, source_path=source_path
    )

    assert plan.missing_insert_count == 1
    session.close()


@pytest.mark.parametrize("alias_kind", ["manifest", "source"])
def test_preview_rejects_manifest_or_source_path_alias_before_write(
    tmp_path: Path, alias_kind: str
) -> None:
    session, account, item, _ = _database(tmp_path)
    source_path = tmp_path / "statement.pdf"
    source_path.write_bytes(b"source bytes that must survive")
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(
        manifest_path,
        account=account,
        item=item,
        source_path=source_path,
    )
    plan = build_account_valuation_import_plan(
        session, manifest_path=manifest_path, source_path=source_path
    )
    target = manifest_path if alias_kind == "manifest" else source_path
    before = target.read_bytes()

    with pytest.raises(ValuationManifestValidationError, match="must not alias"):
        write_account_valuation_import_preview(plan, target)

    assert target.read_bytes() == before
    session.close()


def test_source_file_and_account_mapping_must_match_exact_hashes(tmp_path: Path):
    session, account, item, _ = _database(tmp_path)
    source_path = tmp_path / "statement.pdf"
    source_path.write_bytes(b"statement version one")
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(
        manifest_path,
        account=account,
        item=item,
        source_path=source_path,
    )
    source_path.write_bytes(b"statement version two")

    with pytest.raises(ValuationManifestValidationError, match="evidence file"):
        build_account_valuation_import_plan(
            session, manifest_path=manifest_path, source_path=source_path
        )

    source_path.write_bytes(b"statement version one")
    _write_manifest(
        manifest_path,
        account=account,
        item=item,
        source_path=source_path,
        account_identity_sha256="a" * 64,
    )
    with pytest.raises(ValuationManifestValidationError, match="mapped account"):
        build_account_valuation_import_plan(
            session, manifest_path=manifest_path, source_path=source_path
        )
    session.close()


def test_same_source_locator_with_changed_interpretation_is_a_conflict(tmp_path: Path):
    session, account, item, database_path = _database(tmp_path)
    source_path = tmp_path / "statement.pdf"
    source_path.write_bytes(b"statement")
    manifest_path = tmp_path / "manifest.json"
    preview_path = tmp_path / "preview.json"
    backup_path = tmp_path / "before.db"
    _write_manifest(
        manifest_path,
        account=account,
        item=item,
        source_path=source_path,
    )
    plan = build_account_valuation_import_plan(
        session, manifest_path=manifest_path, source_path=source_path
    )
    write_account_valuation_import_preview(plan, preview_path)
    _backup(database_path, backup_path)
    apply_account_valuation_import_plan(
        session,
        manifest_path=manifest_path,
        source_path=source_path,
        preview_path=preview_path,
        backup_path=backup_path,
        expected_plan_digest=plan.plan_digest,
    )

    _write_manifest(
        manifest_path,
        account=account,
        item=item,
        source_path=source_path,
        total_value=Decimal("100001.00"),
    )
    changed = build_account_valuation_import_plan(
        session, manifest_path=manifest_path, source_path=source_path
    )
    assert changed.conflict_count == 1
    assert changed.missing_insert_count == 0
    session.close()


def test_apply_fails_when_backup_does_not_match_previewed_state(tmp_path: Path):
    session, account, item, database_path = _database(tmp_path)
    source_path = tmp_path / "statement.pdf"
    source_path.write_bytes(b"statement")
    manifest_path = tmp_path / "manifest.json"
    preview_path = tmp_path / "preview.json"
    stale_backup_path = tmp_path / "stale.db"
    _write_manifest(
        manifest_path,
        account=account,
        item=item,
        source_path=source_path,
    )
    # This backup is structurally valid but predates a durable observation.
    _backup(database_path, stale_backup_path)
    record_account_valuation_observation(
        session,
        NewAccountValuationObservation(
            account_id=account.account_id,
            as_of_date=date(2025, 1, 1),
            total_value=Decimal("1"),
            cash_value=None,
            currency="USD",
            source_kind=AccountValuationSourceKind.PROVIDER_API,
            source_provider="plaid",
            source_reference="test fixture",
            source_record_id="fixture",
            source_payload_sha256=None,
            fetched_at=datetime(2025, 1, 2, tzinfo=UTC),
            is_complete=True,
            is_empty=False,
        ),
    )
    session.commit()
    plan = build_account_valuation_import_plan(
        session, manifest_path=manifest_path, source_path=source_path
    )
    write_account_valuation_import_preview(plan, preview_path)

    with pytest.raises(ValuationImportConflictError, match="backup does not match"):
        apply_account_valuation_import_plan(
            session,
            manifest_path=manifest_path,
            source_path=source_path,
            preview_path=preview_path,
            backup_path=stale_backup_path,
            expected_plan_digest=plan.plan_digest,
        )
    assert session.scalar(select(func.count()).select_from(AccountValuationObservation)) == 1
    session.close()


def test_plan_and_backup_checks_bind_stored_canonical_payloads(tmp_path: Path):
    session, account, item, database_path = _database(tmp_path)
    source_path = tmp_path / "statement.pdf"
    source_path.write_bytes(b"statement")
    manifest_path = tmp_path / "manifest.json"
    preview_path = tmp_path / "preview.json"
    backup_path = tmp_path / "before.db"
    _write_manifest(
        manifest_path,
        account=account,
        item=item,
        source_path=source_path,
    )
    plan = build_account_valuation_import_plan(
        session, manifest_path=manifest_path, source_path=source_path
    )
    write_account_valuation_import_preview(plan, preview_path)
    _backup(database_path, backup_path)
    with sqlite3.connect(backup_path) as connection:
        connection.execute(
            "UPDATE accounts SET plaid_account_id = ? WHERE account_id = ?",
            ("tampered-provider-account", account.account_id),
        )

    with pytest.raises(ValuationImportConflictError, match="backup does not match"):
        apply_account_valuation_import_plan(
            session,
            manifest_path=manifest_path,
            source_path=source_path,
            preview_path=preview_path,
            backup_path=backup_path,
            expected_plan_digest=plan.plan_digest,
        )

    # A stored observation whose fields no longer match its immutable key also
    # blocks planning, rather than being reduced to an apparently valid key.
    session.add(
        AccountValuationObservation(
            observation_key="d" * 64,
            account_id=account.account_id,
            as_of_date=date(2024, 1, 1),
            total_value=Decimal("10"),
            cash_value=None,
            currency="USD",
            source_kind="provider_api",
            source_provider="plaid",
            source_reference="tampered fixture",
            source_record_id="tampered-fixture",
            source_payload_sha256=None,
            normalization_version="1",
            fetched_at=datetime(2024, 1, 2, tzinfo=UTC),
            is_complete=True,
            is_empty=False,
        )
    )
    session.commit()
    with pytest.raises(ValuationImportConflictError, match="canonical integrity"):
        build_account_valuation_import_plan(
            session, manifest_path=manifest_path, source_path=source_path
        )
    session.close()


def test_apply_reparses_manifest_and_rejects_changed_plan(tmp_path: Path):
    session, account, item, database_path = _database(tmp_path)
    source_path = tmp_path / "statement.pdf"
    source_path.write_bytes(b"statement")
    manifest_path = tmp_path / "manifest.json"
    preview_path = tmp_path / "preview.json"
    backup_path = tmp_path / "before.db"
    _write_manifest(
        manifest_path,
        account=account,
        item=item,
        source_path=source_path,
    )
    plan = build_account_valuation_import_plan(
        session, manifest_path=manifest_path, source_path=source_path
    )
    write_account_valuation_import_preview(plan, preview_path)
    _backup(database_path, backup_path)
    _write_manifest(
        manifest_path,
        account=account,
        item=item,
        source_path=source_path,
        total_value=Decimal("99999.00"),
    )

    with pytest.raises(ValuationImportConflictError, match="expected_plan_digest"):
        apply_account_valuation_import_plan(
            session,
            manifest_path=manifest_path,
            source_path=source_path,
            preview_path=preview_path,
            backup_path=backup_path,
            expected_plan_digest=plan.plan_digest,
        )
    assert session.scalar(select(func.count()).select_from(AccountValuationObservation)) == 0
    session.close()
