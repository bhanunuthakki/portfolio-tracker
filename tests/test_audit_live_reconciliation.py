from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    Base,
    CashFlowSourceAttestation,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import audit_live_reconciliation as audit  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_database(path: Path) -> tuple[int, int, int]:
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0026')")

    with Session(engine) as session:
        broker_item = Item(
            source="plaid",
            plaid_item_id="provider-item-private",
            plaid_access_token_encrypted="credential-secret-value",
            institution_name="Synthetic Broker",
            is_data_active=True,
        )
        source_item = Item(
            source="snaptrade",
            snaptrade_user_id="synthetic-user",
            snaptrade_user_secret_encrypted="another-credential-secret",
            snaptrade_authorization_id="synthetic-authorization",
            institution_name="Northstar Robotics Benefits",
            is_data_active=True,
        )
        destination_item = Item(
            source="plaid",
            plaid_item_id="blue-harbor-synthetic-item",
            plaid_access_token_encrypted="destination-credential-secret",
            institution_name="Blue Harbor Financial",
            is_data_active=True,
        )
        session.add_all([broker_item, source_item, destination_item])
        session.flush()
        evidence_account = Account(
            item_id=broker_item.item_id,
            plaid_account_id="evidence-account-private",
            name="Statement account private label",
            type="investment",
            subtype="brokerage",
            currency="USD",
        )
        source_account = Account(
            item_id=source_item.item_id,
            plaid_account_id="northstar-source-synthetic",
            name="Northstar Robotics retirement plan",
            type="investment",
            subtype="401k",
            currency="USD",
        )
        destination_account = Account(
            item_id=destination_item.item_id,
            plaid_account_id="blue-harbor-destination-synthetic",
            name="Blue Harbor rollover account",
            type="investment",
            subtype="ira",
            currency="USD",
        )
        session.add_all([evidence_account, source_account, destination_account])
        session.flush()
        security = Security(
            plaid_security_id="synthetic-security",
            ticker="SYN",
            name="Synthetic security",
            type="equity",
            currency="USD",
        )
        session.add(security)
        session.flush()
        for account in (evidence_account, source_account, destination_account):
            session.add(
                HoldingSnapshot(
                    snapshot_date=date(2032, 6, 30),
                    account_id=account.account_id,
                    security_id=security.security_id,
                    quantity=Decimal("1"),
                    institution_price=Decimal("100"),
                    institution_value=Decimal("100"),
                    currency="USD",
                    origin="broker",
                )
            )

        def transaction(
            transaction_id: str,
            event_date: date,
            amount: Decimal,
            *,
            subtype: str = "deposit",
            account_id: int | None = None,
        ) -> InvestmentTransaction:
            return InvestmentTransaction(
                plaid_investment_transaction_id=transaction_id,
                account_id=account_id or evidence_account.account_id,
                security_id=None,
                date=event_date,
                name="private transaction description",
                quantity=Decimal(0),
                amount=amount,
                price=None,
                fees=None,
                type="cash",
                subtype=subtype,
                currency="USD",
                origin="broker",
            )

        session.add(transaction("synthetic-target-a", date(2030, 10, 14), Decimal("731.25")))
        session.add(transaction("synthetic-target-b", date(2031, 8, 21), Decimal("2840")))
        start = date(2031, 1, 1)
        for offset in range(64):
            session.add(
                transaction(
                    f"private-exact-{offset}",
                    start + timedelta(days=offset),
                    Decimal(offset + 1),
                )
            )
        session.add(
            transaction(
                "synthetic-classification-mismatch",
                date(2031, 5, 20),
                Decimal("55"),
            )
        )
        session.add(
            transaction(
                "synthetic-named-transfer-amount",
                date(2031, 4, 12),
                Decimal("4321.09"),
                account_id=destination_account.account_id,
            )
        )
        session.add(
            CashFlowSourceAttestation(
                attestation_key="synthetic-approved-source",
                account_id=evidence_account.account_id,
                coverage_start=date(2030, 7, 1),
                coverage_end=date(2032, 6, 30),
                source_type="brokerage_statement",
                source_reference="private/source.csv",
                source_sha256="a" * 64,
                captured_at=datetime(2032, 6, 30, tzinfo=UTC),
                approved_at=datetime(2032, 6, 30, tzinfo=UTC),
                methodology_version="1",
            )
        )
        session.commit()
        result = (
            evidence_account.account_id,
            source_account.account_id,
            destination_account.account_id,
        )
    engine.dispose()
    return result


def _write_manifest(path: Path, account_id: int) -> None:
    events: list[dict[str, object]] = [
        {
            "source_row_ordinal": 1,
            "date": "2030-10-14",
            "signed_external_amount": "731.25",
            "classification": "external_in",
        },
        {
            "source_row_ordinal": 2,
            "date": "2031-08-21",
            "signed_external_amount": "2840.00",
            "classification": "external_in",
        },
        {
            "source_row_ordinal": 3,
            "date": "2031-08-24",
            "signed_external_amount": "-2840.00",
            "classification": "external_out",
        },
    ]
    start = date(2031, 1, 1)
    events.extend(
        {
            "source_row_ordinal": offset + 4,
            "date": (start + timedelta(days=offset)).isoformat(),
            "signed_external_amount": f"{offset + 1}.00",
            "classification": "external_in",
        }
        for offset in range(64)
    )
    events.append(
        {
            "source_row_ordinal": 68,
            "date": "2031-05-20",
            "signed_external_amount": "-55.00",
            "classification": "external_out",
        }
    )
    path.write_text(
        json.dumps(
            {
                "account_id": account_id,
                "coverage_start": "2030-06-30",
                "coverage_end": "2032-06-30",
                "source_document_sha256": "b" * 64,
                "source_type": "brokerage_statement",
                "events": events,
            }
        ),
        encoding="utf-8",
    )


def _write_review_spec(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "target_events": [
                    {
                        "label": "synthetic_existing_a",
                        "date": "2030-10-14",
                        "signed_external_amount": "731.25",
                        "classification": "external_in",
                    },
                    {
                        "label": "synthetic_existing_b",
                        "date": "2031-08-21",
                        "signed_external_amount": "2840.00",
                        "classification": "external_in",
                    },
                    {
                        "label": "synthetic_missing_reversal",
                        "date": "2031-08-24",
                        "signed_external_amount": "-2840.00",
                        "classification": "external_out",
                    },
                ],
                "named_transfer_universe": {
                    "source_search_terms": ["northstar robotics"],
                    "destination_search_terms": ["blue harbor"],
                    "authoritative_amount": "4321.09",
                    "date_window_start": "2031-04-01",
                    "date_window_end": "2031-04-30",
                },
            }
        ),
        encoding="utf-8",
    )


def test_audit_resolves_every_event_and_reports_private_evidence(tmp_path: Path) -> None:
    database = tmp_path / "private-live.db"
    evidence_account_id, _, _ = _build_database(database)
    manifest = tmp_path / "private-manifest.json"
    _write_manifest(manifest, evidence_account_id)
    review_spec = tmp_path / "private-review-spec.json"
    _write_review_spec(review_spec)
    before = _sha256(database)

    preview = audit.audit_database(database, [manifest], review_spec)

    assert _sha256(database) == before
    database_preview = cast("dict[str, object]", preview["database"])
    assert database_preview["query_only"] is True
    assert database_preview["integrity"] == "ok"
    assert database_preview["alembic_revision"] == "0026"
    assert preview["window"] == {
        "latest_complete_observed_snapshot_date": "2032-06-30",
        "two_year_start_date": "2030-06-30",
        "elapsed_days": 731,
        "cashflow_boundary": "(start, end]",
        "source_coverage_required_start_date": "2030-07-01",
    }
    resolution = cast("dict[str, object]", preview["resolution"])
    assert resolution["event_count"] == 68
    assert resolution["status_counts"] == {
        "existing_exact": 66,
        "override_required": 1,
        "missing_insert": 1,
        "conflict": 0,
    }
    targets = cast("dict[str, list[dict[str, object]]]", resolution["targeted_events"])
    assert targets["synthetic_existing_a"][0]["status"] == "existing_exact"
    assert targets["synthetic_existing_b"][0]["status"] == "existing_exact"
    assert targets["synthetic_missing_reversal"][0]["status"] == "missing_insert"
    universe = cast("dict[str, object]", preview["named_universe_review"])
    assert universe["classification"] == "source_inside_destination_inside"
    assert len(cast("list[object]", universe["source_accounts"])) == 1
    assert len(cast("list[object]", universe["destination_accounts"])) == 1
    assert universe["authoritative_amount_candidate_count"] == 1
    coverage = cast("dict[str, object]", preview["source_coverage"])
    assert coverage["is_complete"] is False
    assert coverage["account_status_counts"] == {
        # A legacy 0025 attestation without parsed Source events remains
        # evidence, but it cannot certify source completeness under the
        # provenance-backed reconciliation contract.
        "complete": 0,
        "partial": 0,
        "missing": 3,
    }
    serialized = json.dumps(preview, sort_keys=True)
    for secret in (
        "credential-secret-value",
        "another-credential-secret",
        "private transaction description",
        "private-dec-id",
        "private-live.db",
    ):
        assert secret not in serialized
    table_counts = cast("dict[str, int]", database_preview["table_counts"])
    table_checksums = cast("dict[str, str]", database_preview["table_checksums"])
    assert table_counts["investment_transactions"] == 68
    assert table_checksums["investment_transactions"].startswith("sha256:")


def test_cli_writes_private_preview_and_stdout_has_only_counts_and_digests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "private-live.db"
    evidence_account_id, _, _ = _build_database(database)
    manifest = tmp_path / "private-manifest.json"
    _write_manifest(manifest, evidence_account_id)
    review_spec = tmp_path / "private-review-spec.json"
    _write_review_spec(review_spec)
    output = tmp_path / "private-preview.json"

    result = audit.main(
        [
            "--db",
            str(database),
            "--output",
            str(output),
            "--manifest",
            str(manifest),
            "--review-spec",
            str(review_spec),
        ]
    )

    assert result == 0
    console = json.loads(capsys.readouterr().out)
    assert all(
        isinstance(value, int) or (isinstance(value, str) and value.startswith("sha256:"))
        for value in console.values()
    )
    rendered = output.read_text(encoding="utf-8")
    assert "Northstar Robotics retirement plan" not in rendered
    assert "blue-harbor-destination-synthetic" not in rendered
    assert "credential-secret-value" not in rendered
    assert str(database) not in rendered
    assert stat_mode(output) == 0o600


def test_optional_review_spec_and_invalid_spec_fail_safely(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "private-live.db"
    evidence_account_id, _, _ = _build_database(database)
    manifest = tmp_path / "private-manifest.json"
    _write_manifest(manifest, evidence_account_id)

    preview = audit.audit_database(database, [manifest])
    resolution = cast("dict[str, object]", preview["resolution"])
    assert resolution["targeted_events"] == {}
    universe = cast("dict[str, object]", preview["named_universe_review"])
    assert universe["status"] == "not_requested"

    invalid_spec = tmp_path / "invalid-private-review.json"
    invalid_spec.write_text(
        '{"target_events": [{"label": "unsafe label", "date": "2031-01-01", '
        '"signed_external_amount": "NaN", "classification": "external_in"}]}',
        encoding="utf-8",
    )
    output = tmp_path / "should-not-exist.json"
    result = audit.main(
        [
            "--db",
            str(database),
            "--output",
            str(output),
            "--manifest",
            str(manifest),
            "--review-spec",
            str(invalid_spec),
        ]
    )
    console = json.loads(capsys.readouterr().out)
    assert result == 2
    assert console["error_count"] == 1
    assert cast("str", console["error_digest"]).startswith("sha256:")
    assert not output.exists()


def test_revision_mismatch_fails_without_path_or_exception_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "private-live.db"
    evidence_account_id, _, _ = _build_database(database)
    manifest = tmp_path / "private-manifest.json"
    _write_manifest(manifest, evidence_account_id)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'unexpected-private-revision'")
    output = tmp_path / "private-preview.json"

    result = audit.main(
        [
            "--db",
            str(database),
            "--output",
            str(output),
            "--manifest",
            str(manifest),
        ]
    )

    assert result == 2
    captured = capsys.readouterr()
    console = json.loads(captured.out)
    assert console["error_count"] == 1
    assert cast("str", console["error_digest"]).startswith("sha256:")
    assert captured.err == ""
    assert str(database) not in captured.out
    assert "unexpected-private-revision" not in captured.out
    assert not output.exists()


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
