"""Safely reconcile owner-approved external-cashflow manifests.

This job is deliberately a two-step writer.  ``build_reconciliation_plan`` is
read-only and produces a deterministic digest.  ``apply_reconciliation_plan``
requires that exact digest, revalidates every source and database precondition
under a SQLite write lock, then commits transactions, overrides, attestations,
and gaps as one unit.

Manifest contents are private, untrusted data.  The CLI and preview artifact
therefore expose only counts, stable opaque hashes, statuses, and reason codes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    CashFlowSourceAttestation,
    CashFlowSourceGap,
    InvestmentTransaction,
    Item,
    TransactionOverride,
)
from portfolio_tracker.services.active_items import valued_account_ids
from portfolio_tracker.services.external_flow_ledger import (
    classify_transaction_cashflow,
)

Classification = Literal["external_in", "external_out"]
EntryStatus = Literal[
    "existing_exact",
    "override_required",
    "missing_insert",
    "conflict",
    "excluded",
]
ResolutionKind = Literal["existing_transaction", "manual_transaction"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_CLASSIFICATIONS: frozenset[str] = frozenset({"external_in", "external_out"})
_SOURCE_TYPES: frozenset[str] = frozenset(
    {"brokerage_statement", "provider_export", "owner_reconciliation"}
)
_GAP_REASONS: frozenset[str] = frozenset(
    {
        "provider_history_unavailable",
        "statement_missing",
        "unresolved_classification",
        "unreconciled_difference",
    }
)
_STATUS_ORDER: tuple[EntryStatus, ...] = (
    "existing_exact",
    "override_required",
    "missing_insert",
    "conflict",
    "excluded",
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "account_id",
        "account_identity_sha256",
        "coverage_start",
        "coverage_end",
        "source_type",
        "source_reference",
        "source_document_sha256",
        "captured_at",
        "methodology_version",
        "gaps",
        "events",
    }
)
_REQUIRED_TOP_LEVEL_KEYS = _TOP_LEVEL_KEYS - {"schema_version"}
_EVENT_KEYS = frozenset(
    {
        "source_row_ordinal",
        "date",
        "signed_external_amount",
        "classification",
        "source_code",
        "currency",
        "resolution",
    }
)
_REQUIRED_EVENT_KEYS = _EVENT_KEYS - {"currency"}
_EXISTING_RESOLUTION_KEYS = frozenset(
    {
        "kind",
        "transaction_id",
        "transaction_identity_sha256",
        "expected_transaction_payload_sha256",
        "expected_current_override",
    }
)
_MANUAL_RESOLUTION_KEYS = frozenset({"kind"})
_GAP_KEYS = frozenset({"gap_start", "gap_end", "reason_code"})
_ROBINHOOD_HEADERS: tuple[str, ...] = (
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


class ManifestValidationError(ValueError):
    """A manifest or its referenced evidence fails deterministic validation."""


class ReconciliationConflictError(RuntimeError):
    """Current database state does not satisfy the approved plan."""


@dataclass(frozen=True)
class ManifestSource:
    """A private manifest paired with the exact evidence file it describes."""

    manifest_path: Path
    source_path: Path


@dataclass(frozen=True)
class _Gap:
    gap_start: date
    gap_end: date
    reason_code: str

    def digest_payload(self) -> dict[str, str]:
        return {
            "gap_start": self.gap_start.isoformat(),
            "gap_end": self.gap_end.isoformat(),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class _SourceRow:
    activity_date: str
    transaction_code: str
    amount: str


@dataclass(frozen=True)
class _Event:
    source_event_id: str
    source_row_ordinal: int
    event_date: date
    signed_external_amount: Decimal
    classification: Classification
    source_code: str
    currency: str
    resolution_kind: ResolutionKind
    transaction_id: str | None
    transaction_identity_sha256: str | None
    expected_transaction_payload_sha256: str | None
    expected_current_override: str | None

    def digest_payload(self) -> dict[str, object]:
        return {
            "source_event_id": self.source_event_id,
            "source_row_ordinal": self.source_row_ordinal,
            "date": self.event_date.isoformat(),
            "signed_external_amount": _decimal_text(self.signed_external_amount),
            "classification": self.classification,
            "source_code": self.source_code,
            "currency": self.currency,
            "resolution_kind": self.resolution_kind,
            "transaction_id_hash": (
                _sha256_text(self.transaction_id) if self.transaction_id is not None else None
            ),
            "transaction_identity_sha256": self.transaction_identity_sha256,
            "expected_transaction_payload_sha256": (self.expected_transaction_payload_sha256),
            "expected_current_override": self.expected_current_override,
        }


@dataclass(frozen=True)
class _Manifest:
    source: ManifestSource
    account_id: int
    account_identity_sha256: str
    coverage_start: date
    coverage_end: date
    source_type: str
    source_reference: str
    source_document_sha256: str
    captured_at: datetime
    methodology_version: str
    gaps: tuple[_Gap, ...]
    events: tuple[_Event, ...]
    attestation_key: str
    manifest_digest: str

    def attestation_payload(self) -> dict[str, object]:
        return {
            "attestation_key": self.attestation_key,
            "account_id": self.account_id,
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
            "source_type": self.source_type,
            "source_reference": self.source_reference,
            "source_sha256": self.source_document_sha256,
            "captured_at": _datetime_text(self.captured_at),
            "methodology_version": self.methodology_version,
            "gaps": [gap.digest_payload() for gap in self.gaps],
        }


@dataclass(frozen=True)
class PlanEntry:
    """One source event's read-only reconciliation disposition."""

    source_event_id: str
    status: EntryStatus
    reason_code: str
    account_id: int
    event: _Event
    resolved_transaction_id: str
    current_transaction_payload_sha256: str | None
    current_override: str | None

    def digest_payload(self) -> dict[str, object]:
        return {
            "source_event_id": self.source_event_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "account_id": self.account_id,
            "event": self.event.digest_payload(),
            "resolved_transaction_id_hash": _sha256_text(self.resolved_transaction_id),
            "current_transaction_payload_sha256": (self.current_transaction_payload_sha256),
            "current_override": self.current_override,
        }


@dataclass(frozen=True)
class _AttestationPlan:
    manifest_digest: str
    status: Literal["existing_exact", "insert_required", "approval_required", "conflict"]
    reason_code: str
    current_payload_sha256: str | None
    planned_mutation_count: int

    def digest_payload(self) -> dict[str, object]:
        return {
            "manifest_digest": self.manifest_digest,
            "status": self.status,
            "reason_code": self.reason_code,
            "current_payload_sha256": self.current_payload_sha256,
            "planned_mutation_count": self.planned_mutation_count,
        }


@dataclass(frozen=True)
class ReconciliationPlan:
    """Immutable dry-run plan whose digest is required for commit."""

    sources: tuple[ManifestSource, ...]
    manifests: tuple[_Manifest, ...]
    entries: tuple[PlanEntry, ...]
    attestations: tuple[_AttestationPlan, ...]
    status_counts: dict[str, int]
    planned_mutation_count: int
    conflict_count: int
    plan_digest: str

    def console_summary(self) -> dict[str, object]:
        """Return only non-sensitive counts and an opaque plan digest."""
        return {
            "committed": False,
            "manifest_count": len(self.manifests),
            "source_event_count": len(self.entries),
            "status_counts": dict(self.status_counts),
            "planned_mutation_count": self.planned_mutation_count,
            "conflict_count": self.conflict_count,
            "plan_digest": self.plan_digest,
        }


@dataclass(frozen=True)
class ReconciliationResult:
    """Sanitized result from one atomic apply."""

    committed: bool
    manifest_count: int
    source_event_count: int
    status_counts: dict[str, int]
    applied_mutation_count: int
    plan_digest: str

    def console_summary(self) -> dict[str, object]:
        return {
            "committed": self.committed,
            "manifest_count": self.manifest_count,
            "source_event_count": self.source_event_count,
            "status_counts": dict(self.status_counts),
            "applied_mutation_count": self.applied_mutation_count,
            "plan_digest": self.plan_digest,
        }


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(payload: str) -> str:
    return _sha256_bytes(payload.encode("utf-8"))


def _digest(payload: object) -> str:
    return _sha256_text(_canonical_json(payload))


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _datetime_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _expect_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{context} must be a JSON object")
    raw_mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw_mapping):
        raise ManifestValidationError(f"{context} must be a JSON object")
    return {cast(str, key): item for key, item in raw_mapping.items()}


def _expect_keys(
    payload: Mapping[str, object],
    *,
    required: frozenset[str],
    allowed: frozenset[str],
    context: str,
) -> None:
    missing = sorted(required - payload.keys())
    unknown = sorted(payload.keys() - allowed)
    if missing:
        raise ManifestValidationError(f"{context} missing required fields: {', '.join(missing)}")
    if unknown:
        raise ManifestValidationError(f"{context} has unknown fields: {', '.join(unknown)}")


def _expect_string(value: object, context: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ManifestValidationError(f"{context} must be a non-empty trimmed string")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ManifestValidationError(f"{context} contains forbidden control characters")
    if maximum is not None and len(value) > maximum:
        raise ManifestValidationError(f"{context} exceeds maximum length")
    return value


def _expect_sha256(value: object, context: str) -> str:
    text_value = _expect_string(value, context)
    if _SHA256_RE.fullmatch(text_value) is None:
        raise ManifestValidationError(f"{context} must be a lowercase SHA-256 digest")
    return text_value


def _expect_date(value: object, context: str) -> date:
    text_value = _expect_string(value, context)
    try:
        parsed = date.fromisoformat(text_value)
    except ValueError as exc:
        raise ManifestValidationError(f"{context} must be an ISO calendar date") from exc
    if parsed.isoformat() != text_value:
        raise ManifestValidationError(f"{context} must use canonical YYYY-MM-DD form")
    return parsed


def _expect_datetime(value: object, context: str) -> datetime:
    text_value = _expect_string(value, context)
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestValidationError(f"{context} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestValidationError(f"{context} must include a UTC offset")
    return parsed.astimezone(UTC)


def _expect_amount(value: object, context: str) -> Decimal:
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise ManifestValidationError(f"{context} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ManifestValidationError(f"{context} must be a finite decimal string") from exc
    if not parsed.is_finite() or parsed == 0:
        raise ManifestValidationError(f"{context} must be finite and non-zero")
    exponent = parsed.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -6 or abs(parsed) >= Decimal("100000000000000"):
        raise ManifestValidationError(f"{context} exceeds database precision")
    return parsed


def _parse_robinhood_csv(source_bytes: bytes) -> tuple[_SourceRow, ...]:
    try:
        decoded = source_bytes.decode("utf-8-sig")
        parsed_rows = list(csv.reader(io.StringIO(decoded, newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ManifestValidationError("evidence source is not a supported UTF-8 CSV") from exc
    if not parsed_rows or tuple(parsed_rows[0]) != _ROBINHOOD_HEADERS:
        raise ManifestValidationError("evidence source has an unsupported CSV header shape")
    source_rows: list[_SourceRow] = []
    reached_trailer = False
    for ordinal, row in enumerate(parsed_rows[1:], start=1):
        if not row or not row[0].strip():
            reached_trailer = True
            continue
        if reached_trailer:
            raise ManifestValidationError("evidence source contains a dated row after its trailer")
        if len(row) != len(_ROBINHOOD_HEADERS):
            raise ManifestValidationError(
                f"evidence source row {ordinal} has an unsupported CSV shape"
            )
        _parse_robinhood_date(row[0], f"evidence source row {ordinal}")
        source_rows.append(_SourceRow(row[0], row[5], row[8]))
    return tuple(source_rows)


def _parse_robinhood_date(value: str, context: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date()
    except ValueError as exc:
        raise ManifestValidationError(f"{context} has an invalid Activity Date") from exc


def _parse_robinhood_amount(value: str, context: str) -> Decimal:
    normalized = value.strip()
    negative = normalized.startswith("(") and normalized.endswith(")")
    if negative:
        normalized = normalized[1:-1].strip()
    if normalized.startswith("$"):
        normalized = normalized[1:]
    normalized = normalized.replace(",", "")
    if _DECIMAL_RE.fullmatch(normalized) is None:
        raise ManifestValidationError(f"{context} has an invalid Amount")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ManifestValidationError(f"{context} has an invalid Amount") from exc
    if negative:
        amount = -amount
    if not amount.is_finite():
        raise ManifestValidationError(f"{context} has an invalid Amount")
    return amount


def _account_identity_sha256(account: Account) -> str:
    return _sha256_text(account.plaid_account_id)


def transaction_identity_sha256(transaction_id: str) -> str:
    """Return the opaque exact identity accepted by existing-row resolutions."""
    return _sha256_text(transaction_id)


def transaction_payload_sha256(transaction: InvestmentTransaction) -> str:
    """Hash every persisted business field used to guard an existing row."""
    return _digest(
        {
            "transaction_id": transaction.plaid_investment_transaction_id,
            "account_id": transaction.account_id,
            "security_id": transaction.security_id,
            "date": transaction.date.isoformat(),
            "name": transaction.name,
            "quantity": _decimal_text(transaction.quantity),
            "amount": _decimal_text(transaction.amount),
            "price": (_decimal_text(transaction.price) if transaction.price is not None else None),
            "fees": _decimal_text(transaction.fees) if transaction.fees is not None else None,
            "type": transaction.type,
            "subtype": transaction.subtype,
            "currency": transaction.currency,
            "origin": transaction.origin,
        }
    )


def _manual_transaction_id(source_event_id: str) -> str:
    return f"manual:cashflow:v1:{source_event_id}"


def _manual_payload(event: _Event, account_id: int) -> dict[str, object]:
    return {
        "plaid_investment_transaction_id": _manual_transaction_id(event.source_event_id),
        "account_id": account_id,
        "security_id": None,
        "date": event.event_date,
        "name": "Manual reconciled external cash flow",
        "quantity": Decimal(0),
        # Plaid transaction amounts use the opposite sign from external flows.
        "amount": -event.signed_external_amount,
        "price": None,
        "fees": None,
        "type": "cash",
        "subtype": "transfer",
        "currency": event.currency,
        "origin": "manual",
    }


def _manual_payload_sha256(event: _Event, account_id: int) -> str:
    values = _manual_payload(event, account_id)
    return _digest(
        {
            "transaction_id": values["plaid_investment_transaction_id"],
            "account_id": account_id,
            "security_id": None,
            "date": event.event_date.isoformat(),
            "name": values["name"],
            "quantity": "0",
            "amount": _decimal_text(cast(Decimal, values["amount"])),
            "price": None,
            "fees": None,
            "type": "cash",
            "subtype": "transfer",
            "currency": event.currency,
            "origin": "manual",
        }
    )


def _parse_event(
    raw: object,
    *,
    index: int,
    source_sha256: str,
    account_identity_sha256: str,
    coverage_start: date,
    coverage_end: date,
    source_rows: tuple[_SourceRow, ...],
) -> _Event:
    context = f"events[{index}]"
    payload = _expect_object(raw, context)
    _expect_keys(
        payload,
        required=_REQUIRED_EVENT_KEYS,
        allowed=_EVENT_KEYS,
        context=context,
    )
    ordinal = payload["source_row_ordinal"]
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
        raise ManifestValidationError(f"{context}.source_row_ordinal must be a positive integer")
    event_date = _expect_date(payload["date"], f"{context}.date")
    if not coverage_start <= event_date <= coverage_end:
        raise ManifestValidationError(f"{context}.date is outside declared coverage")
    amount = _expect_amount(payload["signed_external_amount"], f"{context}.signed_external_amount")
    classification_value = _expect_string(payload["classification"], f"{context}.classification")
    if classification_value not in _CLASSIFICATIONS:
        raise ManifestValidationError(f"{context}.classification is not an external flow")
    classification = cast(Classification, classification_value)
    if (classification == "external_in") != (amount > 0):
        raise ManifestValidationError(f"{context} classification conflicts with amount sign")
    source_code = _expect_string(payload["source_code"], f"{context}.source_code", maximum=32)
    if ordinal > len(source_rows):
        raise ManifestValidationError(f"{context}.source_row_ordinal is outside the evidence CSV")
    source_row = source_rows[ordinal - 1]
    if _parse_robinhood_date(source_row.activity_date, context) != event_date:
        raise ManifestValidationError(f"{context} date does not match its evidence CSV row")
    if source_row.transaction_code.strip() != source_code:
        raise ManifestValidationError(f"{context} source_code does not match its evidence CSV row")
    if _parse_robinhood_amount(source_row.amount, context) != amount:
        raise ManifestValidationError(f"{context} amount does not match its evidence CSV row")
    currency = _expect_string(payload.get("currency", "USD"), f"{context}.currency")
    if currency != "USD":
        raise ManifestValidationError(f"{context}.currency must be USD")

    resolution = _expect_object(payload["resolution"], f"{context}.resolution")
    kind_value = _expect_string(resolution.get("kind"), f"{context}.resolution.kind")
    transaction_id: str | None = None
    identity_sha256: str | None = None
    expected_payload_sha256: str | None = None
    expected_override: str | None = None
    if kind_value == "manual_transaction":
        _expect_keys(
            resolution,
            required=_MANUAL_RESOLUTION_KEYS,
            allowed=_MANUAL_RESOLUTION_KEYS,
            context=f"{context}.resolution",
        )
    elif kind_value == "existing_transaction":
        _expect_keys(
            resolution,
            required=frozenset(
                {"kind", "expected_transaction_payload_sha256", "expected_current_override"}
            ),
            allowed=_EXISTING_RESOLUTION_KEYS,
            context=f"{context}.resolution",
        )
        transaction_id_value = resolution.get("transaction_id")
        identity_value = resolution.get("transaction_identity_sha256")
        if (transaction_id_value is None) == (identity_value is None):
            raise ManifestValidationError(
                f"{context}.resolution requires exactly one exact transaction identity"
            )
        if transaction_id_value is not None:
            transaction_id = _expect_string(
                transaction_id_value,
                f"{context}.resolution.transaction_id",
                maximum=512,
            )
        else:
            identity_sha256 = _expect_sha256(
                identity_value,
                f"{context}.resolution.transaction_identity_sha256",
            )
        expected_payload_sha256 = _expect_sha256(
            resolution["expected_transaction_payload_sha256"],
            f"{context}.resolution.expected_transaction_payload_sha256",
        )
        override_value = resolution["expected_current_override"]
        if override_value is not None:
            expected_override = _expect_string(
                override_value,
                f"{context}.resolution.expected_current_override",
            )
            if expected_override not in {"external_in", "external_out", "internal"}:
                raise ManifestValidationError(
                    f"{context}.resolution.expected_current_override is invalid"
                )
    else:
        raise ManifestValidationError(
            f"{context}.resolution.kind must be existing_transaction or manual_transaction"
        )

    source_event_id = _digest(
        {
            "identity_version": "cashflow_source_row.v1",
            "source_document_sha256": source_sha256,
            "account_identity_sha256": account_identity_sha256,
            "source_row_ordinal": ordinal,
        }
    )
    return _Event(
        source_event_id=source_event_id,
        source_row_ordinal=ordinal,
        event_date=event_date,
        signed_external_amount=amount,
        classification=classification,
        source_code=source_code,
        currency=currency,
        resolution_kind=kind_value,
        transaction_id=transaction_id,
        transaction_identity_sha256=identity_sha256,
        expected_transaction_payload_sha256=expected_payload_sha256,
        expected_current_override=expected_override,
    )


def _parse_manifest(session: Session, source: ManifestSource) -> _Manifest:
    try:
        manifest_bytes = source.manifest_path.read_bytes()
        source_bytes = source.source_path.read_bytes()
    except OSError as exc:
        raise ManifestValidationError("manifest or evidence source is unavailable") from exc
    try:
        raw = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestValidationError("manifest is not valid UTF-8 JSON") from exc
    payload = _expect_object(raw, "manifest")
    _expect_keys(
        payload,
        required=_REQUIRED_TOP_LEVEL_KEYS,
        allowed=_TOP_LEVEL_KEYS,
        context="manifest",
    )
    if payload.get("schema_version", "1") != "1":
        raise ManifestValidationError("manifest.schema_version must be 1")

    account_id = payload["account_id"]
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id < 1:
        raise ManifestValidationError("manifest.account_id must be a positive integer")
    account_identity = _expect_sha256(
        payload["account_identity_sha256"], "manifest.account_identity_sha256"
    )
    account = session.get(Account, account_id)
    if account is None or _account_identity_sha256(account) != account_identity:
        raise ManifestValidationError("manifest account mapping verification failed")
    active_item = session.scalar(
        select(Item.item_id).where(
            Item.item_id == account.item_id,
            Item.is_data_active.is_(True),
        )
    )
    if active_item is None or account_id not in valued_account_ids(session):
        raise ManifestValidationError("manifest account mapping is not active and valued")
    if account.currency != "USD":
        raise ManifestValidationError("manifest account mapping is not a USD account")

    source_sha256 = _expect_sha256(
        payload["source_document_sha256"], "manifest.source_document_sha256"
    )
    if _sha256_bytes(source_bytes) != source_sha256:
        raise ManifestValidationError("manifest source hash verification failed")
    source_rows = _parse_robinhood_csv(source_bytes)
    coverage_start = _expect_date(payload["coverage_start"], "manifest.coverage_start")
    coverage_end = _expect_date(payload["coverage_end"], "manifest.coverage_end")
    if coverage_start > coverage_end:
        raise ManifestValidationError("manifest coverage dates are reversed")
    source_type = _expect_string(payload["source_type"], "manifest.source_type")
    if source_type not in _SOURCE_TYPES:
        raise ManifestValidationError("manifest.source_type is invalid")
    source_reference = _expect_string(
        payload["source_reference"], "manifest.source_reference", maximum=512
    )
    expected_source_reference = f"private:{source_type}:{source_sha256}"
    if source_reference != expected_source_reference:
        raise ManifestValidationError(
            "manifest.source_reference must be the opaque private source reference"
        )
    captured_at = _expect_datetime(payload["captured_at"], "manifest.captured_at")
    methodology_version = _expect_string(
        payload["methodology_version"], "manifest.methodology_version", maximum=16
    )

    raw_gaps = payload["gaps"]
    if not isinstance(raw_gaps, list):
        raise ManifestValidationError("manifest.gaps must be a list")
    raw_gap_items = cast(list[object], raw_gaps)
    gaps: list[_Gap] = []
    gap_keys: set[tuple[date, date, str]] = set()
    for index, raw_gap in enumerate(raw_gap_items):
        gap_payload = _expect_object(raw_gap, f"gaps[{index}]")
        _expect_keys(
            gap_payload,
            required=_GAP_KEYS,
            allowed=_GAP_KEYS,
            context=f"gaps[{index}]",
        )
        gap_start = _expect_date(gap_payload["gap_start"], f"gaps[{index}].gap_start")
        gap_end = _expect_date(gap_payload["gap_end"], f"gaps[{index}].gap_end")
        reason = _expect_string(gap_payload["reason_code"], f"gaps[{index}].reason_code")
        if reason not in _GAP_REASONS:
            raise ManifestValidationError(f"gaps[{index}].reason_code is invalid")
        if gap_start > gap_end or gap_start < coverage_start or gap_end > coverage_end:
            raise ManifestValidationError(f"gaps[{index}] is outside declared coverage")
        key = (gap_start, gap_end, reason)
        if key in gap_keys:
            raise ManifestValidationError("manifest contains a duplicate gap")
        gap_keys.add(key)
        gaps.append(_Gap(gap_start, gap_end, reason))
    gaps.sort(key=lambda gap: (gap.gap_start, gap.gap_end, gap.reason_code))

    raw_events = payload["events"]
    if not isinstance(raw_events, list):
        raise ManifestValidationError("manifest.events must be a list")
    raw_event_items = cast(list[object], raw_events)
    events = tuple(
        _parse_event(
            raw_event,
            index=index,
            source_sha256=source_sha256,
            account_identity_sha256=account_identity,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            source_rows=source_rows,
        )
        for index, raw_event in enumerate(raw_event_items)
    )
    ordinals = [event.source_row_ordinal for event in events]
    if len(ordinals) != len(set(ordinals)):
        raise ManifestValidationError("manifest source_row_ordinal values must be unique")

    identity_payload = {
        "identity_version": "cashflow_attestation.v1",
        "account_identity_sha256": account_identity,
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "source_type": source_type,
        "source_document_sha256": source_sha256,
        "methodology_version": methodology_version,
        "gaps": [gap.digest_payload() for gap in gaps],
        "source_event_ids": sorted(event.source_event_id for event in events),
    }
    identity_digest = _digest(identity_payload)
    attestation_key = f"cashflow:v1:{identity_digest[:52]}"
    manifest_digest = _digest(
        {
            **identity_payload,
            "source_reference_hash": _sha256_text(source_reference),
            "captured_at": _datetime_text(captured_at),
            "events": [event.digest_payload() for event in events],
        }
    )
    return _Manifest(
        source=source,
        account_id=account_id,
        account_identity_sha256=account_identity,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        source_type=source_type,
        source_reference=source_reference,
        source_document_sha256=source_sha256,
        captured_at=captured_at,
        methodology_version=methodology_version,
        gaps=tuple(gaps),
        events=events,
        attestation_key=attestation_key,
        manifest_digest=manifest_digest,
    )


def _resolve_transaction_by_identity(
    session: Session, event: _Event
) -> tuple[InvestmentTransaction | None, str | None]:
    if event.transaction_id is not None:
        return session.get(InvestmentTransaction, event.transaction_id), None
    assert event.transaction_identity_sha256 is not None
    matches: list[InvestmentTransaction] = []
    for transaction in session.scalars(select(InvestmentTransaction)):
        if (
            transaction_identity_sha256(transaction.plaid_investment_transaction_id)
            == event.transaction_identity_sha256
        ):
            matches.append(transaction)
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, "explicit_transaction_not_found"
    return None, "explicit_transaction_identity_ambiguous"


def _event_matches_transaction(event: _Event, tx: InvestmentTransaction, account_id: int) -> bool:
    return (
        tx.account_id == account_id
        and tx.date == event.event_date
        and tx.currency == event.currency
        and tx.security_id is None
        and tx.quantity == 0
        and abs(tx.amount) == abs(event.signed_external_amount)
    )


def _effective_flow_matches(event: _Event, tx: InvestmentTransaction, override: str | None) -> bool:
    decision = classify_transaction_cashflow(
        tx.type,
        tx.subtype,
        tx.amount,
        override=override,
        name=tx.name,
    )
    return (
        decision is not None
        and decision.classification == event.classification
        and decision.signed_external_amount == event.signed_external_amount
    )


def _plan_event(session: Session, manifest: _Manifest, event: _Event) -> PlanEntry:
    if event.resolution_kind == "manual_transaction":
        transaction_id = _manual_transaction_id(event.source_event_id)
        tx = session.get(InvestmentTransaction, transaction_id)
        override = session.get(TransactionOverride, transaction_id)
        current_override = override.classification if override is not None else None
        if tx is None:
            if override is not None:
                return PlanEntry(
                    event.source_event_id,
                    "conflict",
                    "orphan_manual_override",
                    manifest.account_id,
                    event,
                    transaction_id,
                    None,
                    current_override,
                )
            return PlanEntry(
                event.source_event_id,
                "missing_insert",
                "manual_transaction_absent",
                manifest.account_id,
                event,
                transaction_id,
                None,
                None,
            )
        current_payload = transaction_payload_sha256(tx)
        if current_payload != _manual_payload_sha256(event, manifest.account_id):
            return PlanEntry(
                event.source_event_id,
                "conflict",
                "deterministic_manual_payload_conflict",
                manifest.account_id,
                event,
                transaction_id,
                current_payload,
                current_override,
            )
        if current_override != event.classification:
            return PlanEntry(
                event.source_event_id,
                "conflict",
                "deterministic_manual_override_conflict",
                manifest.account_id,
                event,
                transaction_id,
                current_payload,
                current_override,
            )
        return PlanEntry(
            event.source_event_id,
            "existing_exact",
            "manual_transaction_exact",
            manifest.account_id,
            event,
            transaction_id,
            current_payload,
            current_override,
        )

    tx, identity_error = _resolve_transaction_by_identity(session, event)
    fallback_id = event.transaction_id or "opaque-transaction-identity"
    if identity_error is not None or tx is None:
        return PlanEntry(
            event.source_event_id,
            "conflict",
            identity_error or "explicit_transaction_not_found",
            manifest.account_id,
            event,
            fallback_id,
            None,
            None,
        )
    transaction_id = tx.plaid_investment_transaction_id
    current_payload = transaction_payload_sha256(tx)
    override = session.get(TransactionOverride, transaction_id)
    current_override = override.classification if override is not None else None
    if current_payload != event.expected_transaction_payload_sha256:
        reason = "existing_transaction_payload_changed"
    elif not _event_matches_transaction(event, tx, manifest.account_id):
        reason = "existing_transaction_does_not_match_source_event"
    else:
        reason = ""
    if reason:
        return PlanEntry(
            event.source_event_id,
            "conflict",
            reason,
            manifest.account_id,
            event,
            transaction_id,
            current_payload,
            current_override,
        )
    if _effective_flow_matches(event, tx, current_override):
        return PlanEntry(
            event.source_event_id,
            "existing_exact",
            "existing_transaction_exact",
            manifest.account_id,
            event,
            transaction_id,
            current_payload,
            current_override,
        )
    if current_override != event.expected_current_override:
        return PlanEntry(
            event.source_event_id,
            "conflict",
            "existing_override_changed",
            manifest.account_id,
            event,
            transaction_id,
            current_payload,
            current_override,
        )
    return PlanEntry(
        event.source_event_id,
        "override_required",
        "explicit_classification_override_required",
        manifest.account_id,
        event,
        transaction_id,
        current_payload,
        current_override,
    )


def _attestation_current_payload(
    session: Session, attestation: CashFlowSourceAttestation
) -> tuple[dict[str, object], tuple[_Gap, ...]]:
    gaps = tuple(
        sorted(
            (
                _Gap(gap.gap_start, gap.gap_end, gap.reason_code)
                for gap in session.scalars(
                    select(CashFlowSourceGap).where(
                        CashFlowSourceGap.attestation_id == attestation.attestation_id
                    )
                )
            ),
            key=lambda gap: (gap.gap_start, gap.gap_end, gap.reason_code),
        )
    )
    payload: dict[str, object] = {
        "attestation_key": attestation.attestation_key,
        "account_id": attestation.account_id,
        "coverage_start": attestation.coverage_start.isoformat(),
        "coverage_end": attestation.coverage_end.isoformat(),
        "source_type": attestation.source_type,
        "source_reference": attestation.source_reference,
        "source_sha256": attestation.source_sha256,
        "captured_at": _datetime_text(_as_aware_utc(attestation.captured_at)),
        "methodology_version": attestation.methodology_version,
        "gaps": [gap.digest_payload() for gap in gaps],
    }
    return payload, gaps


def _plan_attestation(session: Session, manifest: _Manifest) -> _AttestationPlan:
    existing = session.scalar(
        select(CashFlowSourceAttestation).where(
            CashFlowSourceAttestation.attestation_key == manifest.attestation_key
        )
    )
    competing = tuple(
        session.scalars(
            select(CashFlowSourceAttestation).where(
                CashFlowSourceAttestation.account_id == manifest.account_id,
                CashFlowSourceAttestation.source_sha256 == manifest.source_document_sha256,
                CashFlowSourceAttestation.coverage_start == manifest.coverage_start,
                CashFlowSourceAttestation.coverage_end == manifest.coverage_end,
                CashFlowSourceAttestation.superseded_at.is_(None),
            )
        )
    )
    if existing is None and competing:
        return _AttestationPlan(
            manifest.manifest_digest,
            "conflict",
            "source_attestation_interpretation_conflict",
            _digest(sorted(row.attestation_key for row in competing)),
            0,
        )
    if existing is None:
        return _AttestationPlan(
            manifest.manifest_digest,
            "insert_required",
            "source_attestation_absent",
            None,
            1 + len(manifest.gaps),
        )
    current_payload, _ = _attestation_current_payload(session, existing)
    current_digest = _digest(current_payload)
    if current_payload != manifest.attestation_payload():
        return _AttestationPlan(
            manifest.manifest_digest,
            "conflict",
            "source_attestation_payload_conflict",
            current_digest,
            0,
        )
    if existing.superseded_at is not None or existing.superseded_by_attestation_id is not None:
        return _AttestationPlan(
            manifest.manifest_digest,
            "conflict",
            "source_attestation_superseded",
            current_digest,
            0,
        )
    if existing.approved_at is None:
        return _AttestationPlan(
            manifest.manifest_digest,
            "approval_required",
            "source_attestation_unapproved",
            current_digest,
            1,
        )
    return _AttestationPlan(
        manifest.manifest_digest,
        "existing_exact",
        "source_attestation_exact",
        current_digest,
        0,
    )


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def build_reconciliation_plan(
    session: Session, sources: Sequence[ManifestSource]
) -> ReconciliationPlan:
    """Validate private manifests and build a deterministic read-only plan."""
    if not sources:
        raise ManifestValidationError("at least one manifest source is required")
    normalized_sources = tuple(
        ManifestSource(Path(source.manifest_path), Path(source.source_path)) for source in sources
    )
    manifests = tuple(_parse_manifest(session, source) for source in normalized_sources)
    manifest_digests = [manifest.manifest_digest for manifest in manifests]
    if len(manifest_digests) != len(set(manifest_digests)):
        raise ManifestValidationError("duplicate manifest source")
    all_event_ids = [event.source_event_id for manifest in manifests for event in manifest.events]
    if len(all_event_ids) != len(set(all_event_ids)):
        raise ManifestValidationError("duplicate stable source-row identity across manifests")

    entries = tuple(
        _plan_event(session, manifest, event) for manifest in manifests for event in manifest.events
    )
    target_counts = Counter(entry.resolved_transaction_id for entry in entries)
    entries = tuple(
        replace(
            entry,
            status="conflict",
            reason_code="transaction_target_reused_by_multiple_source_events",
        )
        if target_counts[entry.resolved_transaction_id] > 1
        else entry
        for entry in entries
    )
    attestations = tuple(_plan_attestation(session, manifest) for manifest in manifests)
    counts = Counter(entry.status for entry in entries)
    status_counts = {status: counts.get(status, 0) for status in _STATUS_ORDER}
    conflict_count = status_counts["conflict"] + sum(
        attestation.status == "conflict" for attestation in attestations
    )
    planned_mutations = sum(
        2 if entry.status == "missing_insert" else 1 if entry.status == "override_required" else 0
        for entry in entries
    ) + sum(attestation.planned_mutation_count for attestation in attestations)
    plan_payload = {
        "plan_version": "cashflow_reconciliation.v1",
        "manifests": [manifest.manifest_digest for manifest in manifests],
        "entries": [entry.digest_payload() for entry in entries],
        "attestations": [attestation.digest_payload() for attestation in attestations],
        "planned_mutation_count": planned_mutations,
        "conflict_count": conflict_count,
    }
    return ReconciliationPlan(
        sources=normalized_sources,
        manifests=manifests,
        entries=entries,
        attestations=attestations,
        status_counts=status_counts,
        planned_mutation_count=planned_mutations,
        conflict_count=conflict_count,
        plan_digest=_digest(plan_payload),
    )


def _validate_approval_time(approved_at: datetime, manifests: Sequence[_Manifest]) -> datetime:
    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise ManifestValidationError("approved_at must include a UTC offset")
    normalized = approved_at.astimezone(UTC)
    if any(normalized < manifest.captured_at for manifest in manifests):
        raise ManifestValidationError("approved_at predates source capture")
    return normalized


def _insert_manual_transaction(session: Session, entry: PlanEntry) -> None:
    session.add(InvestmentTransaction(**_manual_payload(entry.event, entry.account_id)))
    session.add(
        TransactionOverride(
            plaid_investment_transaction_id=entry.resolved_transaction_id,
            classification=entry.event.classification,
            notes="Owner-approved cash-flow source reconciliation",
        )
    )


def _insert_or_approve_attestation(
    session: Session,
    manifest: _Manifest,
    attestation_plan: _AttestationPlan,
    approved_at: datetime,
) -> None:
    if attestation_plan.status == "insert_required":
        attestation = CashFlowSourceAttestation(
            attestation_key=manifest.attestation_key,
            account_id=manifest.account_id,
            coverage_start=manifest.coverage_start,
            coverage_end=manifest.coverage_end,
            source_type=manifest.source_type,
            source_reference=manifest.source_reference,
            source_sha256=manifest.source_document_sha256,
            captured_at=manifest.captured_at,
            approved_at=approved_at,
            methodology_version=manifest.methodology_version,
            superseded_at=None,
            superseded_by_attestation_id=None,
        )
        session.add(attestation)
        session.flush()
        session.add_all(
            CashFlowSourceGap(
                attestation_id=attestation.attestation_id,
                gap_start=gap.gap_start,
                gap_end=gap.gap_end,
                reason_code=gap.reason_code,
            )
            for gap in manifest.gaps
        )
    elif attestation_plan.status == "approval_required":
        attestation = session.scalar(
            select(CashFlowSourceAttestation).where(
                CashFlowSourceAttestation.attestation_key == manifest.attestation_key
            )
        )
        if attestation is None:
            raise ReconciliationConflictError("attestation state changed")
        attestation.approved_at = approved_at


def _verify_applied_state(session: Session, plan: ReconciliationPlan) -> None:
    """Verify all intended authorities before allowing the transaction to commit."""
    for entry in plan.entries:
        tx = session.get(InvestmentTransaction, entry.resolved_transaction_id)
        if tx is None:
            raise ReconciliationConflictError("transaction write verification failed")
        expected_payload = (
            _manual_payload_sha256(entry.event, entry.account_id)
            if entry.event.resolution_kind == "manual_transaction"
            else entry.event.expected_transaction_payload_sha256
        )
        if transaction_payload_sha256(tx) != expected_payload:
            raise ReconciliationConflictError("transaction payload verification failed")
        override = session.get(TransactionOverride, entry.resolved_transaction_id)
        current_override = override.classification if override is not None else None
        if not _effective_flow_matches(entry.event, tx, current_override):
            raise ReconciliationConflictError("cash-flow classification verification failed")

    for manifest in plan.manifests:
        attestation = session.scalar(
            select(CashFlowSourceAttestation).where(
                CashFlowSourceAttestation.attestation_key == manifest.attestation_key
            )
        )
        if attestation is None or attestation.approved_at is None:
            raise ReconciliationConflictError("attestation write verification failed")
        current_payload, _ = _attestation_current_payload(session, attestation)
        if current_payload != manifest.attestation_payload():
            raise ReconciliationConflictError("attestation payload verification failed")

    # The evidence is outside the database transaction.  Re-read it at the
    # final boundary so a source replacement during apply fails closed too.
    reparsed = tuple(_parse_manifest(session, source) for source in plan.sources)
    if tuple(item.manifest_digest for item in reparsed) != tuple(
        item.manifest_digest for item in plan.manifests
    ):
        raise ReconciliationConflictError("manifest source changed during commit")


def apply_reconciliation_plan(
    session: Session,
    plan: ReconciliationPlan,
    *,
    expected_plan_digest: str | None = None,
    approved_at: datetime,
) -> ReconciliationResult:
    """Atomically apply an exact, conflict-free plan after locked revalidation."""
    if expected_plan_digest is None or expected_plan_digest != plan.plan_digest:
        raise ReconciliationConflictError("exact plan digest approval is required")
    approved_at = _validate_approval_time(approved_at, plan.manifests)
    if plan.conflict_count:
        raise ReconciliationConflictError("reconciliation plan contains conflicts")

    # Planning performs reads and therefore starts a transaction.  End that
    # snapshot, acquire SQLite's write reservation, and rebuild from source so
    # validation and all writes occur inside one atomic database transaction.
    session.rollback()
    try:
        session.execute(text("BEGIN IMMEDIATE"))
        locked_plan = build_reconciliation_plan(session, plan.sources)
        if locked_plan.plan_digest != expected_plan_digest:
            raise ReconciliationConflictError("reconciliation plan changed before commit")
        if locked_plan.conflict_count:
            raise ReconciliationConflictError("reconciliation plan contains conflicts")

        for entry in locked_plan.entries:
            if entry.status == "missing_insert":
                _insert_manual_transaction(session, entry)
            elif entry.status == "override_required":
                override = session.get(TransactionOverride, entry.resolved_transaction_id)
                if override is None:
                    session.add(
                        TransactionOverride(
                            plaid_investment_transaction_id=entry.resolved_transaction_id,
                            classification=entry.event.classification,
                            notes="Owner-approved cash-flow source reconciliation",
                        )
                    )
                else:
                    override.classification = entry.event.classification
                    override.notes = "Owner-approved cash-flow source reconciliation"
        for manifest, attestation_plan in zip(
            locked_plan.manifests, locked_plan.attestations, strict=True
        ):
            _insert_or_approve_attestation(session, manifest, attestation_plan, approved_at)
        session.flush()
        _verify_applied_state(session, locked_plan)

        applied_mutation_count = locked_plan.planned_mutation_count
        session.commit()
    except Exception:
        session.rollback()
        raise
    return ReconciliationResult(
        committed=True,
        manifest_count=len(locked_plan.manifests),
        source_event_count=len(locked_plan.entries),
        status_counts=dict(locked_plan.status_counts),
        applied_mutation_count=applied_mutation_count,
        plan_digest=locked_plan.plan_digest,
    )


def write_private_preview_artifact(plan: ReconciliationPlan, destination: Path) -> None:
    """Atomically write a mode-0600 private audit preview.

    Unlike stdout, this owner-requested private artifact includes dated amounts
    needed to prove one-to-one source coverage.  Account and provider
    transaction identifiers remain absent or hashed.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    external_in_total = sum(
        (
            entry.event.signed_external_amount
            for entry in plan.entries
            if entry.event.classification == "external_in"
        ),
        Decimal(0),
    )
    external_out_total = sum(
        (
            entry.event.signed_external_amount
            for entry in plan.entries
            if entry.event.classification == "external_out"
        ),
        Decimal(0),
    )
    totals_by_status = {
        status: {
            "event_count": sum(entry.status == status for entry in plan.entries),
            "net_external_cashflow": _decimal_text(
                sum(
                    (
                        entry.event.signed_external_amount
                        for entry in plan.entries
                        if entry.status == status
                    ),
                    Decimal(0),
                )
            ),
        }
        for status in _STATUS_ORDER
    }
    payload = {
        **plan.console_summary(),
        "preview_schema": "cashflow_reconciliation_preview.v1",
        "private_totals": {
            "external_in": _decimal_text(external_in_total),
            "external_out": _decimal_text(external_out_total),
            "net_external_cashflow": _decimal_text(external_in_total + external_out_total),
        },
        "private_totals_by_status": totals_by_status,
        "entries": [
            {
                "source_event_id": entry.source_event_id,
                "source_row_ordinal": entry.event.source_row_ordinal,
                "date": entry.event.event_date.isoformat(),
                "signed_external_amount": _decimal_text(entry.event.signed_external_amount),
                "classification": entry.event.classification,
                "source_code": entry.event.source_code,
                "target_identity_sha256": (
                    entry.event.transaction_identity_sha256
                    or _sha256_text(entry.resolved_transaction_id)
                ),
                "status": entry.status,
                "reason_code": entry.reason_code,
            }
            for entry in plan.entries
        ],
        "attestations": [
            {
                "manifest_digest": attestation.manifest_digest,
                "status": attestation.status,
                "reason_code": attestation.reason_code,
            }
            for attestation in plan.attestations
        ],
    }
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        with suppress(OSError):
            os.close(file_descriptor)
        temporary_path.unlink(missing_ok=True)
        raise


def _parse_cli_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO timestamp with UTC offset") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must include a UTC offset")
    return parsed


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan or apply private cash-flow manifests")
    parser.add_argument(
        "--source",
        action="append",
        nargs=2,
        metavar=("MANIFEST", "EVIDENCE"),
        required=True,
        help="private manifest and its exact evidence file; repeat as needed",
    )
    parser.add_argument("--preview-path", type=Path)
    parser.add_argument("--commit", action="store_true", help="apply the exact approved plan")
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--approved-at", type=_parse_cli_datetime)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a sanitized dry-run by default; commit only with explicit approval."""
    parser = _argument_parser()
    args = parser.parse_args(argv)
    if args.commit and (args.expected_plan_digest is None or args.approved_at is None):
        parser.error("--commit requires --expected-plan-digest and --approved-at")
    sources = tuple(
        ManifestSource(Path(manifest_path), Path(source_path))
        for manifest_path, source_path in args.source
    )
    try:
        from portfolio_tracker.db import SessionLocal

        with SessionLocal() as session:
            plan = build_reconciliation_plan(session, sources)
            if args.preview_path is not None:
                write_private_preview_artifact(plan, args.preview_path)
            if args.commit:
                result = apply_reconciliation_plan(
                    session,
                    plan,
                    expected_plan_digest=args.expected_plan_digest,
                    approved_at=args.approved_at,
                )
                summary = result.console_summary()
            else:
                summary = plan.console_summary()
        print(_canonical_json(summary))
        return 0 if summary.get("conflict_count", 0) == 0 else 2
    except (ManifestValidationError, ReconciliationConflictError):
        # Never echo an exception whose text could contain a private path or ID.
        print(_canonical_json({"committed": False, "error_code": "reconciliation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
