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
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0026')")
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
    assert len(payload["events"]) == 68
    assert all("resolution" in event for event in payload["events"])
    assert payload["events"][67]["disposition"] == "statement_supplement"
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


def test_inventory_must_disposition_every_parsed_cashflow_candidate(tmp_path: Path) -> None:
    database, inventory, csv_path = _inputs(tmp_path, _events(2))
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["events"] = payload["events"][:1]
    inventory.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(builder.BuildError, match="cashflow_candidate_omitted"):
        builder.build_execution_manifests(
            database,
            [inventory],
            [csv_path],
            tmp_path / "output",
        )


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
        "12/31/2024,12/31/2024,12/31/2024,SYN,private description,ACATI,2,,--\n"
        "02/01/2025,02/01/2025,02/01/2025,,private description,SLIP,,,$7.00\n"
        "02/02/2025,02/02/2025,02/02/2025,,private description,FEE,,,($2.00)\n"
        "02/03/2025,02/03/2025,02/03/2025,,private description,GOLD,,,$5.00\n"
        "02/04/2025,02/04/2025,02/04/2025,,private description,REC,,,--\n"
    )
    with csv_path.open("a", encoding="utf-8") as handle:
        handle.write(non_external_rows)
    inventory_payload = json.loads(inventory.read_text(encoding="utf-8"))
    inventory_payload["source_document_sha256"] = _sha256(csv_path)
    inventory.write_text(json.dumps(inventory_payload), encoding="utf-8")

    result = builder.build_execution_manifests(
        database,
        [inventory],
        [csv_path],
        tmp_path / "output",
    )

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
    inventory_payload["source_document_sha256"] = _sha256(csv_path)
    inventory.write_text(json.dumps(inventory_payload), encoding="utf-8")

    with pytest.raises(builder.BuildError, match="in_kind_transfer_inside_requested_window"):
        builder.build_execution_manifests(
            database,
            [inventory],
            [csv_path],
            tmp_path / "output",
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
    result = builder.build_execution_manifests(database, [inventory], [csv_path], output)

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
        builder.build_execution_manifests(
            database,
            [inventory],
            [csv_path],
            tmp_path / "output",
        )


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
