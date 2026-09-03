from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    Base,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import build_cashflow_execution_manifests as builder  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database(path: Path, events: list[dict[str, object]]) -> tuple[int, str]:
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0025')")
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
            f"{event_date:%m/%d/%Y},,,,private description,{event['source_code']},,,"
            f"${event['signed_external_amount']}\n"
        )
    csv_path.write_text(header + "".join(rows), encoding="utf-8")
    inventory = tmp_path / "private-inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "account_id": account_id,
                "account_identity_sha256": fingerprint,
                "coverage_start": "2025-01-01",
                "coverage_end": "2025-12-31",
                "source_type": "brokerage_statement",
                "source_document_sha256": _sha256(csv_path),
                "captured_at": "2026-01-01T00:00:00+00:00",
                "events": events,
                "gaps": [],
            }
        ),
        encoding="utf-8",
    )
    return database, inventory, csv_path


def test_builds_68_explicit_one_to_one_resolutions_without_mutating_database(
    tmp_path: Path,
) -> None:
    events = _events()
    database, inventory, csv_path = _inputs(tmp_path, events)
    output = tmp_path / "private-execution-manifests"
    before = _sha256(database)

    result = builder.build_execution_manifests(database, [inventory], [csv_path], output)

    assert _sha256(database) == before
    assert result.event_count == 68
    assert result.status_counts == {
        "existing_exact": 66,
        "override_required": 1,
        "manual_transaction": 1,
    }
    assert result.conflict_count == 0
    manifests = list(output.glob("*.json"))
    assert len(manifests) == 1
    assert os.stat(manifests[0]).st_mode & 0o777 == 0o600
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert len(payload["events"]) == 68
    assert all("resolution" in event for event in payload["events"])
    assert payload["events"][67]["resolution"] == {"kind": "manual_transaction"}
    assert payload["events"][0]["resolution"]["transaction_identity_sha256"]
    serialized = manifests[0].read_text(encoding="utf-8")
    assert "private-provider-transaction" not in serialized
    assert "Private Account Name" not in serialized
    assert "private-credential" not in serialized


def test_multiple_same_account_candidates_fail_closed_without_output(tmp_path: Path) -> None:
    events = _events()
    database, inventory, csv_path = _inputs(tmp_path, events, duplicate_candidate=True)
    output = tmp_path / "private-execution-manifests"
    before = _sha256(database)

    with pytest.raises(builder.BuildError, match="transaction_match_ambiguous"):
        builder.build_execution_manifests(database, [inventory], [csv_path], output)

    assert _sha256(database) == before
    assert not output.exists()


def test_legacy_inventory_derives_exact_source_code_account_hash_and_capture_time(
    tmp_path: Path,
) -> None:
    events = _events(2)
    database, inventory, csv_path = _inputs(tmp_path, events)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload.pop("account_identity_sha256")
    payload.pop("captured_at")
    for event in payload["events"]:
        event.pop("source_code")
    inventory.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "private-execution-manifests"

    result = builder.build_execution_manifests(database, [inventory], [csv_path], output)

    assert result.event_count == 2
    manifest = json.loads(next(output.glob("*.json")).read_text(encoding="utf-8"))
    assert len(manifest["account_identity_sha256"]) == 64
    assert manifest["captured_at"].endswith("+00:00")
    assert [event["source_code"] for event in manifest["events"]] == ["ACH", "ACH"]


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
        builder.build_execution_manifests(database, [inventory], [csv_path], tmp_path / "output")


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
