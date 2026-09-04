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
    CashFlowReconciliationDecision,
    CashFlowReconciliationRun,
    CashFlowReconciliationRunDecision,
    CashFlowReconciliationRunTransactionMutation,
    CashFlowSourceAttestation,
    CashFlowSourceEvent,
    CashFlowSourceGap,
    InvestmentTransaction,
    Item,
    TransactionOverride,
)
from portfolio_tracker.services.active_items import valued_account_ids
from portfolio_tracker.services.cashflow_source_coverage import (
    canonical_decision_payload_sha256,
)
from portfolio_tracker.services.external_flow_ledger import (
    classify_transaction_cashflow,
)

Classification = Literal["external_in", "external_out", "internal", "excluded"]
EntryStatus = Literal[
    "existing_exact",
    "override_required",
    "missing_insert",
    "conflict",
    "excluded",
]
ResolutionKind = Literal["existing_transaction", "manual_transaction", "no_transaction"]
Disposition = Literal[
    "provider_exact",
    "statement_supplement",
    "internal",
    "excluded",
    "unresolved",
    "provider_supersedes_supplement",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"external_in", "external_out", "internal", "excluded"}
)
_DISPOSITIONS: frozenset[str] = frozenset(
    {
        "provider_exact",
        "statement_supplement",
        "internal",
        "excluded",
        "unresolved",
        "provider_supersedes_supplement",
    }
)
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
_TOP_LEVEL_KEYS_V2 = frozenset(
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
        "account_mapping_basis",
        "account_mapping_confidence",
        "source_format",
        "parser_version",
        "source_timezone",
        "source_row_count",
        "cashflow_candidate_count",
        "source_event_set_sha256",
        "gaps",
        "events",
    }
)
_TOP_LEVEL_KEYS_V3 = _TOP_LEVEL_KEYS_V2 | frozenset(
    {"requested_return_start", "requested_return_end"}
)
_TOP_LEVEL_KEYS_V4 = _TOP_LEVEL_KEYS_V3 | frozenset({"provider_unresolved_resolutions"})
_REQUIRED_TOP_LEVEL_KEYS_V2 = _TOP_LEVEL_KEYS_V2 - {"schema_version"}
_REQUIRED_TOP_LEVEL_KEYS_V3 = _TOP_LEVEL_KEYS_V3
_REQUIRED_TOP_LEVEL_KEYS_V4 = _TOP_LEVEL_KEYS_V4
_EVENT_KEYS = frozenset(
    {
        "source_row_ordinal",
        "source_row_sha256",
        "activity_date",
        "process_date",
        "settlement_date",
        "source_amount",
        "source_amount_sign_basis",
        "date",
        "signed_external_amount",
        "classification",
        "source_code",
        "currency",
        "disposition",
        "ledger_effective_date",
        "effective_date_basis",
        "effective_timezone",
        "confidence",
        "assumption_code",
        "decision_authority",
        "resolution",
    }
)
_REQUIRED_EVENT_KEYS = _EVENT_KEYS - {"currency", "date"}
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
_NO_TRANSACTION_RESOLUTION_KEYS = frozenset({"kind"})
_GAP_KEYS = frozenset({"gap_start", "gap_end", "reason_code"})
_PROVIDER_UNRESOLVED_RESOLUTION_KEYS = frozenset(
    {
        "evidence_source_row_ordinal",
        "provider_source_event_id",
        "expected_provider_source_row_sha256",
        "expected_current_decision_key",
        "expected_current_decision_payload_sha256",
    }
)
_PROVIDER_UNRESOLVED_ASSUMPTION_CODES = frozenset(
    {
        "provider_activity_type_unresolved",
        "provider_in_kind_requires_reconciliation",
        "provider_external_cash_amount_zero",
        "provider_cash_classification_unresolved",
        "provider_transfer_classification_unresolved",
    }
)
_CORROBORATING_PROVIDER_RESOLUTION_ASSUMPTION = (
    "corroborating_evidence_resolves_provider_unresolved"
)
_CORROBORATING_PROVIDER_RESOLVED_ASSUMPTION = "corroborating_evidence_confirms_provider_resolved"
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
_SUPPORTED_EXTERNAL_CASH_CODES = frozenset({"ACH", "MTCH", "ACATI", "DRFRO", "CFIR"})
_SUPPORTED_IN_KIND_TRANSFER_CODES = frozenset({"ACATI"})


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
    process_date: str
    settlement_date: str
    instrument: str
    transaction_code: str
    quantity: str
    amount: str
    source_row_sha256: str


@dataclass(frozen=True)
class _ProviderUnresolvedResolution:
    """Exact preconditions for replacing one provider-created unresolved decision."""

    evidence_source_row_ordinal: int
    provider_source_event_id: str
    expected_provider_source_row_sha256: str
    expected_current_decision_key: str
    expected_current_decision_payload_sha256: str

    def digest_payload(self) -> dict[str, object]:
        return {
            "evidence_source_row_ordinal": self.evidence_source_row_ordinal,
            "provider_source_event_id": self.provider_source_event_id,
            "expected_provider_source_row_sha256": self.expected_provider_source_row_sha256,
            "expected_current_decision_key": self.expected_current_decision_key,
            "expected_current_decision_payload_sha256": (
                self.expected_current_decision_payload_sha256
            ),
        }


@dataclass(frozen=True)
class _Event:
    source_event_id: str
    source_row_ordinal: int
    source_row_sha256: str
    activity_date: date
    process_date: date | None
    settlement_date: date | None
    source_amount: Decimal
    source_amount_sign_basis: str
    signed_external_amount: Decimal | None
    classification: Classification | None
    source_code: str
    currency: str
    disposition: Disposition
    ledger_effective_date: date | None
    effective_date_basis: str | None
    effective_timezone: str | None
    confidence: str
    assumption_code: str | None
    decision_authority: str
    resolution_kind: ResolutionKind
    transaction_id: str | None
    transaction_identity_sha256: str | None
    expected_transaction_payload_sha256: str | None
    expected_current_override: str | None

    def digest_payload(self) -> dict[str, object]:
        return {
            "source_event_id": self.source_event_id,
            "source_row_ordinal": self.source_row_ordinal,
            "source_row_sha256": self.source_row_sha256,
            "activity_date": self.activity_date.isoformat(),
            "process_date": self.process_date.isoformat() if self.process_date else None,
            "settlement_date": (self.settlement_date.isoformat() if self.settlement_date else None),
            "source_amount": _decimal_text(self.source_amount),
            "source_amount_sign_basis": self.source_amount_sign_basis,
            "signed_external_amount": (
                _decimal_text(self.signed_external_amount)
                if self.signed_external_amount is not None
                else None
            ),
            "classification": self.classification,
            "source_code": self.source_code,
            "currency": self.currency,
            "disposition": self.disposition,
            "ledger_effective_date": (
                self.ledger_effective_date.isoformat()
                if self.ledger_effective_date is not None
                else None
            ),
            "effective_date_basis": self.effective_date_basis,
            "effective_timezone": self.effective_timezone,
            "confidence": self.confidence,
            "assumption_code": self.assumption_code,
            "decision_authority": self.decision_authority,
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
    schema_version: Literal["2", "3", "4"]
    account_id: int
    account_identity_sha256: str
    coverage_start: date
    coverage_end: date
    requested_return_start: date | None
    requested_return_end: date | None
    source_type: str
    source_reference: str
    source_document_sha256: str
    captured_at: datetime
    methodology_version: str
    account_mapping_basis: str
    account_mapping_confidence: str
    source_format: str
    parser_version: str
    source_timezone: str
    source_row_count: int
    cashflow_candidate_count: int
    source_event_set_sha256: str
    gaps: tuple[_Gap, ...]
    events: tuple[_Event, ...]
    provider_unresolved_resolutions: tuple[_ProviderUnresolvedResolution, ...]
    attestation_key: str
    attestation_manifest_sha256: str
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
            "account_identity_sha256": self.account_identity_sha256,
            "account_mapping_basis": self.account_mapping_basis,
            "account_mapping_confidence": self.account_mapping_confidence,
            "source_format": self.source_format,
            "parser_version": self.parser_version,
            "source_timezone": self.source_timezone,
            "source_row_count": self.source_row_count,
            "cashflow_candidate_count": self.cashflow_candidate_count,
            "source_event_set_sha256": self.source_event_set_sha256,
            "manifest_sha256": self.attestation_manifest_sha256,
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
    resolved_transaction_id: str | None
    current_transaction_payload_sha256: str | None
    current_override: str | None

    def digest_payload(self) -> dict[str, object]:
        return {
            "source_event_id": self.source_event_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "account_id": self.account_id,
            "event": self.event.digest_payload(),
            "resolved_transaction_id_hash": (
                _sha256_text(self.resolved_transaction_id)
                if self.resolved_transaction_id is not None
                else None
            ),
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
class _ProviderUnresolvedResolutionPlan:
    """Read-only disposition for one exact provider unresolved-decision replacement."""

    evidence_source_event_id: str
    provider_source_event_id: str
    expected_provider_source_row_sha256: str
    expected_current_decision_key: str
    expected_current_decision_payload_sha256: str
    desired_decision_values: dict[str, object]
    status: Literal["existing_exact", "supersession_required", "conflict"]
    reason_code: str
    planned_mutation_count: int

    def digest_payload(self) -> dict[str, object]:
        desired = {
            key: (
                value.isoformat()
                if isinstance(value, date)
                else _decimal_text(value)
                if isinstance(value, Decimal)
                else value
            )
            for key, value in self.desired_decision_values.items()
        }
        return {
            "evidence_source_event_id": self.evidence_source_event_id,
            "provider_source_event_id": self.provider_source_event_id,
            "expected_provider_source_row_sha256": self.expected_provider_source_row_sha256,
            "expected_current_decision_key": self.expected_current_decision_key,
            "expected_current_decision_payload_sha256": (
                self.expected_current_decision_payload_sha256
            ),
            "desired_decision_values": desired,
            "status": self.status,
            "reason_code": self.reason_code,
            "planned_mutation_count": self.planned_mutation_count,
        }


@dataclass(frozen=True)
class ReconciliationPlan:
    """Immutable dry-run plan whose digest is required for commit."""

    sources: tuple[ManifestSource, ...]
    manifests: tuple[_Manifest, ...]
    entries: tuple[PlanEntry, ...]
    attestations: tuple[_AttestationPlan, ...]
    provider_unresolved_resolutions: tuple[_ProviderUnresolvedResolutionPlan, ...]
    requested_return_start: date | None
    requested_return_end: date | None
    status_counts: dict[str, int]
    planned_mutation_count: int
    conflict_count: int
    plan_digest: str

    def console_summary(self) -> dict[str, object]:
        """Return only non-sensitive counts and an opaque plan digest."""
        return {
            "committed": False,
            "manifest_count": len(self.manifests),
            "source_event_count": len(self.entries) + len(self.provider_unresolved_resolutions),
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


@dataclass(frozen=True)
class _DecisionMembership:
    decision_key: str
    membership_kind: Literal["created", "superseded", "verified"]


@dataclass(frozen=True)
class _TransactionMutationReceipt:
    target_transaction_id: str
    mutation_kind: Literal["transaction_insert", "override_insert", "override_update"]
    before_payload_sha256: str | None
    after_payload_sha256: str


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


def _expect_nonnegative_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManifestValidationError(f"{context} must be a non-negative integer")
    return value


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


def _expect_decimal(value: object, context: str, *, allow_zero: bool) -> Decimal:
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise ManifestValidationError(f"{context} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ManifestValidationError(f"{context} must be a finite decimal string") from exc
    if not parsed.is_finite() or (not allow_zero and parsed == 0):
        qualifier = "finite" if allow_zero else "finite and non-zero"
        raise ManifestValidationError(f"{context} must be {qualifier}")
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
        _parse_optional_robinhood_date(row[1], f"evidence source row {ordinal}.Process Date")
        _parse_optional_robinhood_date(row[2], f"evidence source row {ordinal}.Settle Date")
        source_rows.append(
            _SourceRow(
                row[0],
                row[1],
                row[2],
                row[3],
                row[5],
                row[6],
                row[8],
                _digest(
                    {
                        header: value.strip()
                        for header, value in zip(_ROBINHOOD_HEADERS, row, strict=True)
                    }
                ),
            )
        )
    return tuple(source_rows)


def _parse_robinhood_date(value: str, context: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date()
    except ValueError as exc:
        raise ManifestValidationError(f"{context} has an invalid Activity Date") from exc


def _parse_optional_robinhood_date(value: str, context: str) -> date | None:
    if not value.strip():
        return None
    return _parse_robinhood_date(value, context)


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


def _external_cash_candidate_amount(
    row: _SourceRow,
    context: str,
) -> Decimal | None:
    if row.transaction_code.strip() not in _SUPPORTED_EXTERNAL_CASH_CODES:
        return None
    if _is_in_kind_transfer(row):
        return None
    amount = _parse_robinhood_amount(row.amount, context)
    return amount if amount != 0 else None


def _is_nonzero_numeric_quantity(value: str) -> bool:
    normalized = value.strip().replace(",", "")
    try:
        quantity = Decimal(normalized)
    except InvalidOperation:
        return False
    return quantity.is_finite() and quantity != 0


def _is_in_kind_transfer(row: _SourceRow) -> bool:
    if (
        row.transaction_code.strip() not in _SUPPORTED_IN_KIND_TRANSFER_CODES
        or row.amount.strip() not in {"", "--"}
    ):
        return False
    try:
        _parse_robinhood_amount(row.amount, "in-kind transfer")
    except ManifestValidationError:
        return bool(row.instrument.strip()) and _is_nonzero_numeric_quantity(row.quantity)
    return False


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


def _override_payload_sha256(classification: str, notes: str) -> str:
    return _digest({"classification": classification, "notes": notes})


def _manual_transaction_id(source_event_id: str) -> str:
    return f"manual:cashflow:v1:{source_event_id}"


def _manual_payload(event: _Event, account_id: int) -> dict[str, object]:
    if event.ledger_effective_date is None or event.signed_external_amount is None:
        raise ReconciliationConflictError("supplemental event lacks economic fields")
    return {
        "plaid_investment_transaction_id": _manual_transaction_id(event.source_event_id),
        "account_id": account_id,
        "security_id": None,
        "date": event.ledger_effective_date,
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
            "date": cast(date, values["date"]).isoformat(),
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
    if ordinal > len(source_rows):
        raise ManifestValidationError(f"{context}.source_row_ordinal is outside the evidence CSV")
    source_row = source_rows[ordinal - 1]
    source_row_sha256 = _expect_sha256(payload["source_row_sha256"], f"{context}.source_row_sha256")
    if source_row_sha256 != source_row.source_row_sha256:
        raise ManifestValidationError(f"{context} row hash does not match its evidence CSV row")
    activity_date = _expect_date(payload["activity_date"], f"{context}.activity_date")
    process_date = (
        _expect_date(payload["process_date"], f"{context}.process_date")
        if payload["process_date"] is not None
        else None
    )
    settlement_date = (
        _expect_date(payload["settlement_date"], f"{context}.settlement_date")
        if payload["settlement_date"] is not None
        else None
    )
    if not coverage_start <= activity_date <= coverage_end:
        raise ManifestValidationError(f"{context}.activity_date is outside declared coverage")
    if _parse_robinhood_date(source_row.activity_date, context) != activity_date:
        raise ManifestValidationError(f"{context} date does not match its evidence CSV row")
    if _parse_optional_robinhood_date(source_row.process_date, context) != process_date:
        raise ManifestValidationError(f"{context} process date does not match its evidence CSV row")
    if _parse_optional_robinhood_date(source_row.settlement_date, context) != settlement_date:
        raise ManifestValidationError(
            f"{context} settlement date does not match its evidence CSV row"
        )
    if (
        payload.get("date") is not None
        and _expect_date(payload["date"], f"{context}.date") != activity_date
    ):
        raise ManifestValidationError(f"{context}.date must equal activity_date")
    source_amount = _expect_decimal(
        payload["source_amount"], f"{context}.source_amount", allow_zero=True
    )
    source_candidate_amount = _external_cash_candidate_amount(source_row, context)
    if source_candidate_amount is None or source_candidate_amount != source_amount:
        raise ManifestValidationError(f"{context} amount does not match its evidence CSV row")
    source_amount_sign_basis = _expect_string(
        payload["source_amount_sign_basis"],
        f"{context}.source_amount_sign_basis",
    )
    if source_amount_sign_basis not in {
        "statement_printed",
        "provider_reported",
        "normalized_external",
    }:
        raise ManifestValidationError(f"{context}.source_amount_sign_basis is invalid")
    source_code = _expect_string(payload["source_code"], f"{context}.source_code", maximum=32)
    if source_row.transaction_code.strip() != source_code:
        raise ManifestValidationError(f"{context} source_code does not match its evidence CSV row")
    currency = _expect_string(payload.get("currency", "USD"), f"{context}.currency")
    if currency != "USD":
        raise ManifestValidationError(f"{context}.currency must be USD")

    disposition_value = _expect_string(payload["disposition"], f"{context}.disposition")
    if disposition_value not in _DISPOSITIONS:
        raise ManifestValidationError(f"{context}.disposition is invalid")
    disposition = cast(Disposition, disposition_value)
    classification_raw = payload["classification"]
    amount_raw = payload["signed_external_amount"]
    ledger_date_raw = payload["ledger_effective_date"]
    date_basis_raw = payload["effective_date_basis"]
    timezone_raw = payload["effective_timezone"]
    if disposition == "unresolved":
        if any(
            value is not None
            for value in (
                classification_raw,
                amount_raw,
                ledger_date_raw,
                date_basis_raw,
                timezone_raw,
            )
        ):
            raise ManifestValidationError(f"{context} unresolved disposition has economic fields")
        classification: Classification | None = None
        signed_external_amount: Decimal | None = None
        ledger_effective_date: date | None = None
        effective_date_basis: str | None = None
        effective_timezone: str | None = None
    else:
        classification_value = _expect_string(classification_raw, f"{context}.classification")
        if classification_value not in _CLASSIFICATIONS:
            raise ManifestValidationError(f"{context}.classification is invalid")
        classification = cast(Classification, classification_value)
        signed_external_amount = _expect_decimal(
            amount_raw,
            f"{context}.signed_external_amount",
            allow_zero=classification in {"internal", "excluded"},
        )
        if classification == "external_in" and signed_external_amount <= 0:
            raise ManifestValidationError(f"{context} external_in requires a positive amount")
        if classification == "external_out" and signed_external_amount >= 0:
            raise ManifestValidationError(f"{context} external_out requires a negative amount")
        if classification in {"internal", "excluded"} and signed_external_amount != 0:
            raise ManifestValidationError(f"{context} non-external disposition requires zero")
        if disposition in {
            "provider_exact",
            "statement_supplement",
            "provider_supersedes_supplement",
        }:
            if classification not in {"external_in", "external_out"}:
                raise ManifestValidationError(f"{context} external disposition is misclassified")
        elif disposition == "internal" and classification != "internal":
            raise ManifestValidationError(f"{context} internal disposition is misclassified")
        elif disposition == "excluded" and classification != "excluded":
            raise ManifestValidationError(f"{context} excluded disposition is misclassified")
        ledger_effective_date = _expect_date(ledger_date_raw, f"{context}.ledger_effective_date")
        effective_date_basis = _expect_string(date_basis_raw, f"{context}.effective_date_basis")
        if effective_date_basis not in {
            "source_activity",
            "source_process",
            "source_settlement",
            "provider_posting",
            "owner_resolved",
        }:
            raise ManifestValidationError(f"{context}.effective_date_basis is invalid")
        effective_timezone = _expect_string(
            timezone_raw, f"{context}.effective_timezone", maximum=64
        )

    confidence = _expect_string(payload["confidence"], f"{context}.confidence")
    if confidence not in {"exact", "high", "provisional"}:
        raise ManifestValidationError(f"{context}.confidence is invalid")
    assumption_raw = payload["assumption_code"]
    assumption_code = (
        _expect_string(assumption_raw, f"{context}.assumption_code", maximum=64)
        if assumption_raw is not None
        else None
    )
    decision_authority = _expect_string(
        payload["decision_authority"], f"{context}.decision_authority"
    )
    if decision_authority not in {"provider", "brokerage_statement", "owner_approved"}:
        raise ManifestValidationError(f"{context}.decision_authority is invalid")

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
        if disposition != "statement_supplement":
            raise ManifestValidationError(f"{context} manual resolution requires supplement")
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
        if disposition not in {"provider_exact", "provider_supersedes_supplement"}:
            raise ManifestValidationError(f"{context} existing resolution requires provider")
    elif kind_value == "no_transaction":
        _expect_keys(
            resolution,
            required=_NO_TRANSACTION_RESOLUTION_KEYS,
            allowed=_NO_TRANSACTION_RESOLUTION_KEYS,
            context=f"{context}.resolution",
        )
        if disposition not in {"internal", "excluded", "unresolved"}:
            raise ManifestValidationError(f"{context} no-transaction resolution is invalid")
    else:
        raise ManifestValidationError(f"{context}.resolution.kind is invalid")
    if ledger_effective_date is not None:
        if effective_date_basis == "source_activity" and ledger_effective_date != activity_date:
            raise ManifestValidationError(f"{context} activity basis does not match source date")
        if effective_date_basis == "source_process" and (
            process_date is None or ledger_effective_date != process_date
        ):
            raise ManifestValidationError(f"{context} process basis does not match source date")
        if effective_date_basis == "source_settlement" and ledger_effective_date != settlement_date:
            raise ManifestValidationError(f"{context} settlement basis does not match source date")
        if effective_date_basis == "provider_posting" and kind_value != "existing_transaction":
            raise ManifestValidationError(f"{context} provider posting requires provider target")
        if effective_date_basis == "owner_resolved" and (
            decision_authority != "owner_approved" or assumption_code is None
        ):
            raise ManifestValidationError(
                f"{context} owner_resolved requires owner authority and assumption"
            )

    source_event_id = _digest(
        {
            "identity_version": "cashflow_source_row.v2",
            "source_document_sha256": source_sha256,
            "account_identity_sha256": account_identity_sha256,
            "source_row_ordinal": ordinal,
            "source_row_sha256": source_row_sha256,
        }
    )
    return _Event(
        source_event_id=source_event_id,
        source_row_ordinal=ordinal,
        source_row_sha256=source_row_sha256,
        activity_date=activity_date,
        process_date=process_date,
        settlement_date=settlement_date,
        source_amount=source_amount,
        source_amount_sign_basis=source_amount_sign_basis,
        signed_external_amount=signed_external_amount,
        classification=classification,
        source_code=source_code,
        currency=currency,
        disposition=disposition,
        ledger_effective_date=ledger_effective_date,
        effective_date_basis=effective_date_basis,
        effective_timezone=effective_timezone,
        confidence=confidence,
        assumption_code=assumption_code,
        decision_authority=decision_authority,
        resolution_kind=kind_value,
        transaction_id=transaction_id,
        transaction_identity_sha256=identity_sha256,
        expected_transaction_payload_sha256=expected_payload_sha256,
        expected_current_override=expected_override,
    )


def _parse_provider_unresolved_resolution(
    raw: object,
    *,
    index: int,
    events_by_ordinal: Mapping[int, _Event],
) -> _ProviderUnresolvedResolution:
    context = f"provider_unresolved_resolutions[{index}]"
    payload = _expect_object(raw, context)
    _expect_keys(
        payload,
        required=_PROVIDER_UNRESOLVED_RESOLUTION_KEYS,
        allowed=_PROVIDER_UNRESOLVED_RESOLUTION_KEYS,
        context=context,
    )
    ordinal = payload["evidence_source_row_ordinal"]
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
        raise ManifestValidationError(
            f"{context}.evidence_source_row_ordinal must be a positive integer"
        )
    evidence = events_by_ordinal.get(ordinal)
    if evidence is None:
        raise ManifestValidationError(f"{context} references a missing evidence event")
    if (
        evidence.disposition == "unresolved"
        or evidence.confidence == "provisional"
        or evidence.resolution_kind != "existing_transaction"
    ):
        raise ManifestValidationError(
            f"{context} requires resolved non-provisional evidence targeting an existing transaction"
        )
    return _ProviderUnresolvedResolution(
        evidence_source_row_ordinal=ordinal,
        provider_source_event_id=_expect_sha256(
            payload["provider_source_event_id"], f"{context}.provider_source_event_id"
        ),
        expected_provider_source_row_sha256=_expect_sha256(
            payload["expected_provider_source_row_sha256"],
            f"{context}.expected_provider_source_row_sha256",
        ),
        expected_current_decision_key=_expect_sha256(
            payload["expected_current_decision_key"],
            f"{context}.expected_current_decision_key",
        ),
        expected_current_decision_payload_sha256=_expect_sha256(
            payload["expected_current_decision_payload_sha256"],
            f"{context}.expected_current_decision_payload_sha256",
        ),
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
    schema_version = payload.get("schema_version")
    if schema_version == "2":
        required_keys = _REQUIRED_TOP_LEVEL_KEYS_V2
        allowed_keys = _TOP_LEVEL_KEYS_V2
    elif schema_version == "3":
        required_keys = _REQUIRED_TOP_LEVEL_KEYS_V3
        allowed_keys = _TOP_LEVEL_KEYS_V3
    elif schema_version == "4":
        required_keys = _REQUIRED_TOP_LEVEL_KEYS_V4
        allowed_keys = _TOP_LEVEL_KEYS_V4
    else:
        raise ManifestValidationError("manifest.schema_version must be 2, 3, or 4")
    _expect_keys(
        payload,
        required=required_keys,
        allowed=allowed_keys,
        context="manifest",
    )

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
    if schema_version in {"3", "4"}:
        requested_return_start = _expect_date(
            payload["requested_return_start"], "manifest.requested_return_start"
        )
        requested_return_end = _expect_date(
            payload["requested_return_end"], "manifest.requested_return_end"
        )
        if requested_return_start >= requested_return_end:
            raise ManifestValidationError("manifest requested return dates are reversed or empty")
    else:
        # Schema v2 did not separate source coverage from requested return
        # scope. Preserve its conservative historical behavior.
        requested_return_start = None
        requested_return_end = None
    if any(
        _is_in_kind_transfer(row)
        and (
            (
                schema_version in {"3", "4"}
                and requested_return_start is not None
                and requested_return_end is not None
                and requested_return_start
                < _parse_robinhood_date(row.activity_date, "in-kind transfer")
                <= requested_return_end
            )
            or (
                schema_version == "2"
                and coverage_start
                <= _parse_robinhood_date(row.activity_date, "in-kind transfer")
                <= coverage_end
            )
        )
        for row in source_rows
    ):
        raise ManifestValidationError(
            "in-kind transfer inside requested performance window requires explicit reconciliation"
        )
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
    account_mapping_basis = _expect_string(
        payload["account_mapping_basis"], "manifest.account_mapping_basis"
    )
    if account_mapping_basis not in {
        "provider_account_id",
        "statement_account_identifier",
        "owner_confirmed",
    }:
        raise ManifestValidationError("manifest.account_mapping_basis is invalid")
    account_mapping_confidence = _expect_string(
        payload["account_mapping_confidence"], "manifest.account_mapping_confidence"
    )
    if account_mapping_confidence not in {"exact", "high", "provisional"}:
        raise ManifestValidationError("manifest.account_mapping_confidence is invalid")
    source_format = _expect_string(payload["source_format"], "manifest.source_format")
    parser_version = _expect_string(payload["parser_version"], "manifest.parser_version")
    source_timezone = _expect_string(
        payload["source_timezone"], "manifest.source_timezone", maximum=64
    )
    source_row_count = _expect_nonnegative_int(
        payload["source_row_count"], "manifest.source_row_count"
    )
    cashflow_candidate_count = _expect_nonnegative_int(
        payload["cashflow_candidate_count"], "manifest.cashflow_candidate_count"
    )
    source_event_set_sha256 = _expect_sha256(
        payload["source_event_set_sha256"], "manifest.source_event_set_sha256"
    )
    if source_row_count != len(source_rows):
        raise ManifestValidationError("manifest source row count does not match evidence")

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
    if schema_version in {"3", "4"}:
        assert requested_return_start is not None
        assert requested_return_end is not None
        outside_window_in_kind_dates = {
            _parse_robinhood_date(row.activity_date, "in-kind transfer")
            for row in source_rows
            if _is_in_kind_transfer(row)
            and not requested_return_start
            < _parse_robinhood_date(row.activity_date, "in-kind transfer")
            <= requested_return_end
        }
        for in_kind_date in outside_window_in_kind_dates:
            if not any(
                gap.reason_code == "unreconciled_difference"
                and gap.gap_start <= in_kind_date <= gap.gap_end
                for gap in gaps
            ):
                raise ManifestValidationError(
                    "out-of-window in-kind transfer requires an explicit source gap"
                )

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
    events_by_ordinal = {event.source_row_ordinal: event for event in events}
    if schema_version == "4":
        if source_type not in {"brokerage_statement", "owner_reconciliation"}:
            raise ManifestValidationError(
                "provider unresolved resolutions require statement or owner evidence"
            )
        raw_resolutions = payload["provider_unresolved_resolutions"]
        if not isinstance(raw_resolutions, list):
            raise ManifestValidationError("manifest.provider_unresolved_resolutions must be a list")
        provider_unresolved_resolutions = tuple(
            _parse_provider_unresolved_resolution(
                raw_resolution,
                index=index,
                events_by_ordinal=events_by_ordinal,
            )
            for index, raw_resolution in enumerate(cast(list[object], raw_resolutions))
        )
        provider_event_ids = [
            resolution.provider_source_event_id for resolution in provider_unresolved_resolutions
        ]
        evidence_ordinals = [
            resolution.evidence_source_row_ordinal for resolution in provider_unresolved_resolutions
        ]
        if len(provider_event_ids) != len(set(provider_event_ids)):
            raise ManifestValidationError(
                "manifest contains duplicate provider unresolved source-event identity"
            )
        if len(evidence_ordinals) != len(set(evidence_ordinals)):
            raise ManifestValidationError(
                "one evidence event cannot resolve multiple provider unresolved events"
            )
    else:
        provider_unresolved_resolutions = ()
    if source_type == "brokerage_statement" and any(
        event.ledger_effective_date is not None
        and not (
            event.effective_date_basis == "source_activity"
            and event.ledger_effective_date == event.activity_date
        )
        and not (
            schema_version == "4"
            and event.resolution_kind == "existing_transaction"
            and event.disposition == "provider_exact"
            and event.effective_date_basis == "owner_resolved"
            and event.decision_authority == "owner_approved"
            and event.assumption_code == _CORROBORATING_PROVIDER_RESOLVED_ASSUMPTION
        )
        for event in events
    ):
        raise ManifestValidationError("statement-backed events must use the source activity date")
    ordinals = [event.source_row_ordinal for event in events]
    if len(ordinals) != len(set(ordinals)):
        raise ManifestValidationError("manifest source_row_ordinal values must be unique")
    parsed_candidate_ordinals = {
        ordinal
        for ordinal, row in enumerate(source_rows, start=1)
        if _external_cash_candidate_amount(row, f"evidence source row {ordinal}") is not None
    }
    event_ordinals = set(ordinals)
    if parsed_candidate_ordinals != event_ordinals:
        raise ManifestValidationError(
            "manifest must disposition every source cashflow candidate exactly once"
        )
    if cashflow_candidate_count != len(parsed_candidate_ordinals):
        raise ManifestValidationError("manifest cashflow candidate count does not match evidence")
    actual_event_set_sha256 = _digest(
        sorted(source_rows[ordinal - 1].source_row_sha256 for ordinal in event_ordinals)
    )
    if source_event_set_sha256 != actual_event_set_sha256:
        raise ManifestValidationError("manifest source event set hash does not match evidence")
    unresolved_dates = {
        event.activity_date for event in events if event.disposition == "unresolved"
    }
    for unresolved_date in unresolved_dates:
        if not any(
            gap.reason_code == "unresolved_classification"
            and gap.gap_start <= unresolved_date <= gap.gap_end
            for gap in gaps
        ):
            raise ManifestValidationError("unresolved candidate requires an explicit source gap")

    identity_payload = {
        "identity_version": "cashflow_attestation.v2",
        "account_identity_sha256": account_identity,
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "source_type": source_type,
        "source_document_sha256": source_sha256,
        "methodology_version": methodology_version,
        "account_mapping_basis": account_mapping_basis,
        "account_mapping_confidence": account_mapping_confidence,
        "source_format": source_format,
        "parser_version": parser_version,
        "source_timezone": source_timezone,
        "source_row_count": source_row_count,
        "cashflow_candidate_count": cashflow_candidate_count,
        "source_event_set_sha256": source_event_set_sha256,
        "gaps": [gap.digest_payload() for gap in gaps],
        "source_event_ids": sorted(event.source_event_id for event in events),
    }
    identity_digest = _digest(identity_payload)
    attestation_key = f"cashflow:v2:{identity_digest[:52]}"
    manifest_payload = {
        **identity_payload,
        "source_reference_hash": _sha256_text(source_reference),
        "captured_at": _datetime_text(captured_at),
        "events": [event.digest_payload() for event in events],
    }
    if schema_version in {"3", "4"}:
        assert requested_return_start is not None
        assert requested_return_end is not None
        manifest_payload.update(
            {
                "requested_return_start": requested_return_start.isoformat(),
                "requested_return_end": requested_return_end.isoformat(),
            }
        )
    if schema_version == "4":
        manifest_payload["provider_unresolved_resolutions"] = [
            resolution.digest_payload() for resolution in provider_unresolved_resolutions
        ]
    manifest_digest = _digest(manifest_payload)
    return _Manifest(
        source=source,
        schema_version=cast(Literal["2", "3", "4"], schema_version),
        account_id=account_id,
        account_identity_sha256=account_identity,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        requested_return_start=requested_return_start,
        requested_return_end=requested_return_end,
        source_type=source_type,
        source_reference=source_reference,
        source_document_sha256=source_sha256,
        captured_at=captured_at,
        methodology_version=methodology_version,
        account_mapping_basis=account_mapping_basis,
        account_mapping_confidence=account_mapping_confidence,
        source_format=source_format,
        parser_version=parser_version,
        source_timezone=source_timezone,
        source_row_count=source_row_count,
        cashflow_candidate_count=cashflow_candidate_count,
        source_event_set_sha256=source_event_set_sha256,
        gaps=tuple(gaps),
        events=events,
        provider_unresolved_resolutions=provider_unresolved_resolutions,
        attestation_key=attestation_key,
        attestation_manifest_sha256=identity_digest,
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
        and abs((tx.date - event.activity_date).days) <= 14
        and tx.currency == event.currency
        and tx.security_id is None
        and tx.quantity == 0
        and event.signed_external_amount is not None
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
    if event.resolution_kind == "no_transaction":
        return PlanEntry(
            event.source_event_id,
            "excluded",
            f"source_event_{event.disposition}",
            manifest.account_id,
            event,
            None,
            None,
            None,
        )
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
    elif (
        event.effective_date_basis == "provider_posting" and event.ledger_effective_date != tx.date
    ):
        reason = "provider_posting_basis_does_not_match_target_date"
    elif (
        manifest.schema_version == "4"
        and manifest.source_type == "brokerage_statement"
        and event.effective_date_basis == "owner_resolved"
        and event.assumption_code == _CORROBORATING_PROVIDER_RESOLVED_ASSUMPTION
        and event.ledger_effective_date != tx.date
    ):
        reason = "provider_corroboration_date_does_not_match_target_date"
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


def _source_event_values(event: _Event, attestation_id: int) -> dict[str, object]:
    return {
        "source_event_id": event.source_event_id,
        "attestation_id": attestation_id,
        "source_record_id": None,
        "source_locator_kind": "row",
        "source_locator": f"row:{event.source_row_ordinal}",
        "source_row_ordinal": event.source_row_ordinal,
        "source_page": None,
        "source_line": None,
        "source_row_sha256": event.source_row_sha256,
        "activity_date": event.activity_date,
        "process_date": event.process_date,
        "settlement_date": event.settlement_date,
        "source_amount": event.source_amount,
        "source_amount_sign_basis": event.source_amount_sign_basis,
        "currency": event.currency,
        "source_code": event.source_code,
    }


def _source_event_matches(row: CashFlowSourceEvent, values: Mapping[str, object]) -> bool:
    return all(getattr(row, key) == value for key, value in values.items())


def _decision_values(event: _Event, target_transaction_id: str | None) -> dict[str, object]:
    payload = {
        "source_event_id": event.source_event_id,
        "target_transaction_id": target_transaction_id,
        "resolution_kind": event.disposition,
        "classification": event.classification,
        "signed_external_amount": event.signed_external_amount,
        "effective_date": event.ledger_effective_date,
        "effective_date_basis": event.effective_date_basis,
        "effective_timezone": event.effective_timezone,
        "decision_authority": event.decision_authority,
        "confidence": event.confidence,
        "assumption_code": event.assumption_code,
        "methodology_version": "2",
    }
    payload_sha256 = _digest(
        {
            key: (
                value.isoformat()
                if isinstance(value, date)
                else _decimal_text(value)
                if isinstance(value, Decimal)
                else value
            )
            for key, value in payload.items()
        }
    )
    return {
        "decision_key": _digest(
            {
                "identity_version": "cashflow_reconciliation_decision.v1",
                "source_event_id": event.source_event_id,
                "decision_payload_sha256": payload_sha256,
            }
        ),
        **payload,
        "decision_payload_sha256": payload_sha256,
    }


def _decision_matches(
    row: CashFlowReconciliationDecision,
    values: Mapping[str, object],
) -> bool:
    return all(getattr(row, key) == value for key, value in values.items())


def _provider_resolution_decision_values(
    evidence: _Event,
    provider_source_event_id: str,
    target_transaction_id: str,
) -> dict[str, object]:
    if evidence.classification in {"external_in", "external_out"}:
        disposition: Disposition = "provider_exact"
    elif evidence.classification == "internal":
        disposition = "internal"
    elif evidence.classification == "excluded":
        disposition = "excluded"
    else:
        raise ManifestValidationError("provider unresolved replacement evidence is unresolved")
    owner_resolution = replace(
        evidence,
        source_event_id=provider_source_event_id,
        disposition=disposition,
        effective_date_basis="owner_resolved",
        decision_authority="owner_approved",
        assumption_code=_CORROBORATING_PROVIDER_RESOLUTION_ASSUMPTION,
    )
    return _decision_values(owner_resolution, target_transaction_id)


def _plan_provider_unresolved_resolution(
    session: Session,
    manifest: _Manifest,
    reference: _ProviderUnresolvedResolution,
    evidence_entry: PlanEntry,
) -> _ProviderUnresolvedResolutionPlan:
    def conflict(
        reason_code: str,
        desired: dict[str, object] | None = None,
    ) -> _ProviderUnresolvedResolutionPlan:
        return _ProviderUnresolvedResolutionPlan(
            evidence_source_event_id=evidence_entry.source_event_id,
            provider_source_event_id=reference.provider_source_event_id,
            expected_provider_source_row_sha256=reference.expected_provider_source_row_sha256,
            expected_current_decision_key=reference.expected_current_decision_key,
            expected_current_decision_payload_sha256=(
                reference.expected_current_decision_payload_sha256
            ),
            desired_decision_values=desired or {},
            status="conflict",
            reason_code=reason_code,
            planned_mutation_count=0,
        )

    if manifest.source_type not in {"brokerage_statement", "owner_reconciliation"}:
        return conflict("provider_resolution_evidence_authority_invalid")
    if evidence_entry.status == "conflict" or evidence_entry.resolved_transaction_id is None:
        return conflict("provider_resolution_evidence_target_unavailable")
    provider_event = session.get(CashFlowSourceEvent, reference.provider_source_event_id)
    if provider_event is None:
        return conflict("provider_unresolved_source_event_missing")
    provider_attestation = session.get(CashFlowSourceAttestation, provider_event.attestation_id)
    if (
        provider_attestation is None
        or provider_attestation.source_type != "provider_api"
        or provider_attestation.account_id != manifest.account_id
        or provider_attestation.approved_at is None
    ):
        return conflict("provider_unresolved_source_event_authority_mismatch")
    if (
        provider_event.source_locator_kind != "provider_record"
        or provider_event.source_record_id is None
        or provider_event.source_record_id != evidence_entry.resolved_transaction_id
    ):
        return conflict("provider_unresolved_target_identity_mismatch")
    if provider_event.source_row_sha256 != reference.expected_provider_source_row_sha256:
        return conflict("provider_unresolved_source_row_digest_mismatch")

    desired = _provider_resolution_decision_values(
        evidence_entry.event,
        reference.provider_source_event_id,
        evidence_entry.resolved_transaction_id,
    )
    current_rows = tuple(
        session.scalars(
            select(CashFlowReconciliationDecision).where(
                CashFlowReconciliationDecision.source_event_id
                == reference.provider_source_event_id,
                CashFlowReconciliationDecision.superseded_at.is_(None),
            )
        )
    )
    expected = session.get(CashFlowReconciliationDecision, reference.expected_current_decision_key)
    desired_key = cast(str, desired["decision_key"])
    if len(current_rows) == 1 and _decision_matches(current_rows[0], desired):
        current = current_rows[0]
        if (
            current.approved_at is None
            or current.confidence == "provisional"
            or expected is None
            or expected.source_event_id != reference.provider_source_event_id
            or expected.decision_payload_sha256
            != reference.expected_current_decision_payload_sha256
            or expected.superseded_at is None
            or expected.superseded_by_decision_key != desired_key
        ):
            return conflict("provider_unresolved_supersession_receipt_mismatch", desired)
        return _ProviderUnresolvedResolutionPlan(
            evidence_entry.source_event_id,
            reference.provider_source_event_id,
            reference.expected_provider_source_row_sha256,
            reference.expected_current_decision_key,
            reference.expected_current_decision_payload_sha256,
            desired,
            "existing_exact",
            "provider_unresolved_supersession_exact",
            0,
        )
    if len(current_rows) != 1:
        return conflict("provider_unresolved_current_decision_not_unique", desired)
    current = current_rows[0]
    if (
        current.decision_key != reference.expected_current_decision_key
        or current.decision_payload_sha256 != reference.expected_current_decision_payload_sha256
        or canonical_decision_payload_sha256(current)
        != reference.expected_current_decision_payload_sha256
    ):
        return conflict("provider_unresolved_current_decision_digest_mismatch", desired)
    if (
        current.resolution_kind != "unresolved"
        or current.decision_authority != "provider"
        or current.methodology_version != "provider-api-v1"
        or current.confidence != "provisional"
        or current.approved_at is None
        or current.target_transaction_id is not None
        or current.assumption_code not in _PROVIDER_UNRESOLVED_ASSUMPTION_CODES
    ):
        return conflict("provider_unresolved_current_decision_not_provider_created", desired)
    return _ProviderUnresolvedResolutionPlan(
        evidence_entry.source_event_id,
        reference.provider_source_event_id,
        reference.expected_provider_source_row_sha256,
        reference.expected_current_decision_key,
        reference.expected_current_decision_payload_sha256,
        desired,
        "supersession_required",
        "provider_unresolved_decision_requires_supersession",
        2,
    )


def _provenance_disposition(
    session: Session,
    manifest: _Manifest,
    entry: PlanEntry,
) -> tuple[int, str | None]:
    attestation = session.scalar(
        select(CashFlowSourceAttestation).where(
            CashFlowSourceAttestation.attestation_key == manifest.attestation_key
        )
    )
    source_event = session.get(CashFlowSourceEvent, entry.source_event_id)
    source_mutations = 0
    if source_event is None:
        source_mutations = 1
    elif attestation is None or not _source_event_matches(
        source_event,
        _source_event_values(entry.event, attestation.attestation_id),
    ):
        return 0, "source_event_drift"

    desired = _decision_values(entry.event, entry.resolved_transaction_id)
    current = session.scalar(
        select(CashFlowReconciliationDecision).where(
            CashFlowReconciliationDecision.source_event_id == entry.source_event_id,
            CashFlowReconciliationDecision.superseded_at.is_(None),
        )
    )
    if current is None:
        return source_mutations + 1, None
    if _decision_matches(current, desired):
        return source_mutations, None
    if (
        entry.event.disposition == "provider_supersedes_supplement"
        and current.resolution_kind == "statement_supplement"
    ):
        return source_mutations + 3, None
    return 0, "current_reconciliation_decision_conflict"


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
        "account_identity_sha256": attestation.account_identity_sha256,
        "account_mapping_basis": attestation.account_mapping_basis,
        "account_mapping_confidence": attestation.account_mapping_confidence,
        "source_format": attestation.source_format,
        "parser_version": attestation.parser_version,
        "source_timezone": attestation.source_timezone,
        "source_row_count": attestation.source_row_count,
        "cashflow_candidate_count": attestation.cashflow_candidate_count,
        "source_event_set_sha256": attestation.source_event_set_sha256,
        "manifest_sha256": attestation.manifest_sha256,
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
    schema_versions = {manifest.schema_version for manifest in manifests}
    if len(schema_versions) != 1:
        raise ManifestValidationError("different manifest schema versions cannot be mixed")
    if schema_versions in ({"3"}, {"4"}):
        requested_windows = {
            (manifest.requested_return_start, manifest.requested_return_end)
            for manifest in manifests
        }
        if len(requested_windows) != 1:
            raise ManifestValidationError("manifests do not share one requested return window")
        requested_return_start, requested_return_end = next(iter(requested_windows))
        assert requested_return_start is not None
        assert requested_return_end is not None
    else:
        requested_return_start = None
        requested_return_end = None
    manifest_digests = [manifest.manifest_digest for manifest in manifests]
    if len(manifest_digests) != len(set(manifest_digests)):
        raise ManifestValidationError("duplicate manifest source")
    all_event_ids = [event.source_event_id for manifest in manifests for event in manifest.events]
    if len(all_event_ids) != len(set(all_event_ids)):
        raise ManifestValidationError("duplicate stable source-row identity across manifests")
    all_provider_resolution_ids = [
        resolution.provider_source_event_id
        for manifest in manifests
        for resolution in manifest.provider_unresolved_resolutions
    ]
    if len(all_provider_resolution_ids) != len(set(all_provider_resolution_ids)):
        raise ManifestValidationError(
            "duplicate provider unresolved source-event identity across manifests"
        )

    entries = tuple(
        _plan_event(session, manifest, event) for manifest in manifests for event in manifest.events
    )
    manifest_by_event = {
        event.source_event_id: manifest for manifest in manifests for event in manifest.events
    }
    provenance_mutations = 0
    reconciled_entries: list[PlanEntry] = []
    for entry in entries:
        mutation_count, conflict_reason = _provenance_disposition(
            session,
            manifest_by_event[entry.source_event_id],
            entry,
        )
        provenance_mutations += mutation_count
        reconciled_entries.append(
            replace(entry, status="conflict", reason_code=conflict_reason)
            if conflict_reason is not None
            else entry
        )
    entries = tuple(reconciled_entries)
    classifications_by_target: dict[str, set[str]] = {}
    for entry in entries:
        if entry.resolved_transaction_id is None or entry.event.classification is None:
            continue
        classifications_by_target.setdefault(entry.resolved_transaction_id, set()).add(
            entry.event.classification
        )
    conflicting_targets = {
        target_id
        for target_id, classifications in classifications_by_target.items()
        if len(classifications) > 1
    }
    if conflicting_targets:
        entries = tuple(
            replace(
                entry,
                status="conflict",
                reason_code="shared_transaction_classification_conflict",
            )
            if entry.resolved_transaction_id in conflicting_targets
            else entry
            for entry in entries
        )
    entries_by_manifest_ordinal = {
        (manifest.manifest_digest, entry.event.source_row_ordinal): entry
        for manifest in manifests
        for entry in entries
        if entry.source_event_id in {event.source_event_id for event in manifest.events}
    }
    provider_unresolved_resolutions = tuple(
        _plan_provider_unresolved_resolution(
            session,
            manifest,
            reference,
            entries_by_manifest_ordinal[
                (manifest.manifest_digest, reference.evidence_source_row_ordinal)
            ],
        )
        for manifest in manifests
        for reference in manifest.provider_unresolved_resolutions
    )
    attestations = tuple(_plan_attestation(session, manifest) for manifest in manifests)
    counts = Counter(entry.status for entry in entries)
    status_counts = {status: counts.get(status, 0) for status in _STATUS_ORDER}
    conflict_count = status_counts["conflict"] + sum(
        attestation.status == "conflict" for attestation in attestations
    )
    conflict_count += sum(
        resolution.status == "conflict" for resolution in provider_unresolved_resolutions
    )
    manual_insert_ids = {
        entry.resolved_transaction_id for entry in entries if entry.status == "missing_insert"
    }
    override_target_ids = {
        entry.resolved_transaction_id for entry in entries if entry.status == "override_required"
    }
    planned_mutations = (
        2 * len(manual_insert_ids)
        + len(override_target_ids)
        + sum(attestation.planned_mutation_count for attestation in attestations)
    )
    planned_mutations += provenance_mutations
    planned_mutations += sum(
        resolution.planned_mutation_count for resolution in provider_unresolved_resolutions
    )
    if planned_mutations:
        planned_mutations += 1  # durable applied-run receipt
    plan_payload: dict[str, object] = {
        "plan_version": "cashflow_reconciliation.v1",
        "manifests": [manifest.manifest_digest for manifest in manifests],
        "entries": [entry.digest_payload() for entry in entries],
        "attestations": [attestation.digest_payload() for attestation in attestations],
        "provider_unresolved_resolutions": [
            resolution.digest_payload() for resolution in provider_unresolved_resolutions
        ],
        "planned_mutation_count": planned_mutations,
        "conflict_count": conflict_count,
    }
    if requested_return_start is not None and requested_return_end is not None:
        plan_payload.update(
            {
                "requested_return_start": requested_return_start.isoformat(),
                "requested_return_end": requested_return_end.isoformat(),
            }
        )
    return ReconciliationPlan(
        sources=normalized_sources,
        manifests=manifests,
        entries=entries,
        attestations=attestations,
        provider_unresolved_resolutions=provider_unresolved_resolutions,
        requested_return_start=requested_return_start,
        requested_return_end=requested_return_end,
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


def _validate_decision_chain(
    session: Session,
    source_event_id: str,
) -> None:
    rows = tuple(
        session.scalars(
            select(CashFlowReconciliationDecision).where(
                CashFlowReconciliationDecision.source_event_id == source_event_id
            )
        )
    )
    by_key = {row.decision_key: row for row in rows}
    for row in rows:
        seen: set[str] = set()
        cursor = row
        while cursor.superseded_by_decision_key is not None:
            if cursor.decision_key in seen:
                raise ReconciliationConflictError("reconciliation decision cycle detected")
            seen.add(cursor.decision_key)
            successor = by_key.get(cursor.superseded_by_decision_key)
            if successor is None or successor.source_event_id != source_event_id:
                raise ReconciliationConflictError("reconciliation decision chain is invalid")
            cursor = successor


def _validate_attestation_chains(session: Session, account_ids: set[int]) -> None:
    rows = tuple(
        session.scalars(
            select(CashFlowSourceAttestation).where(
                CashFlowSourceAttestation.account_id.in_(account_ids)
            )
        )
    )
    by_id = {row.attestation_id: row for row in rows}
    for row in rows:
        seen: set[int] = set()
        cursor = row
        while cursor.superseded_by_attestation_id is not None:
            if cursor.attestation_id in seen:
                raise ReconciliationConflictError("source attestation cycle detected")
            seen.add(cursor.attestation_id)
            successor = by_id.get(cursor.superseded_by_attestation_id)
            if successor is None or successor.account_id != row.account_id:
                raise ReconciliationConflictError(
                    "source attestation replacement crosses account boundary"
                )
            cursor = successor


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
            account_identity_sha256=manifest.account_identity_sha256,
            account_mapping_basis=manifest.account_mapping_basis,
            account_mapping_confidence=manifest.account_mapping_confidence,
            source_format=manifest.source_format,
            parser_version=manifest.parser_version,
            source_timezone=manifest.source_timezone,
            source_row_count=manifest.source_row_count,
            cashflow_candidate_count=manifest.cashflow_candidate_count,
            source_event_set_sha256=manifest.source_event_set_sha256,
            manifest_sha256=manifest.attestation_manifest_sha256,
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


def _persist_source_events_and_decisions(
    session: Session,
    plan: ReconciliationPlan,
    approved_at: datetime,
) -> tuple[int, tuple[_DecisionMembership, ...], tuple[_TransactionMutationReceipt, ...]]:
    mutations = 0
    memberships: list[_DecisionMembership] = []
    transaction_mutations: list[_TransactionMutationReceipt] = []
    manifest_by_event = {
        event.source_event_id: manifest for manifest in plan.manifests for event in manifest.events
    }
    for entry in plan.entries:
        manifest = manifest_by_event[entry.source_event_id]
        attestation = session.scalar(
            select(CashFlowSourceAttestation).where(
                CashFlowSourceAttestation.attestation_key == manifest.attestation_key
            )
        )
        if attestation is None:
            raise ReconciliationConflictError("source attestation is unavailable")
        source_values = _source_event_values(entry.event, attestation.attestation_id)
        source_event = session.get(CashFlowSourceEvent, entry.source_event_id)
        if source_event is None:
            session.add(CashFlowSourceEvent(**source_values))
            # SessionLocal disables autoflush, and these provenance mappers do not
            # declare an ORM relationship.  Materialize the parent before queuing
            # its reconciliation decision so foreign-key enforcement cannot let
            # SQLAlchemy emit the child first.
            session.flush()
            mutations += 1
        elif not _source_event_matches(source_event, source_values):
            raise ReconciliationConflictError("source event changed before commit")

        decision_values = _decision_values(entry.event, entry.resolved_transaction_id)
        decision_key = cast(str, decision_values["decision_key"])
        _validate_decision_chain(session, entry.source_event_id)
        current = session.scalar(
            select(CashFlowReconciliationDecision).where(
                CashFlowReconciliationDecision.source_event_id == entry.source_event_id,
                CashFlowReconciliationDecision.superseded_at.is_(None),
            )
        )
        if current is not None and _decision_matches(current, decision_values):
            memberships.append(_DecisionMembership(current.decision_key, "verified"))
            continue
        if current is not None:
            if not (
                entry.event.disposition == "provider_supersedes_supplement"
                and current.resolution_kind == "statement_supplement"
            ):
                raise ReconciliationConflictError("reconciliation decision changed before commit")
            old_target = current.target_transaction_id
            current.superseded_at = approved_at
            current.superseded_by_decision_key = decision_key
            memberships.append(_DecisionMembership(current.decision_key, "superseded"))
            mutations += 1
            if old_target is not None:
                old_override = session.get(TransactionOverride, old_target)
                notes = "Provider transaction supersedes statement supplement"
                if old_override is None:
                    session.add(
                        TransactionOverride(
                            plaid_investment_transaction_id=old_target,
                            classification="internal",
                            notes=notes,
                        )
                    )
                    mutation_kind = "override_insert"
                    before_digest = None
                else:
                    before_digest = _override_payload_sha256(
                        old_override.classification,
                        old_override.notes or "",
                    )
                    old_override.classification = "internal"
                    old_override.notes = notes
                    mutation_kind = "override_update"
                transaction_mutations.append(
                    _TransactionMutationReceipt(
                        old_target,
                        mutation_kind,
                        before_digest,
                        _override_payload_sha256("internal", notes),
                    )
                )
                mutations += 1
        session.add(
            CashFlowReconciliationDecision(
                **decision_values,
                approved_at=approved_at,
                superseded_at=None,
                superseded_by_decision_key=None,
            )
        )
        memberships.append(_DecisionMembership(decision_key, "created"))
        mutations += 1
    return mutations, tuple(memberships), tuple(transaction_mutations)


def _persist_provider_unresolved_resolutions(
    session: Session,
    plan: ReconciliationPlan,
    approved_at: datetime,
) -> tuple[int, tuple[_DecisionMembership, ...]]:
    mutations = 0
    memberships: list[_DecisionMembership] = []
    for resolution in plan.provider_unresolved_resolutions:
        desired_key = cast(str, resolution.desired_decision_values["decision_key"])
        current = session.scalar(
            select(CashFlowReconciliationDecision).where(
                CashFlowReconciliationDecision.source_event_id
                == resolution.provider_source_event_id,
                CashFlowReconciliationDecision.superseded_at.is_(None),
            )
        )
        if resolution.status == "existing_exact":
            if current is None or not _decision_matches(
                current, resolution.desired_decision_values
            ):
                raise ReconciliationConflictError(
                    "provider unresolved supersession changed before commit"
                )
            memberships.append(_DecisionMembership(current.decision_key, "verified"))
            continue
        if resolution.status != "supersession_required" or current is None:
            raise ReconciliationConflictError("provider unresolved supersession is not applicable")
        if (
            current.decision_key != resolution.expected_current_decision_key
            or current.decision_payload_sha256
            != resolution.expected_current_decision_payload_sha256
            or current.resolution_kind != "unresolved"
            or current.decision_authority != "provider"
            or current.methodology_version != "provider-api-v1"
        ):
            raise ReconciliationConflictError("provider unresolved decision changed before commit")
        current.superseded_at = approved_at
        current.superseded_by_decision_key = desired_key
        memberships.append(_DecisionMembership(current.decision_key, "superseded"))
        # Retire the partial-unique current row before inserting its successor.
        session.flush()
        session.add(
            CashFlowReconciliationDecision(
                **resolution.desired_decision_values,
                approved_at=approved_at,
                superseded_at=None,
                superseded_by_decision_key=None,
            )
        )
        memberships.append(_DecisionMembership(desired_key, "created"))
        mutations += 2
    session.flush()
    return mutations, tuple(memberships)


def _persist_run_receipt(
    session: Session,
    plan: ReconciliationPlan,
    *,
    approved_at: datetime,
    software_revision: str,
    backup_reference: str,
    preview_reference: str,
    applied_mutation_count: int,
) -> tuple[int, str | None]:
    if applied_mutation_count == 0:
        return 0, None
    manifest_set_sha256 = _digest(sorted(item.manifest_digest for item in plan.manifests))
    run_id = _digest(
        {
            "plan_digest": plan.plan_digest,
            "manifest_set_sha256": manifest_set_sha256,
            "software_revision": software_revision,
            "backup_reference": backup_reference,
            "preview_reference": preview_reference,
        }
    )
    existing = session.get(CashFlowReconciliationRun, run_id)
    if existing is not None:
        if existing.plan_digest != plan.plan_digest:
            raise ReconciliationConflictError("run receipt identity conflict")
        return 0, run_id
    session.add(
        CashFlowReconciliationRun(
            run_id=run_id,
            plan_digest=plan.plan_digest,
            manifest_set_sha256=manifest_set_sha256,
            software_revision=software_revision,
            backup_reference=backup_reference,
            preview_reference=preview_reference,
            affected_start=min(item.coverage_start for item in plan.manifests),
            affected_end=max(item.coverage_end for item in plan.manifests),
            requested_return_start=plan.requested_return_start,
            requested_return_end=plan.requested_return_end,
            affected_account_count=len({item.account_id for item in plan.manifests}),
            source_event_count=len(plan.entries) + len(plan.provider_unresolved_resolutions),
            planned_mutation_count=plan.planned_mutation_count,
            applied_mutation_count=applied_mutation_count + 1,
            status="applied",
            approved_at=approved_at,
            applied_at=approved_at,
        )
    )
    return 1, run_id


def _persist_run_memberships(
    session: Session,
    run_id: str,
    decision_memberships: Sequence[_DecisionMembership],
    transaction_mutations: Sequence[_TransactionMutationReceipt],
) -> None:
    decision_keys: set[str] = set()
    for membership in decision_memberships:
        if membership.decision_key in decision_keys:
            raise ReconciliationConflictError("duplicate decision run membership")
        decision_keys.add(membership.decision_key)
        session.add(
            CashFlowReconciliationRunDecision(
                run_id=run_id,
                decision_key=membership.decision_key,
                membership_kind=membership.membership_kind,
            )
        )
    mutation_keys: set[tuple[str, str]] = set()
    for mutation in transaction_mutations:
        key = (mutation.target_transaction_id, mutation.mutation_kind)
        if key in mutation_keys:
            raise ReconciliationConflictError("duplicate transaction mutation receipt")
        mutation_keys.add(key)
        session.add(
            CashFlowReconciliationRunTransactionMutation(
                run_id=run_id,
                target_transaction_id=mutation.target_transaction_id,
                mutation_kind=mutation.mutation_kind,
                before_payload_sha256=mutation.before_payload_sha256,
                after_payload_sha256=mutation.after_payload_sha256,
            )
        )


def _verify_run_receipt(
    session: Session,
    plan: ReconciliationPlan,
    run_id: str | None,
    decision_memberships: Sequence[_DecisionMembership],
    transaction_mutations: Sequence[_TransactionMutationReceipt],
) -> None:
    if run_id is None:
        if decision_memberships or transaction_mutations:
            raise ReconciliationConflictError("run membership exists without run receipt")
        return
    run = session.get(CashFlowReconciliationRun, run_id)
    if (
        run is None
        or run.plan_digest != plan.plan_digest
        or run.requested_return_start != plan.requested_return_start
        or run.requested_return_end != plan.requested_return_end
        or run.planned_mutation_count != plan.planned_mutation_count
        or run.applied_mutation_count != plan.planned_mutation_count
    ):
        raise ReconciliationConflictError("run receipt verification failed")
    persisted_decisions = tuple(
        session.scalars(
            select(CashFlowReconciliationRunDecision).where(
                CashFlowReconciliationRunDecision.run_id == run_id
            )
        )
    )
    if {(row.decision_key, row.membership_kind) for row in persisted_decisions} != {
        (item.decision_key, item.membership_kind) for item in decision_memberships
    }:
        raise ReconciliationConflictError("decision run membership verification failed")
    persisted_mutations = tuple(
        session.scalars(
            select(CashFlowReconciliationRunTransactionMutation).where(
                CashFlowReconciliationRunTransactionMutation.run_id == run_id
            )
        )
    )
    if {
        (
            row.target_transaction_id,
            row.mutation_kind,
            row.before_payload_sha256,
            row.after_payload_sha256,
        )
        for row in persisted_mutations
    } != {
        (
            item.target_transaction_id,
            item.mutation_kind,
            item.before_payload_sha256,
            item.after_payload_sha256,
        )
        for item in transaction_mutations
    }:
        raise ReconciliationConflictError("transaction mutation receipt verification failed")


def _verify_applied_state(session: Session, plan: ReconciliationPlan) -> None:
    """Verify all intended authorities before allowing the transaction to commit."""
    for entry in plan.entries:
        if entry.resolved_transaction_id is None:
            continue
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
        for event in manifest.events:
            _validate_decision_chain(session, event.source_event_id)
            source_event = session.get(CashFlowSourceEvent, event.source_event_id)
            if source_event is None or not _source_event_matches(
                source_event,
                _source_event_values(event, attestation.attestation_id),
            ):
                raise ReconciliationConflictError("source event write verification failed")
            current_decision = session.scalar(
                select(CashFlowReconciliationDecision).where(
                    CashFlowReconciliationDecision.source_event_id == event.source_event_id,
                    CashFlowReconciliationDecision.superseded_at.is_(None),
                )
            )
            target = next(
                entry.resolved_transaction_id
                for entry in plan.entries
                if entry.source_event_id == event.source_event_id
            )
            expected_decision = _decision_values(event, target)
            if (
                current_decision is None
                or current_decision.approved_at is None
                or not _decision_matches(
                    current_decision,
                    expected_decision,
                )
            ):
                raise ReconciliationConflictError("decision write verification failed")

    for resolution in plan.provider_unresolved_resolutions:
        provider_event = session.get(CashFlowSourceEvent, resolution.provider_source_event_id)
        if (
            provider_event is None
            or provider_event.source_row_sha256 != resolution.expected_provider_source_row_sha256
        ):
            raise ReconciliationConflictError(
                "provider unresolved source-event verification failed"
            )
        current = session.scalar(
            select(CashFlowReconciliationDecision).where(
                CashFlowReconciliationDecision.source_event_id
                == resolution.provider_source_event_id,
                CashFlowReconciliationDecision.superseded_at.is_(None),
            )
        )
        previous = session.get(
            CashFlowReconciliationDecision, resolution.expected_current_decision_key
        )
        desired_key = cast(str, resolution.desired_decision_values["decision_key"])
        if (
            current is None
            or current.approved_at is None
            or not _decision_matches(current, resolution.desired_decision_values)
            or previous is None
            or previous.decision_payload_sha256
            != resolution.expected_current_decision_payload_sha256
            or previous.superseded_at is None
            or previous.superseded_by_decision_key != desired_key
        ):
            raise ReconciliationConflictError(
                "provider unresolved decision supersession verification failed"
            )
        _validate_decision_chain(session, resolution.provider_source_event_id)

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
    software_revision: str,
    backup_reference: str,
    preview_reference: str,
) -> ReconciliationResult:
    """Atomically apply an exact, conflict-free plan after locked revalidation."""
    if expected_plan_digest is None or expected_plan_digest != plan.plan_digest:
        raise ReconciliationConflictError("exact plan digest approval is required")
    approved_at = _validate_approval_time(approved_at, plan.manifests)
    software_revision = _expect_string(software_revision, "software_revision", maximum=64)
    backup_reference = _expect_string(backup_reference, "backup_reference", maximum=512)
    preview_reference = _expect_string(preview_reference, "preview_reference", maximum=512)
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

        applied_mutation_count = 0
        transaction_mutations: list[_TransactionMutationReceipt] = []
        economically_mutated_targets: set[str] = set()
        for entry in locked_plan.entries:
            if entry.status == "missing_insert":
                assert entry.resolved_transaction_id is not None
                if entry.resolved_transaction_id in economically_mutated_targets:
                    continue
                _insert_manual_transaction(session, entry)
                notes = "Owner-approved cash-flow source reconciliation"
                transaction_mutations.extend(
                    (
                        _TransactionMutationReceipt(
                            entry.resolved_transaction_id,
                            "transaction_insert",
                            None,
                            _manual_payload_sha256(entry.event, entry.account_id),
                        ),
                        _TransactionMutationReceipt(
                            entry.resolved_transaction_id,
                            "override_insert",
                            None,
                            _override_payload_sha256(cast(str, entry.event.classification), notes),
                        ),
                    )
                )
                economically_mutated_targets.add(entry.resolved_transaction_id)
                applied_mutation_count += 2
            elif entry.status == "override_required":
                assert entry.resolved_transaction_id is not None
                if entry.event.classification is None:
                    raise ReconciliationConflictError("override lacks classification")
                if entry.resolved_transaction_id in economically_mutated_targets:
                    continue
                override = session.get(TransactionOverride, entry.resolved_transaction_id)
                notes = "Owner-approved cash-flow source reconciliation"
                if override is None:
                    session.add(
                        TransactionOverride(
                            plaid_investment_transaction_id=entry.resolved_transaction_id,
                            classification=entry.event.classification,
                            notes=notes,
                        )
                    )
                    mutation_kind: Literal["override_insert", "override_update"] = "override_insert"
                    before_digest = None
                else:
                    before_digest = _override_payload_sha256(
                        override.classification,
                        override.notes or "",
                    )
                    override.classification = entry.event.classification
                    override.notes = notes
                    mutation_kind = "override_update"
                transaction_mutations.append(
                    _TransactionMutationReceipt(
                        entry.resolved_transaction_id,
                        mutation_kind,
                        before_digest,
                        _override_payload_sha256(cast(str, entry.event.classification), notes),
                    )
                )
                economically_mutated_targets.add(entry.resolved_transaction_id)
                applied_mutation_count += 1
        _validate_attestation_chains(
            session,
            {manifest.account_id for manifest in locked_plan.manifests},
        )
        for manifest, attestation_plan in zip(
            locked_plan.manifests, locked_plan.attestations, strict=True
        ):
            _insert_or_approve_attestation(session, manifest, attestation_plan, approved_at)
            applied_mutation_count += attestation_plan.planned_mutation_count
        session.flush()
        provenance_count, decision_memberships, provenance_transaction_mutations = (
            _persist_source_events_and_decisions(
                session,
                locked_plan,
                approved_at,
            )
        )
        provider_resolution_count, provider_resolution_memberships = (
            _persist_provider_unresolved_resolutions(
                session,
                locked_plan,
                approved_at,
            )
        )
        provenance_count += provider_resolution_count
        decision_memberships = (
            *decision_memberships,
            *provider_resolution_memberships,
        )
        applied_mutation_count += provenance_count
        transaction_mutations.extend(provenance_transaction_mutations)
        if applied_mutation_count == 0:
            decision_memberships = ()
            transaction_mutations = []
        _validate_attestation_chains(
            session,
            {manifest.account_id for manifest in locked_plan.manifests},
        )
        session.flush()
        receipt_count, run_id = _persist_run_receipt(
            session,
            locked_plan,
            approved_at=approved_at,
            software_revision=software_revision,
            backup_reference=backup_reference,
            preview_reference=preview_reference,
            applied_mutation_count=applied_mutation_count,
        )
        applied_mutation_count += receipt_count
        session.flush()
        if run_id is not None:
            _persist_run_memberships(
                session,
                run_id,
                decision_memberships,
                transaction_mutations,
            )
            session.flush()
        if applied_mutation_count != locked_plan.planned_mutation_count:
            raise ReconciliationConflictError("applied mutation count changed")
        _verify_applied_state(session, locked_plan)
        _verify_run_receipt(
            session,
            locked_plan,
            run_id,
            decision_memberships,
            transaction_mutations,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return ReconciliationResult(
        committed=True,
        manifest_count=len(locked_plan.manifests),
        source_event_count=(
            len(locked_plan.entries) + len(locked_plan.provider_unresolved_resolutions)
        ),
        status_counts=dict(locked_plan.status_counts),
        applied_mutation_count=applied_mutation_count,
        plan_digest=locked_plan.plan_digest,
    )


def write_private_preview_artifact(plan: ReconciliationPlan, destination: Path) -> None:
    """Atomically write a mode-0600 private audit preview.

    Unlike stdout, this owner-requested private artifact includes dated amounts
    needed to prove one-to-one source coverage.  Account and provider
    transaction identifiers remain only in this private artifact so an owner
    can audit the exact provider row selected; they are never written to stdout.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    external_in_total = sum(
        (
            entry.event.signed_external_amount
            for entry in plan.entries
            if entry.event.classification == "external_in"
            and entry.event.signed_external_amount is not None
        ),
        Decimal(0),
    )
    external_out_total = sum(
        (
            entry.event.signed_external_amount
            for entry in plan.entries
            if entry.event.classification == "external_out"
            and entry.event.signed_external_amount is not None
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
                        if entry.status == status and entry.event.signed_external_amount is not None
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
        "requested_return_start": (
            plan.requested_return_start.isoformat()
            if plan.requested_return_start is not None
            else None
        ),
        "requested_return_end": (
            plan.requested_return_end.isoformat() if plan.requested_return_end is not None else None
        ),
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
                "source_row_sha256": entry.event.source_row_sha256,
                "activity_date": entry.event.activity_date.isoformat(),
                "process_date": (
                    entry.event.process_date.isoformat()
                    if entry.event.process_date is not None
                    else None
                ),
                "settlement_date": (
                    entry.event.settlement_date.isoformat()
                    if entry.event.settlement_date is not None
                    else None
                ),
                "source_amount": _decimal_text(entry.event.source_amount),
                "signed_external_amount": (
                    _decimal_text(entry.event.signed_external_amount)
                    if entry.event.signed_external_amount is not None
                    else None
                ),
                "classification": entry.event.classification,
                "source_code": entry.event.source_code,
                "disposition": entry.event.disposition,
                "ledger_effective_date": (
                    entry.event.ledger_effective_date.isoformat()
                    if entry.event.ledger_effective_date is not None
                    else None
                ),
                "effective_date_basis": entry.event.effective_date_basis,
                "effective_timezone": entry.event.effective_timezone,
                "confidence": entry.event.confidence,
                "assumption_code": entry.event.assumption_code,
                "decision_authority": entry.event.decision_authority,
                "target_transaction_id": entry.resolved_transaction_id,
                "target_identity_sha256": (
                    entry.event.transaction_identity_sha256
                    or (
                        _sha256_text(entry.resolved_transaction_id)
                        if entry.resolved_transaction_id is not None
                        else None
                    )
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
        "provider_unresolved_resolutions": [
            {
                "evidence_source_event_id": resolution.evidence_source_event_id,
                "provider_source_event_id": resolution.provider_source_event_id,
                "expected_provider_source_row_sha256": (
                    resolution.expected_provider_source_row_sha256
                ),
                "expected_current_decision_key": resolution.expected_current_decision_key,
                "expected_current_decision_payload_sha256": (
                    resolution.expected_current_decision_payload_sha256
                ),
                "desired_decision_key": resolution.desired_decision_values.get("decision_key"),
                "target_transaction_id": resolution.desired_decision_values.get(
                    "target_transaction_id"
                ),
                "status": resolution.status,
                "reason_code": resolution.reason_code,
            }
            for resolution in plan.provider_unresolved_resolutions
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
    parser.add_argument("--software-revision")
    parser.add_argument("--backup-reference")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a sanitized dry-run by default; commit only with explicit approval."""
    parser = _argument_parser()
    args = parser.parse_args(argv)
    if args.commit and any(
        value is None
        for value in (
            args.expected_plan_digest,
            args.approved_at,
            args.software_revision,
            args.backup_reference,
            args.preview_path,
        )
    ):
        parser.error(
            "--commit requires --expected-plan-digest, --approved-at, "
            "--software-revision, --backup-reference, and --preview-path"
        )
    sources = tuple(
        ManifestSource(Path(manifest_path), Path(source_path))
        for manifest_path, source_path in args.source
    )
    try:
        from portfolio_tracker.db import SessionLocal

        with SessionLocal() as session:
            plan = build_reconciliation_plan(session, sources)
            preview_reference: str | None = None
            if args.preview_path is not None:
                write_private_preview_artifact(plan, args.preview_path)
                preview_reference = (
                    f"private:preview:sha256:{_sha256_bytes(args.preview_path.read_bytes())}"
                )
            if args.commit:
                assert preview_reference is not None
                result = apply_reconciliation_plan(
                    session,
                    plan,
                    expected_plan_digest=args.expected_plan_digest,
                    approved_at=args.approved_at,
                    software_revision=args.software_revision,
                    backup_reference=args.backup_reference,
                    preview_reference=preview_reference,
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
    except Exception:
        # Database and filesystem exceptions may contain private paths, SQL
        # parameters, transaction identifiers, or amounts. Fail closed without
        # reflecting exception content to an operator-visible stream.
        print(_canonical_json({"committed": False, "error_code": "reconciliation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
