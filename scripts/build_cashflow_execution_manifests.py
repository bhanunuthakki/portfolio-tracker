"""Build private, explicit cash-flow execution manifests without mutating the DB."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from portfolio_tracker.jobs.reconcile_cashflow_manifest import (  # noqa: E402
    ManifestSource,
    build_reconciliation_plan,
    transaction_identity_sha256,
    transaction_payload_sha256,
)
from portfolio_tracker.models import (  # noqa: E402
    Account,
    CashFlowReconciliationDecision,
    InvestmentTransaction,
    Item,
    TransactionOverride,
)
from portfolio_tracker.services.active_items import valued_account_ids  # noqa: E402

EXPECTED_ALEMBIC_REVISION = "0026"
PARSER_VERSION = "robinhood_activity_csv.v3"
SOURCE_TIMEZONE = "America/New_York"
_DATE_SHIFT_DAYS = 14
_HEADERS: tuple[str, ...] = (
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
_STATUS_ORDER = (
    "provider_exact",
    "statement_supplement",
    "internal",
    "excluded",
    "unresolved",
)
_SUPPORTED_EXTERNAL_CASH_CODES = frozenset({"ACH", "MTCH", "ACATI", "DRFRO", "CFIR"})


class BuildError(RuntimeError):
    """Expected fail-closed outcome identified by a non-sensitive code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EvidenceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_row_ordinal: int | None = Field(default=None, gt=0)
    date: date
    source_amount: Decimal | None = None
    signed_external_amount: Decimal | None = None
    classification: Literal["external_in", "external_out", "internal", "excluded"] | None = None
    source_code: str | None = None
    disposition: (
        Literal[
            "provider_exact",
            "statement_supplement",
            "internal",
            "excluded",
            "unresolved",
        ]
        | None
    ) = None
    ledger_effective_date: date | None = None
    effective_date_basis: Literal[
        "source_activity",
        "source_process",
        "source_settlement",
        "provider_posting",
        "owner_resolved",
    ] = "source_activity"
    effective_timezone: str = SOURCE_TIMEZONE
    confidence: Literal["exact", "high", "provisional"] = "high"
    assumption_code: str = "statement_activity_date_used"
    decision_authority: Literal["provider", "brokerage_statement", "owner_approved"] = (
        "owner_approved"
    )

    @model_validator(mode="after")
    def validate_direction(self) -> EvidenceEvent:
        source_amount = self.source_amount
        signed = self.signed_external_amount
        if source_amount is not None and not source_amount.is_finite():
            raise ValueError("source amount must be finite")
        if self.disposition == "unresolved":
            if self.classification is not None or signed is not None:
                raise ValueError("unresolved events cannot assert an economic classification")
        elif self.classification in {"internal", "excluded"}:
            if signed != 0:
                raise ValueError("internal and excluded events require zero external amount")
        else:
            if self.classification not in {"external_in", "external_out"}:
                raise ValueError("resolved external events require a classification")
            if signed is None or not signed.is_finite() or signed == 0:
                raise ValueError("external amount must be finite and nonzero")
            if (self.classification == "external_in") != (signed > 0):
                raise ValueError("classification conflicts with amount sign")
        if self.source_code is not None and (
            not self.source_code.strip()
            or self.source_code != self.source_code.strip()
            or len(self.source_code) > 32
        ):
            raise ValueError("invalid source code")
        return self


class EvidenceGap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gap_start: date
    gap_end: date
    reason_code: Literal[
        "provider_history_unavailable",
        "statement_missing",
        "unresolved_classification",
        "unreconciled_difference",
    ]

    @model_validator(mode="after")
    def validate_range(self) -> EvidenceGap:
        if self.gap_start > self.gap_end:
            raise ValueError("gap dates are reversed")
        return self


class EvidenceInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: int = Field(gt=0)
    account_identity_sha256: str | None = None
    coverage_start: date
    coverage_end: date
    source_type: Literal["brokerage_statement", "provider_export", "owner_reconciliation"] = (
        "brokerage_statement"
    )
    source_document_sha256: str
    captured_at: datetime | None = None
    methodology_version: str = "1"
    gaps: list[EvidenceGap] = Field(default_factory=list[EvidenceGap])
    events: list[EvidenceEvent] = Field(default_factory=list[EvidenceEvent])

    @field_validator("account_identity_sha256", "source_document_sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("expected lowercase SHA-256")
        return value

    @field_validator("methodology_version")
    @classmethod
    def validate_methodology(cls, value: str) -> str:
        if not value or value != value.strip() or len(value) > 16:
            raise ValueError("invalid methodology version")
        return value

    @model_validator(mode="after")
    def validate_inventory(self) -> EvidenceInventory:
        if self.coverage_start > self.coverage_end:
            raise ValueError("coverage dates are reversed")
        if self.captured_at is not None and self.captured_at.utcoffset() is None:
            raise ValueError("captured_at requires a UTC offset")
        if any(not self.coverage_start <= event.date <= self.coverage_end for event in self.events):
            raise ValueError("event outside coverage")
        if any(
            gap.gap_start < self.coverage_start or gap.gap_end > self.coverage_end
            for gap in self.gaps
        ):
            raise ValueError("gap outside coverage")
        ordinals = [event.source_row_ordinal for event in self.events if event.source_row_ordinal]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("duplicate source row ordinal")
        return self


@dataclass(frozen=True)
class SourceRow:
    ordinal: int
    activity_date: date
    process_date: date | None
    settlement_date: date | None
    source_code: str
    signed_amount: Decimal | None
    source_row_sha256: str
    is_cashflow_candidate: bool


@dataclass(frozen=True)
class BuildResult:
    manifest_count: int
    event_count: int
    status_counts: dict[str, int]
    conflict_count: int
    output_digest: str

    def console_summary(self) -> dict[str, object]:
        return {
            "manifest_count": self.manifest_count,
            "event_count": self.event_count,
            "status_counts": dict(self.status_counts),
            "conflict_count": self.conflict_count,
            "output_digest": self.output_digest,
        }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return _sha256(encoded)


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _readonly_engine(database: Path) -> Engine:
    absolute = database.resolve()
    if not absolute.is_file():
        raise BuildError("database_unavailable")
    uri = f"file:{quote(absolute.as_posix(), safe='/:')}?mode=ro"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection

    return create_engine("sqlite://", creator=connect, poolclass=NullPool, future=True)


def _verify_database(session: Session) -> None:
    try:
        query_only = int(session.execute(text("PRAGMA query_only")).scalar_one())
        integrity = list(session.execute(text("PRAGMA integrity_check")).scalars())
        revisions = list(session.execute(text("SELECT version_num FROM alembic_version")).scalars())
    except Exception:
        raise BuildError("database_verification_failed") from None
    if query_only != 1:
        raise BuildError("database_not_readonly")
    if integrity != ["ok"]:
        raise BuildError("database_integrity_failed")
    if revisions != [EXPECTED_ALEMBIC_REVISION]:
        raise BuildError("alembic_revision_mismatch")


def _parse_amount(value: str) -> Decimal:
    normalized = value.strip()
    negative = normalized.startswith("(") and normalized.endswith(")")
    if negative:
        normalized = normalized[1:-1].strip()
    if normalized.startswith("$"):
        normalized = normalized[1:]
    normalized = normalized.replace(",", "")
    try:
        result = Decimal(normalized)
    except InvalidOperation:
        raise BuildError("csv_amount_invalid") from None
    if not result.is_finite():
        raise BuildError("csv_amount_invalid")
    return -result if negative else result


def _parse_optional_date(value: str) -> date | None:
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return datetime.strptime(normalized, "%m/%d/%Y").date()
    except ValueError:
        raise BuildError("csv_date_invalid") from None


def _source_row_sha256(record: Sequence[str]) -> str:
    return _digest({header: value.strip() for header, value in zip(_HEADERS, record, strict=True)})


def _parse_csv(path: Path) -> tuple[str, tuple[SourceRow, ...]]:
    try:
        raw = path.read_bytes()
        decoded = raw.decode("utf-8-sig")
        records = list(csv.reader(io.StringIO(decoded, newline=""), strict=True))
    except (OSError, UnicodeDecodeError, csv.Error):
        raise BuildError("csv_unavailable_or_invalid") from None
    if not records or tuple(records[0]) != _HEADERS:
        raise BuildError("csv_header_unsupported")
    rows: list[SourceRow] = []
    reached_trailer = False
    for record in records[1:]:
        if not record or not record[0].strip():
            reached_trailer = True
            continue
        if reached_trailer or len(record) != len(_HEADERS):
            raise BuildError("csv_row_shape_unsupported")
        activity_date = _parse_optional_date(record[0])
        if activity_date is None:
            raise BuildError("csv_date_invalid")
        process_date = _parse_optional_date(record[1])
        settlement_date = _parse_optional_date(record[2])
        source_code = record[5].strip()
        if not source_code or len(source_code) > 32:
            raise BuildError("csv_source_code_invalid")
        try:
            signed_amount = _parse_amount(record[8])
        except BuildError:
            if source_code in _SUPPORTED_EXTERNAL_CASH_CODES:
                raise
            signed_amount = None
        rows.append(
            SourceRow(
                ordinal=len(rows) + 1,
                activity_date=activity_date,
                process_date=process_date,
                settlement_date=settlement_date,
                source_code=source_code,
                signed_amount=signed_amount,
                source_row_sha256=_source_row_sha256(record),
                is_cashflow_candidate=(
                    source_code in _SUPPORTED_EXTERNAL_CASH_CODES
                    and signed_amount is not None
                    and signed_amount != 0
                ),
            )
        )
    return _sha256(raw), tuple(rows)


def _load_inventory(path: Path) -> EvidenceInventory:
    try:
        return EvidenceInventory.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        raise BuildError("inventory_invalid") from None


def _match_source_rows(
    inventory: EvidenceInventory, rows: tuple[SourceRow, ...]
) -> tuple[tuple[EvidenceEvent, SourceRow], ...]:
    matched: list[tuple[EvidenceEvent, SourceRow]] = []
    used: set[int] = set()
    for event in inventory.events:
        source_amount = (
            event.source_amount if event.source_amount is not None else event.signed_external_amount
        )
        if source_amount is None:
            raise BuildError("source_amount_required")
        candidates = [
            row
            for row in rows
            if row.activity_date == event.date
            and row.signed_amount == source_amount
            and (event.source_code is None or row.source_code == event.source_code)
            and (event.source_row_ordinal is None or row.ordinal == event.source_row_ordinal)
        ]
        if not candidates:
            raise BuildError("source_event_not_found")
        if len(candidates) != 1:
            raise BuildError("source_event_ambiguous")
        selected = candidates[0]
        if selected.ordinal in used:
            raise BuildError("source_row_reused")
        if not selected.is_cashflow_candidate:
            raise BuildError("source_event_not_cashflow_candidate")
        if selected.signed_amount is None:
            raise BuildError("csv_amount_invalid")
        used.add(selected.ordinal)
        matched.append((event, selected))
    candidate_ordinals = {row.ordinal for row in rows if row.is_cashflow_candidate}
    if candidate_ordinals - used:
        raise BuildError("cashflow_candidate_omitted")
    if used - candidate_ordinals:
        raise BuildError("source_event_not_cashflow_candidate")
    return tuple(matched)


def _candidate_transactions(
    session: Session,
    inventory: EvidenceInventory,
    event: EvidenceEvent,
    source_row: SourceRow,
) -> tuple[InvestmentTransaction, ...]:
    match_amount = event.signed_external_amount
    if match_amount is None:
        match_amount = event.source_amount
    if match_amount is None:
        raise BuildError("source_amount_required")
    candidates = tuple(
        session.scalars(
            select(InvestmentTransaction).where(
                InvestmentTransaction.account_id == inventory.account_id,
                InvestmentTransaction.date
                >= source_row.activity_date - timedelta(days=_DATE_SHIFT_DAYS),
                InvestmentTransaction.date
                <= source_row.activity_date + timedelta(days=_DATE_SHIFT_DAYS),
                InvestmentTransaction.currency == "USD",
                InvestmentTransaction.security_id.is_(None),
                InvestmentTransaction.quantity == 0,
                InvestmentTransaction.type.in_(("cash", "transfer")),
            )
        )
    )
    return tuple(
        transaction
        for transaction in candidates
        if abs(Decimal(transaction.amount)) == abs(match_amount)
    )


def _resolve_event(
    session: Session,
    inventory: EvidenceInventory,
    event: EvidenceEvent,
    source_row: SourceRow,
    source_event_id: str,
) -> tuple[str, dict[str, object], date | None, str | None, str | None]:
    if event.disposition in {"internal", "excluded", "unresolved"}:
        effective_date = _inventory_effective_date(event, source_row)
        if event.disposition == "unresolved":
            effective_date = None
        return (
            event.disposition,
            {"kind": "no_transaction"},
            effective_date,
            None if effective_date is None else "source_activity",
            event.assumption_code,
        )

    candidates = _candidate_transactions(session, inventory, event, source_row)
    if len(candidates) > 1:
        raise BuildError("transaction_match_ambiguous")
    if not candidates:
        if event.disposition == "provider_exact":
            raise BuildError("expected_provider_transaction_missing")
        return (
            "statement_supplement",
            {"kind": "manual_transaction"},
            _inventory_effective_date(event, source_row),
            "source_activity",
            event.assumption_code,
        )
    transaction = candidates[0]
    current_decision = session.scalar(
        select(CashFlowReconciliationDecision).where(
            CashFlowReconciliationDecision.source_event_id == source_event_id,
            CashFlowReconciliationDecision.superseded_at.is_(None),
        )
    )
    disposition = "provider_exact"
    if current_decision is not None and current_decision.resolution_kind == "statement_supplement":
        disposition = "provider_supersedes_supplement"
    elif event.disposition == "statement_supplement":
        raise BuildError("supplement_duplicates_provider_transaction")
    override = session.get(TransactionOverride, transaction.plaid_investment_transaction_id)
    current_override = override.classification if override is not None else None
    assumption = event.assumption_code
    if transaction.date != source_row.activity_date:
        assumption = "provider_posting_date_shift"
    return (
        disposition,
        {
            "kind": "existing_transaction",
            "transaction_identity_sha256": transaction_identity_sha256(
                transaction.plaid_investment_transaction_id
            ),
            "expected_transaction_payload_sha256": transaction_payload_sha256(transaction),
            "expected_current_override": current_override,
        },
        source_row.activity_date,
        "source_activity",
        assumption,
    )


def _inventory_effective_date(event: EvidenceEvent, source_row: SourceRow) -> date:
    if (
        event.ledger_effective_date is not None
        and event.ledger_effective_date != source_row.activity_date
    ):
        raise BuildError("statement_effective_date_must_use_activity_date")
    return source_row.activity_date


def _verify_account(session: Session, inventory: EvidenceInventory) -> str:
    account = session.get(Account, inventory.account_id)
    if account is None:
        raise BuildError("account_not_found")
    actual_fingerprint = _sha256(account.plaid_account_id.encode())
    if (
        inventory.account_identity_sha256 is not None
        and actual_fingerprint != inventory.account_identity_sha256
    ):
        raise BuildError("account_identity_mismatch")
    active = session.scalar(
        select(Item.item_id).where(
            Item.item_id == account.item_id,
            Item.is_data_active.is_(True),
        )
    )
    if active is None or account.account_id not in valued_account_ids(session):
        raise BuildError("account_not_active_and_valued")
    return actual_fingerprint


def _captured_at(inventory: EvidenceInventory, csv_path: Path) -> str:
    captured_at = inventory.captured_at
    if captured_at is None:
        try:
            captured_at = datetime.fromtimestamp(csv_path.stat().st_mtime, UTC)
        except OSError:
            raise BuildError("csv_unavailable_or_invalid") from None
    return captured_at.astimezone(UTC).isoformat()


def _build_document(
    session: Session,
    inventory: EvidenceInventory,
    rows: tuple[SourceRow, ...],
    csv_path: Path,
) -> tuple[dict[str, object], Counter[str]]:
    account_identity_sha256 = _verify_account(session, inventory)
    matched = _match_source_rows(inventory, rows)
    counts: Counter[str] = Counter()
    events: list[dict[str, object]] = []
    for event, source_row in matched:
        if source_row.signed_amount is None:
            raise BuildError("csv_amount_invalid")
        source_event_id = _digest(
            {
                "identity_version": "cashflow_source_row.v2",
                "source_document_sha256": inventory.source_document_sha256,
                "account_identity_sha256": account_identity_sha256,
                "source_row_ordinal": source_row.ordinal,
                "source_row_sha256": source_row.source_row_sha256,
            }
        )
        disposition, resolution, effective_date, date_basis, assumption_code = _resolve_event(
            session,
            inventory,
            event,
            source_row,
            source_event_id,
        )
        counts[
            "provider_exact" if disposition == "provider_supersedes_supplement" else disposition
        ] += 1
        classification = event.classification
        signed_external_amount = event.signed_external_amount
        events.append(
            {
                "source_row_ordinal": source_row.ordinal,
                "source_row_sha256": source_row.source_row_sha256,
                "activity_date": source_row.activity_date.isoformat(),
                "process_date": (
                    source_row.process_date.isoformat()
                    if source_row.process_date is not None
                    else None
                ),
                "settlement_date": (
                    source_row.settlement_date.isoformat()
                    if source_row.settlement_date is not None
                    else None
                ),
                "source_amount": _decimal_text(source_row.signed_amount),
                "source_amount_sign_basis": "statement_printed",
                "date": source_row.activity_date.isoformat(),
                "signed_external_amount": (
                    _decimal_text(signed_external_amount)
                    if signed_external_amount is not None
                    else None
                ),
                "classification": classification,
                "source_code": source_row.source_code,
                "currency": "USD",
                "disposition": disposition,
                "ledger_effective_date": (
                    effective_date.isoformat() if effective_date is not None else None
                ),
                "effective_date_basis": date_basis,
                "effective_timezone": (
                    None if disposition == "unresolved" else event.effective_timezone
                ),
                "confidence": event.confidence,
                "assumption_code": assumption_code,
                "decision_authority": event.decision_authority,
                "resolution": resolution,
            }
        )
    unresolved_dates: set[str] = {
        cast(str, event["activity_date"])
        for event in events
        if event["disposition"] == "unresolved"
    }
    gaps = [
        {
            "gap_start": gap.gap_start.isoformat(),
            "gap_end": gap.gap_end.isoformat(),
            "reason_code": gap.reason_code,
        }
        for gap in inventory.gaps
    ]
    gaps.extend(
        {
            "gap_start": event_date,
            "gap_end": event_date,
            "reason_code": "unresolved_classification",
        }
        for event_date in sorted(unresolved_dates)
        if not any(
            gap["gap_start"] <= event_date <= gap["gap_end"]
            and gap["reason_code"] == "unresolved_classification"
            for gap in gaps
        )
    )
    candidate_hashes = sorted(row.source_row_sha256 for row in rows if row.is_cashflow_candidate)
    document: dict[str, object] = {
        "schema_version": "2",
        "account_id": inventory.account_id,
        "account_identity_sha256": account_identity_sha256,
        "account_mapping_basis": "provider_account_id",
        "account_mapping_confidence": "exact",
        "coverage_start": inventory.coverage_start.isoformat(),
        "coverage_end": inventory.coverage_end.isoformat(),
        "source_type": inventory.source_type,
        "source_reference": (f"private:{inventory.source_type}:{inventory.source_document_sha256}"),
        "source_document_sha256": inventory.source_document_sha256,
        "captured_at": _captured_at(inventory, csv_path),
        "methodology_version": inventory.methodology_version,
        "source_format": "robinhood_activity_csv",
        "parser_version": PARSER_VERSION,
        "source_timezone": SOURCE_TIMEZONE,
        "source_row_count": len(rows),
        "cashflow_candidate_count": len(candidate_hashes),
        "source_event_set_sha256": _digest(candidate_hashes),
        "gaps": gaps,
        "events": events,
    }
    return document, counts


def _write_staged(path: Path, payload: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _cleanup_stage(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_file():
            child.unlink()
    path.rmdir()


def build_execution_manifests(
    database: Path,
    inventory_paths: Sequence[Path],
    csv_paths: Sequence[Path],
    output_directory: Path,
) -> BuildResult:
    """Build and atomically publish explicit manifests from read-only inputs."""
    if not inventory_paths or not csv_paths:
        raise BuildError("inputs_required")
    output_directory = Path(output_directory)
    if output_directory.exists():
        raise BuildError("output_directory_exists")
    inventories = tuple(_load_inventory(Path(path)) for path in inventory_paths)
    parsed_csvs = tuple(_parse_csv(Path(path)) for path in csv_paths)
    csv_by_hash: dict[str, tuple[Path, tuple[SourceRow, ...]]] = {}
    for path, (source_hash, rows) in zip(csv_paths, parsed_csvs, strict=True):
        if source_hash in csv_by_hash:
            raise BuildError("duplicate_csv_source")
        csv_by_hash[source_hash] = Path(path), rows

    engine = _readonly_engine(Path(database))
    documents: list[tuple[str, dict[str, object], Path]] = []
    total_counts: Counter[str] = Counter()
    try:
        with Session(engine) as session:
            _verify_database(session)
            for inventory in inventories:
                matched_csv = csv_by_hash.get(inventory.source_document_sha256)
                if matched_csv is None:
                    raise BuildError("source_hash_mismatch")
                csv_path, rows = matched_csv
                document, counts = _build_document(session, inventory, rows, csv_path)
                total_counts.update(counts)
                file_token = _digest(
                    {
                        "account": document["account_identity_sha256"],
                        "source": inventory.source_document_sha256,
                    }
                )[:20]
                documents.append((f"cashflow-execution-{file_token}.json", document, csv_path))

            if len({name for name, _document, _csv in documents}) != len(documents):
                raise BuildError("output_name_collision")

            output_directory.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(
                tempfile.mkdtemp(prefix=f".{output_directory.name}.", dir=output_directory.parent)
            )
            os.chmod(stage, 0o700)
            try:
                sources: list[ManifestSource] = []
                for filename, document, csv_path in documents:
                    destination = stage / filename
                    _write_staged(destination, document)
                    sources.append(ManifestSource(destination, csv_path))
                writer_plan = build_reconciliation_plan(session, sources)
                if writer_plan.conflict_count:
                    raise BuildError("writer_contract_conflict")
                os.rename(stage, output_directory)
            except Exception:
                _cleanup_stage(stage)
                raise
    finally:
        engine.dispose()

    manifest_digests = [
        _sha256(path.read_bytes()) for path in sorted(output_directory.glob("*.json"))
    ]
    status_counts = {status: total_counts[status] for status in _STATUS_ORDER}
    return BuildResult(
        manifest_count=len(documents),
        event_count=sum(status_counts.values()),
        status_counts=status_counts,
        conflict_count=0,
        output_digest=f"sha256:{_digest(manifest_digests)}",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, action="append", required=True)
    parser.add_argument("--csv", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_execution_manifests(args.db, args.inventory, args.csv, args.output_dir)
        print(json.dumps(result.console_summary(), sort_keys=True))
        return 0
    except BuildError as exc:
        print(
            json.dumps(
                {
                    "error_count": 1,
                    "error_digest": f"sha256:{_sha256(exc.code.encode())}",
                },
                sort_keys=True,
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "error_count": 1,
                    "error_digest": f"sha256:{_sha256(b'internal_error')}",
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
