"""Preview and import broker-reported historical account totals.

The manifest is an owner-reviewed claim about one account and one immutable
statement or provider export.  This job never derives a total from holdings or
transactions.  Preview is read-only; apply requires the exact preview digest,
an unchanged manifest/evidence pair, an unchanged database state, and a
verified SQLite backup of that state.

Manifest format (``account_valuation_manifest.v1``)::

    {
      "schema_version": "account_valuation_manifest.v1",
      "account_id": 1,
      "account_identity_sha256": "...",
      "account_mapping_basis": "provider_account_id",
      "account_mapping_confidence": "exact",
      "source_kind": "brokerage_statement",
      "source_provider": "broker_slug",
      "source_reference": "2025-12 monthly statement",
      "source_document_sha256": "...",
      "captured_at": "2026-09-03T19:00:00Z",
      "source_timezone": "America/New_York",
      "source_row_count": 1,
      "rows": [{
        "source_locator": "page=1;field=ending_account_value",
        "source_value_sha256": "...",
        "as_of_date": "2025-12-31",
        "as_of_at": null,
        "total_value": "100000.00",
        "cash_value": "5000.00",
        "currency": "USD",
        "is_complete": true,
        "is_empty": false
      }]
    }

``as_of_at`` is deliberately nullable for statements that report only a date;
the importer preserves that limitation rather than inventing a timestamp.
Manifest and preview artifacts contain private financial data and are written
with owner-only permissions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import (
    INVESTMENT_ACCOUNT_TYPES,
    Account,
    AccountValuationObservation,
    AccountValuationSourceKind,
    Item,
)
from portfolio_tracker.services.account_valuations import (
    NewAccountValuationObservation,
    account_valuation_observation_key,
    account_valuation_observation_payload,
    record_account_valuation_observation,
    stored_account_valuation_observation_payload,
)

EntryStatus = Literal["existing_exact", "missing_insert", "conflict"]

_SCHEMA_VERSION = "account_valuation_manifest.v1"
_PREVIEW_VERSION = "account_valuation_import_preview.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_TOP_KEYS = frozenset(
    {
        "schema_version",
        "account_id",
        "account_identity_sha256",
        "account_mapping_basis",
        "account_mapping_confidence",
        "source_kind",
        "source_provider",
        "source_reference",
        "source_document_sha256",
        "captured_at",
        "source_timezone",
        "source_row_count",
        "rows",
    }
)
_ROW_KEYS = frozenset(
    {
        "source_locator",
        "source_value_sha256",
        "as_of_date",
        "as_of_at",
        "total_value",
        "cash_value",
        "currency",
        "is_complete",
        "is_empty",
    }
)
_MAPPING_BASES = frozenset(
    {"provider_account_id", "statement_account_identifier", "owner_confirmed"}
)
_MAPPING_CONFIDENCES = frozenset({"exact", "high"})
_SOURCE_KINDS = frozenset(
    {
        AccountValuationSourceKind.BROKERAGE_STATEMENT.value,
        AccountValuationSourceKind.PROVIDER_EXPORT.value,
    }
)


class ValuationManifestValidationError(ValueError):
    """The manifest, evidence, mapping, or preview is invalid."""


class ValuationImportConflictError(RuntimeError):
    """The approved plan no longer matches durable state."""


@dataclass(frozen=True, slots=True)
class ValuationImportEntry:
    status: EntryStatus
    source_locator: str
    source_value_sha256: str
    source_record_id: str
    observation_key: str
    value: NewAccountValuationObservation

    def digest_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "source_locator": self.source_locator,
            "source_value_sha256": self.source_value_sha256,
            "source_record_id": self.source_record_id,
            "observation_key": self.observation_key,
            "as_of_date": self.value.as_of_date.isoformat(),
            "as_of_at": _datetime_text(self.value.as_of_at),
            "total_value": _decimal_text(self.value.total_value),
            "cash_value": _decimal_text(self.value.cash_value),
            "currency": self.value.currency,
            "is_complete": self.value.is_complete,
            "is_empty": self.value.is_empty,
        }


@dataclass(frozen=True, slots=True)
class AccountValuationImportPlan:
    manifest_path: Path
    source_path: Path
    manifest_sha256: str
    source_document_sha256: str
    account_id: int
    account_identity_sha256: str
    database_state_sha256: str
    entries: tuple[ValuationImportEntry, ...]
    plan_digest: str

    @property
    def missing_insert_count(self) -> int:
        return sum(entry.status == "missing_insert" for entry in self.entries)

    @property
    def existing_exact_count(self) -> int:
        return sum(entry.status == "existing_exact" for entry in self.entries)

    @property
    def conflict_count(self) -> int:
        return sum(entry.status == "conflict" for entry in self.entries)


def _require_exact_keys(value: dict[str, object], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValuationManifestValidationError(
            f"{label} keys must match schema exactly; missing={missing}, extra={extra}"
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _required_string(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ValuationManifestValidationError(
            f"{label} must be a non-blank string of at most {maximum} characters"
        )
    return value


def _parse_datetime(value: object, label: str) -> datetime:
    raw = _required_string(value, label, maximum=64)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValuationManifestValidationError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValuationManifestValidationError(f"{label} must include an explicit UTC offset")
    return parsed


def _parse_date(value: object, label: str) -> date:
    raw = _required_string(value, label, maximum=10)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValuationManifestValidationError(f"{label} must be an ISO-8601 date") from exc


def _parse_decimal(value: object, label: str, *, nullable: bool = False) -> Decimal | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
        raise ValuationManifestValidationError(f"{label} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValuationManifestValidationError(f"{label} must be a decimal string") from exc
    if not parsed.is_finite():
        raise ValuationManifestValidationError(f"{label} must be finite")
    return parsed


def _account_identity_payload(
    *, account_id: int, item_id: int, provider_account_id: str, item_source: str
) -> dict[str, object]:
    return {
        "account_id": account_id,
        "item_id": item_id,
        "provider_account_id": provider_account_id,
        "item_source": item_source,
    }


def _account_state_payload(
    *,
    account_id: int,
    item_id: int,
    provider_account_id: str,
    item_source: str,
    account_type: str,
    currency: str,
    is_data_active: bool,
) -> dict[str, object]:
    return {
        **_account_identity_payload(
            account_id=account_id,
            item_id=item_id,
            provider_account_id=provider_account_id,
            item_source=item_source,
        ),
        "account_type": account_type,
        "currency": currency,
        "is_data_active": is_data_active,
    }


def canonical_account_identity_sha256(account: Account, item: Item) -> str:
    """Commit to the local account and its stable aggregator identity."""
    return _json_sha256(
        _account_identity_payload(
            account_id=account.account_id,
            item_id=account.item_id,
            provider_account_id=account.plaid_account_id,
            item_source=item.source,
        )
    )


def canonical_manifest_value_sha256(
    *,
    source_document_sha256: str,
    source_locator: str,
    as_of_date: date,
    as_of_at: datetime | None,
    total_value: Decimal,
    cash_value: Decimal | None,
    currency: str,
    is_complete: bool,
    is_empty: bool,
) -> str:
    """Commit to the exact source location and interpreted broker total."""
    return _json_sha256(
        {
            "source_document_sha256": source_document_sha256,
            "source_locator": source_locator,
            "as_of_date": as_of_date.isoformat(),
            "as_of_at": _datetime_text(as_of_at),
            "total_value": _decimal_text(total_value),
            "cash_value": _decimal_text(cash_value),
            "currency": currency,
            "is_complete": is_complete,
            "is_empty": is_empty,
        }
    )


def _source_record_id(document_sha256: str, locator: str) -> str:
    return f"sha256:{document_sha256}#locator:{locator}"


def _database_state_sha256(session: Session, account: Account, item: Item) -> str:
    observations = tuple(
        session.scalars(
            select(AccountValuationObservation)
            .where(AccountValuationObservation.account_id == account.account_id)
            .order_by(AccountValuationObservation.valuation_observation_id)
        ).all()
    )
    try:
        rows = tuple(
            {
                "valuation_observation_id": observation.valuation_observation_id,
                "observation_key": observation.observation_key,
                "payload": stored_account_valuation_observation_payload(observation),
            }
            for observation in observations
        )
    except (TypeError, ValueError) as exc:
        raise ValuationImportConflictError(
            "stored account valuation failed canonical integrity validation"
        ) from exc
    return _json_sha256(
        {
            "account": _account_state_payload(
                account_id=account.account_id,
                item_id=account.item_id,
                provider_account_id=account.plaid_account_id,
                item_source=item.source,
                account_type=account.type,
                currency=account.currency,
                is_data_active=item.is_data_active,
            ),
            "account_valuation_observations": rows,
        }
    )


def _read_manifest(path: Path) -> tuple[dict[str, object], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValuationManifestValidationError("manifest file cannot be read") from exc
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValuationManifestValidationError("manifest must be valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValuationManifestValidationError("manifest must be a JSON object")
    manifest = cast(dict[str, object], parsed)
    _require_exact_keys(manifest, _TOP_KEYS, "manifest")
    return manifest, hashlib.sha256(raw).hexdigest()


def _build_entries(
    session: Session,
    *,
    manifest: dict[str, object],
    source_document_sha256: str,
    account: Account,
) -> tuple[ValuationImportEntry, ...]:
    source_kind_raw = _required_string(manifest["source_kind"], "source_kind", maximum=32)
    if source_kind_raw not in _SOURCE_KINDS:
        raise ValuationManifestValidationError(
            "source_kind must be brokerage_statement or provider_export"
        )
    source_kind = AccountValuationSourceKind(source_kind_raw)
    source_provider = _required_string(manifest["source_provider"], "source_provider", maximum=64)
    source_reference = _required_string(
        manifest["source_reference"], "source_reference", maximum=160
    )
    basis = _required_string(manifest["account_mapping_basis"], "account_mapping_basis", maximum=40)
    confidence = _required_string(
        manifest["account_mapping_confidence"], "account_mapping_confidence", maximum=16
    )
    if basis not in _MAPPING_BASES:
        raise ValuationManifestValidationError("unsupported account_mapping_basis")
    if confidence not in _MAPPING_CONFIDENCES:
        raise ValuationManifestValidationError(
            "account_mapping_confidence must be exact or high; provisional mappings cannot apply"
        )
    timezone_name = _required_string(manifest["source_timezone"], "source_timezone", maximum=64)
    try:
        source_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValuationManifestValidationError(
            "source_timezone is not a known IANA timezone"
        ) from exc
    captured_at = _parse_datetime(manifest["captured_at"], "captured_at")

    raw_rows = manifest["rows"]
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValuationManifestValidationError("rows must be a non-empty JSON array")
    rows = cast(list[object], raw_rows)
    row_count = manifest["source_row_count"]
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count != len(rows):
        raise ValuationManifestValidationError("source_row_count must equal the exact rows length")

    entries: list[ValuationImportEntry] = []
    locators: set[str] = set()
    boundaries: set[tuple[date, str]] = set()
    for ordinal, raw_row in enumerate(rows, start=1):
        if not isinstance(raw_row, dict):
            raise ValuationManifestValidationError(f"row {ordinal} must be a JSON object")
        row = cast(dict[str, object], raw_row)
        _require_exact_keys(row, _ROW_KEYS, f"row {ordinal}")
        locator = _required_string(
            row["source_locator"], f"row {ordinal}.source_locator", maximum=96
        )
        if locator in locators:
            raise ValuationManifestValidationError("source_locator values must be unique")
        locators.add(locator)
        as_of_date = _parse_date(row["as_of_date"], f"row {ordinal}.as_of_date")
        as_of_at_raw = row["as_of_at"]
        as_of_at = (
            None
            if as_of_at_raw is None
            else _parse_datetime(as_of_at_raw, f"row {ordinal}.as_of_at")
        )
        if as_of_at is not None and as_of_at.astimezone(source_timezone).date() != as_of_date:
            raise ValuationManifestValidationError(
                f"row {ordinal}.as_of_at does not fall on as_of_date in source_timezone"
            )
        if as_of_at is None and captured_at.astimezone(source_timezone).date() < as_of_date:
            raise ValuationManifestValidationError(
                f"row {ordinal}.captured_at cannot precede a date-only as_of_date"
            )
        total_value = _parse_decimal(row["total_value"], f"row {ordinal}.total_value")
        cash_value = _parse_decimal(row["cash_value"], f"row {ordinal}.cash_value", nullable=True)
        assert total_value is not None
        currency = _required_string(row["currency"], f"row {ordinal}.currency", maximum=3)
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValuationManifestValidationError("row currency must be three uppercase letters")
        if currency != account.currency:
            raise ValuationManifestValidationError(
                f"row {ordinal}.currency does not match the mapped account currency"
            )
        boundary = (as_of_date, currency)
        if boundary in boundaries:
            raise ValuationManifestValidationError(
                "a manifest may contain only one account total per date and currency"
            )
        boundaries.add(boundary)
        is_complete = row["is_complete"]
        is_empty = row["is_empty"]
        if not isinstance(is_complete, bool) or not isinstance(is_empty, bool):
            raise ValuationManifestValidationError("is_complete and is_empty must be booleans")
        expected_value_sha = canonical_manifest_value_sha256(
            source_document_sha256=source_document_sha256,
            source_locator=locator,
            as_of_date=as_of_date,
            as_of_at=as_of_at,
            total_value=total_value,
            cash_value=cash_value,
            currency=currency,
            is_complete=is_complete,
            is_empty=is_empty,
        )
        supplied_value_sha = _required_string(
            row["source_value_sha256"], f"row {ordinal}.source_value_sha256", maximum=64
        )
        if not _SHA256_RE.fullmatch(supplied_value_sha) or supplied_value_sha != expected_value_sha:
            raise ValuationManifestValidationError(
                f"row {ordinal}.source_value_sha256 does not match normalized source value"
            )
        source_record_id = _source_record_id(source_document_sha256, locator)
        persisted_reference = (
            f"{source_reference}#locator={locator};mapping={basis}/{confidence};"
            f"timezone={timezone_name}"
        )
        value = NewAccountValuationObservation(
            account_id=account.account_id,
            as_of_date=as_of_date,
            as_of_at=as_of_at,
            total_value=total_value,
            cash_value=cash_value,
            currency=currency,
            source_kind=source_kind,
            source_provider=source_provider,
            source_reference=persisted_reference,
            source_record_id=source_record_id,
            source_payload_sha256=supplied_value_sha,
            fetched_at=captured_at,
            is_complete=is_complete,
            is_empty=is_empty,
        )
        try:
            observation_key = account_valuation_observation_key(value)
        except ValueError as exc:
            raise ValuationManifestValidationError(f"row {ordinal}: {exc}") from exc
        exact = session.scalar(
            select(AccountValuationObservation).where(
                AccountValuationObservation.observation_key == observation_key
            )
        )
        same_source = tuple(
            session.scalars(
                select(AccountValuationObservation).where(
                    AccountValuationObservation.account_id == account.account_id,
                    AccountValuationObservation.source_record_id == source_record_id,
                )
            ).all()
        )
        status: EntryStatus
        if exact is not None:
            status = "existing_exact"
        elif any(
            row.source_payload_sha256 != supplied_value_sha
            or row.source_kind != source_kind.value
            or row.source_provider != source_provider
            for row in same_source
        ):
            status = "conflict"
        else:
            status = "missing_insert"
        entries.append(
            ValuationImportEntry(
                status=status,
                source_locator=locator,
                source_value_sha256=supplied_value_sha,
                source_record_id=source_record_id,
                observation_key=observation_key,
                value=value,
            )
        )
    return tuple(entries)


def build_account_valuation_import_plan(
    session: Session, *, manifest_path: Path, source_path: Path
) -> AccountValuationImportPlan:
    """Build a deterministic, read-only import plan."""
    manifest, manifest_sha256 = _read_manifest(manifest_path)
    if manifest["schema_version"] != _SCHEMA_VERSION:
        raise ValuationManifestValidationError(f"schema_version must be {_SCHEMA_VERSION}")
    document_sha = _required_string(
        manifest["source_document_sha256"], "source_document_sha256", maximum=64
    )
    if not _SHA256_RE.fullmatch(document_sha) or _file_sha256(source_path) != document_sha:
        raise ValuationManifestValidationError(
            "source_document_sha256 does not match the exact evidence file"
        )
    account_id_raw = manifest["account_id"]
    if (
        isinstance(account_id_raw, bool)
        or not isinstance(account_id_raw, int)
        or account_id_raw <= 0
    ):
        raise ValuationManifestValidationError("account_id must be a positive integer")
    account_and_item = session.execute(
        select(Account, Item)
        .join(Item, Item.item_id == Account.item_id)
        .where(Account.account_id == account_id_raw)
    ).one_or_none()
    if account_and_item is None:
        raise ValuationManifestValidationError("mapped account does not exist")
    account, item = account_and_item
    if account.type not in INVESTMENT_ACCOUNT_TYPES or not item.is_data_active:
        raise ValuationManifestValidationError(
            "mapped account must be an active investment account"
        )
    actual_identity = canonical_account_identity_sha256(account, item)
    supplied_identity = _required_string(
        manifest["account_identity_sha256"], "account_identity_sha256", maximum=64
    )
    if not _SHA256_RE.fullmatch(supplied_identity) or supplied_identity != actual_identity:
        raise ValuationManifestValidationError(
            "account_identity_sha256 does not match the mapped account"
        )
    entries = _build_entries(
        session,
        manifest=manifest,
        source_document_sha256=document_sha,
        account=account,
    )
    state_sha = _database_state_sha256(session, account, item)
    digest_payload = {
        "preview_schema_version": _PREVIEW_VERSION,
        "manifest_sha256": manifest_sha256,
        "source_document_sha256": document_sha,
        "account_id": account.account_id,
        "account_identity_sha256": actual_identity,
        "database_state_sha256": state_sha,
        "entries": [entry.digest_payload() for entry in entries],
    }
    return AccountValuationImportPlan(
        manifest_path=manifest_path,
        source_path=source_path,
        manifest_sha256=manifest_sha256,
        source_document_sha256=document_sha,
        account_id=account.account_id,
        account_identity_sha256=actual_identity,
        database_state_sha256=state_sha,
        entries=entries,
        plan_digest=_json_sha256(digest_payload),
    )


def _preview_payload(plan: AccountValuationImportPlan) -> dict[str, object]:
    return {
        "preview_schema_version": _PREVIEW_VERSION,
        "plan_digest": plan.plan_digest,
        "manifest_sha256": plan.manifest_sha256,
        "source_document_sha256": plan.source_document_sha256,
        "account_id": plan.account_id,
        "account_identity_sha256": plan.account_identity_sha256,
        "database_state_sha256": plan.database_state_sha256,
        "source_row_count": len(plan.entries),
        "missing_insert_count": plan.missing_insert_count,
        "existing_exact_count": plan.existing_exact_count,
        "conflict_count": plan.conflict_count,
        "entries": [entry.digest_payload() for entry in plan.entries],
    }


def write_account_valuation_import_preview(
    plan: AccountValuationImportPlan, preview_path: Path
) -> None:
    """Atomically write the exact private plan with owner-only permissions."""
    if _paths_alias(preview_path, plan.manifest_path) or _paths_alias(
        preview_path, plan.source_path
    ):
        raise ValuationManifestValidationError(
            "preview path must not alias the manifest or source evidence"
        )
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{preview_path.name}.", dir=preview_path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(_preview_payload(plan), output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, preview_path)
        os.chmod(preview_path, 0o600)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def _paths_alias(first: Path, second: Path) -> bool:
    if first.resolve(strict=False) == second.resolve(strict=False):
        return True
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def _live_sqlite_path(session: Session) -> Path:
    rows = session.execute(text("PRAGMA database_list")).all()
    for _, name, filename in rows:
        if name == "main" and filename:
            return Path(cast(str, filename)).resolve()
    raise ValuationImportConflictError("apply requires a file-backed SQLite database")


def _backup_state_sha256(backup_path: Path, account_id: int) -> str:
    try:
        connection = sqlite3.connect(f"file:{backup_path.resolve()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ValuationImportConflictError("backup is not a readable SQLite database") from exc
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise ValuationImportConflictError("backup failed SQLite integrity_check")
        row = connection.execute(
            """
            SELECT a.account_id, a.item_id, a.plaid_account_id, i.source,
                   a.type, a.currency, i.is_data_active
            FROM accounts AS a
            JOIN items AS i ON i.item_id = a.item_id
            WHERE a.account_id = ?
            """,
            (account_id,),
        ).fetchone()
        if row is None:
            raise ValuationImportConflictError("backup does not contain the mapped account")
        raw_rows = connection.execute(
            """
                SELECT valuation_observation_id, observation_key, account_id,
                       as_of_date, as_of_at, total_value, cash_value, currency,
                       source_kind, source_provider, source_reference,
                       source_record_id, source_payload_sha256,
                       normalization_version, fetched_at, is_complete, is_empty
                FROM account_valuation_observations
                WHERE account_id = ?
                ORDER BY valuation_observation_id
                """,
            (account_id,),
        ).fetchall()
        canonical_rows: list[dict[str, object]] = []
        for raw in raw_rows:
            as_of_at = _sqlite_datetime(raw[4])
            fetched_at = _sqlite_datetime(raw[14])
            if fetched_at is None:
                raise ValuationImportConflictError("backup valuation has no fetched_at")
            value = NewAccountValuationObservation(
                account_id=cast(int, raw[2]),
                as_of_date=date.fromisoformat(cast(str, raw[3])),
                as_of_at=as_of_at,
                total_value=Decimal(str(raw[5])),
                cash_value=None if raw[6] is None else Decimal(str(raw[6])),
                currency=cast(str, raw[7]),
                source_kind=AccountValuationSourceKind(cast(str, raw[8])),
                source_provider=cast(str, raw[9]),
                source_reference=cast(str, raw[10]),
                source_record_id=cast(str | None, raw[11]),
                source_payload_sha256=cast(str | None, raw[12]),
                fetched_at=fetched_at,
                is_complete=bool(raw[15]),
                is_empty=bool(raw[16]),
            )
            if raw[13] != "1":
                raise ValuationImportConflictError(
                    "backup valuation uses an unsupported normalization version"
                )
            payload = account_valuation_observation_payload(value)
            key = cast(str, raw[1])
            if account_valuation_observation_key(value) != key:
                raise ValuationImportConflictError(
                    "backup valuation payload does not match observation_key"
                )
            canonical_rows.append(
                {
                    "valuation_observation_id": cast(int, raw[0]),
                    "observation_key": key,
                    "payload": payload,
                }
            )
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise ValuationImportConflictError(
            "backup lacks the required account-valuation recovery schema"
        ) from exc
    finally:
        connection.close()
    return _json_sha256(
        {
            "account": _account_state_payload(
                account_id=cast(int, row[0]),
                item_id=cast(int, row[1]),
                provider_account_id=cast(str, row[2]),
                item_source=cast(str, row[3]),
                account_type=cast(str, row[4]),
                currency=cast(str, row[5]),
                is_data_active=bool(row[6]),
            ),
            "account_valuation_observations": canonical_rows,
        }
    )


def _sqlite_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValuationImportConflictError("backup valuation timestamp is not text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValuationImportConflictError("backup valuation timestamp is invalid") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _validate_exact_preview(plan: AccountValuationImportPlan, preview_path: Path) -> None:
    try:
        parsed = json.loads(preview_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValuationImportConflictError("approved preview is not readable valid JSON") from exc
    if parsed != _preview_payload(plan):
        raise ValuationImportConflictError("approved preview does not match the current exact plan")


def apply_account_valuation_import_plan(
    session: Session,
    *,
    manifest_path: Path,
    source_path: Path,
    preview_path: Path,
    backup_path: Path,
    expected_plan_digest: str,
) -> int:
    """Apply only an unchanged, conflict-free plan backed by a restorable DB."""
    if _paths_alias(preview_path, manifest_path) or _paths_alias(preview_path, source_path):
        raise ValuationImportConflictError(
            "preview path must not alias the manifest or source evidence"
        )
    if not _SHA256_RE.fullmatch(expected_plan_digest):
        raise ValuationImportConflictError("expected_plan_digest must be a lowercase SHA-256")
    session.rollback()
    try:
        session.execute(text("BEGIN IMMEDIATE"))
        plan = build_account_valuation_import_plan(
            session, manifest_path=manifest_path, source_path=source_path
        )
        if plan.plan_digest != expected_plan_digest:
            raise ValuationImportConflictError("current plan does not match expected_plan_digest")
        if plan.conflict_count:
            raise ValuationImportConflictError("plan contains source identity conflicts")
        _validate_exact_preview(plan, preview_path)
        live_path = _live_sqlite_path(session)
        if backup_path.resolve() == live_path:
            raise ValuationImportConflictError("backup must be distinct from the live database")
        if _backup_state_sha256(backup_path, plan.account_id) != plan.database_state_sha256:
            raise ValuationImportConflictError(
                "backup does not match the exact pre-import account-valuation state"
            )
        created = 0
        for entry in plan.entries:
            if entry.status != "missing_insert":
                continue
            result = record_account_valuation_observation(session, entry.value)
            if not result.created:
                raise ValuationImportConflictError("planned insert ceased to be new")
            created += 1
        expected_keys = [entry.observation_key for entry in plan.entries]
        persisted = set(
            session.scalars(
                select(AccountValuationObservation.observation_key).where(
                    AccountValuationObservation.observation_key.in_(expected_keys)
                )
            ).all()
        )
        if persisted != set(expected_keys) or created != plan.missing_insert_count:
            raise ValuationImportConflictError("post-write row-count verification failed")
        session.commit()
        return created
    except BaseException:
        session.rollback()
        raise


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--backup", type=Path)
    return parser


def main() -> int:
    args = _build_argparser().parse_args()
    with SessionLocal() as session:
        plan = build_account_valuation_import_plan(
            session, manifest_path=args.manifest, source_path=args.source
        )
        if not args.commit:
            write_account_valuation_import_preview(plan, args.preview)
            print(
                f"DRY RUN rows={len(plan.entries)} inserts={plan.missing_insert_count} "
                f"existing={plan.existing_exact_count} conflicts={plan.conflict_count}"
            )
            print(f"plan_digest={plan.plan_digest}")
            return 0
        if args.expected_plan_digest is None or args.backup is None:
            raise SystemExit("--commit requires --expected-plan-digest and --backup")
        created = apply_account_valuation_import_plan(
            session,
            manifest_path=args.manifest,
            source_path=args.source,
            preview_path=args.preview,
            backup_path=args.backup,
            expected_plan_digest=args.expected_plan_digest,
        )
        print(f"Imported {created} account valuation observation rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
