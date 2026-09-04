"""Preview-bound, owner-approved correction of legacy provider transaction rows.

Ordinary provider sync remains insert-only and fail-closed. This module is the
separate authority for the exceptional case where a count-verified provider
record reuses an existing provider ID but its stored normalized economics are
stale. A correction cannot mutate until an exact private preview and a verified
SQLite backup are both hash-bound to explicit owner approval.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    CashFlowDecisionAuthority,
    CashFlowReconciliationRun,
    InvestmentTransaction,
    ProviderTransactionCorrectionReceipt,
)

if TYPE_CHECKING:
    from portfolio_tracker.services.provider_transaction_provenance import (
        ProviderAccountTransactionCapture,
    )

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PREVIEW_SCHEMA_VERSION = "provider-transaction-correction-preview.v1"
_ECONOMIC_FIELDS = (
    "account_id",
    "security_id",
    "date",
    "type",
    "subtype",
    "amount",
    "quantity",
    "price",
    "fees",
    "currency",
    "name",
)

JsonValue = str | int | None


class ProviderTransactionCorrectionError(RuntimeError):
    """Credential-safe refusal of an unbound or stale correction request."""


@dataclass(frozen=True)
class ProviderTransactionFieldChange:
    field: str
    before: JsonValue
    after: JsonValue


@dataclass(frozen=True)
class ProviderTransactionCorrection:
    provider_record_id: str
    source_provider: Literal["plaid", "snaptrade"]
    source_locator_kind: Literal["provider_record"]
    account_id: int
    changed_fields: tuple[ProviderTransactionFieldChange, ...]
    before_payload: dict[str, JsonValue]
    after_payload: dict[str, JsonValue]
    before_payload_sha256: str
    after_payload_sha256: str


@dataclass(frozen=True)
class ProviderTransactionCorrectionPlan:
    plan_digest: str
    provider: Literal["plaid", "snaptrade"]
    account_id: int
    account_identity_sha256: str
    coverage_start: date
    coverage_end: date
    delivery_record_set_sha256: str
    corrections: tuple[ProviderTransactionCorrection, ...]


@dataclass(frozen=True)
class ProviderTransactionCorrectionApproval:
    expected_plan_digest: str
    approved_at: datetime
    software_revision: str
    backup_path: Path
    backup_sha256: str
    preview_path: Path
    preview_sha256: str


@dataclass(frozen=True)
class ProviderTransactionCorrectionApplyResult:
    run_id: str
    corrected_count: int
    created: bool


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _stored_payload(transaction: InvestmentTransaction) -> dict[str, JsonValue]:
    return {
        "account_id": transaction.account_id,
        "security_id": transaction.security_id,
        "date": transaction.date.isoformat(),
        "type": transaction.type,
        "subtype": transaction.subtype,
        "amount": _decimal_text(Decimal(transaction.amount)),
        "quantity": _decimal_text(Decimal(transaction.quantity)),
        "price": (
            _decimal_text(Decimal(transaction.price)) if transaction.price is not None else None
        ),
        "fees": (
            _decimal_text(Decimal(transaction.fees)) if transaction.fees is not None else None
        ),
        "currency": transaction.currency,
        "name": transaction.name,
    }


def _source_payload(
    capture: ProviderAccountTransactionCapture,
    transaction_index: int,
) -> dict[str, JsonValue]:
    transaction = capture.transactions[transaction_index]
    provider_security_id = transaction.plaid_security_id
    if provider_security_id is None:
        security_id = None
    else:
        security_id = capture.security_ids_by_provider_id.get(provider_security_id)
        if security_id is None:
            raise ProviderTransactionCorrectionError("provider_security_mapping_missing")
    return {
        "account_id": capture.account_id,
        "security_id": security_id,
        "date": transaction.date.isoformat(),
        "type": transaction.type,
        "subtype": transaction.subtype,
        "amount": _decimal_text(transaction.amount),
        "quantity": _decimal_text(transaction.quantity),
        "price": _decimal_text(transaction.price),
        "fees": _decimal_text(transaction.fees),
        "currency": transaction.currency,
        "name": transaction.name,
    }


def _correction_payload(correction: ProviderTransactionCorrection) -> dict[str, object]:
    return {
        "provider_record_id": correction.provider_record_id,
        "source_provider": correction.source_provider,
        "source_locator_kind": correction.source_locator_kind,
        "account_id": correction.account_id,
        "changed_fields": [
            {"field": change.field, "before": change.before, "after": change.after}
            for change in correction.changed_fields
        ],
        "before_payload": correction.before_payload,
        "after_payload": correction.after_payload,
        "before_payload_sha256": correction.before_payload_sha256,
        "after_payload_sha256": correction.after_payload_sha256,
    }


def provider_transaction_correction_plan_payload(
    plan: ProviderTransactionCorrectionPlan,
) -> dict[str, object]:
    return {
        "schema_version": _PREVIEW_SCHEMA_VERSION,
        "plan_digest": plan.plan_digest,
        "provider": plan.provider,
        "account_id": plan.account_id,
        "account_identity_sha256": plan.account_identity_sha256,
        "coverage_start": plan.coverage_start.isoformat(),
        "coverage_end": plan.coverage_end.isoformat(),
        "delivery_record_set_sha256": plan.delivery_record_set_sha256,
        "decision_authority_required": CashFlowDecisionAuthority.OWNER_APPROVED.value,
        "corrections": [_correction_payload(item) for item in plan.corrections],
    }


def preview_provider_transaction_corrections(
    session: Session,
    capture: ProviderAccountTransactionCapture,
) -> ProviderTransactionCorrectionPlan:
    """Compare all normalized economic fields without mutating the session."""
    if (
        capture.coverage_start != capture.delivery.requested_start_date
        or capture.coverage_end != capture.delivery.requested_end_date
        or not capture.delivery.is_complete
    ):
        raise ProviderTransactionCorrectionError("provider_delivery_binding_invalid")
    account_identity_sha256 = _digest(
        {
            "provider": capture.delivery.provider,
            "provider_account_id": capture.provider_account_id,
        }
    )
    corrections: list[ProviderTransactionCorrection] = []
    for index, source in enumerate(capture.transactions):
        stored = session.get(InvestmentTransaction, source.plaid_investment_transaction_id)
        if stored is None:
            raise ProviderTransactionCorrectionError("stored_provider_transaction_missing")
        before = _stored_payload(stored)
        after = _source_payload(capture, index)
        changes = tuple(
            ProviderTransactionFieldChange(field, before[field], after[field])
            for field in _ECONOMIC_FIELDS
            if before[field] != after[field]
        )
        if not changes:
            continue
        corrections.append(
            ProviderTransactionCorrection(
                provider_record_id=source.plaid_investment_transaction_id,
                source_provider=capture.delivery.provider,
                source_locator_kind="provider_record",
                account_id=capture.account_id,
                changed_fields=changes,
                before_payload=before,
                after_payload=after,
                before_payload_sha256=_digest(before),
                after_payload_sha256=_digest(after),
            )
        )
    ordered = tuple(sorted(corrections, key=lambda item: item.provider_record_id))
    digest_payload = {
        "schema_version": _PREVIEW_SCHEMA_VERSION,
        "provider": capture.delivery.provider,
        "account_id": capture.account_id,
        "account_identity_sha256": account_identity_sha256,
        "coverage_start": capture.coverage_start.isoformat(),
        "coverage_end": capture.coverage_end.isoformat(),
        "delivery_record_set_sha256": capture.delivery.record_set_sha256,
        "corrections": [_correction_payload(item) for item in ordered],
    }
    return ProviderTransactionCorrectionPlan(
        plan_digest=_digest(digest_payload),
        provider=capture.delivery.provider,
        account_id=capture.account_id,
        account_identity_sha256=account_identity_sha256,
        coverage_start=capture.coverage_start,
        coverage_end=capture.coverage_end,
        delivery_record_set_sha256=capture.delivery.record_set_sha256,
        corrections=ordered,
    )


def write_provider_transaction_correction_preview(
    plan: ProviderTransactionCorrectionPlan,
    destination: Path,
) -> str:
    """Create one private, immutable preview and return its byte digest."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            provider_transaction_correction_plan_payload(plan),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    try:
        with destination.open("xb") as output:
            output.write(encoded)
    except FileExistsError as exc:
        raise ProviderTransactionCorrectionError("correction_preview_already_exists") from exc
    destination.chmod(0o600)
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ProviderTransactionCorrectionError("approval_evidence_unreadable") from exc
    return digest.hexdigest()


def _verify_preview(
    plan: ProviderTransactionCorrectionPlan,
    path: Path,
    expected_sha256: str,
) -> None:
    if _file_sha256(path) != expected_sha256:
        raise ProviderTransactionCorrectionError("correction_preview_digest_mismatch")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderTransactionCorrectionError("correction_preview_invalid") from exc
    if payload != provider_transaction_correction_plan_payload(plan):
        raise ProviderTransactionCorrectionError("correction_preview_plan_mismatch")


def _backup_transaction_payload(
    connection: sqlite3.Connection,
    provider_record_id: str,
) -> dict[str, JsonValue] | None:
    row = connection.execute(
        """
        SELECT account_id, security_id, date, type, subtype, amount, quantity,
               price, fees, currency, name
        FROM investment_transactions
        WHERE plaid_investment_transaction_id = ?
        """,
        (provider_record_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "account_id": int(row[0]),
        "security_id": int(row[1]) if row[1] is not None else None,
        "date": str(row[2]),
        "type": str(row[3]),
        "subtype": str(row[4]) if row[4] is not None else None,
        "amount": _decimal_text(Decimal(str(row[5]))),
        "quantity": _decimal_text(Decimal(str(row[6]))),
        "price": _decimal_text(Decimal(str(row[7]))) if row[7] is not None else None,
        "fees": _decimal_text(Decimal(str(row[8]))) if row[8] is not None else None,
        "currency": str(row[9]),
        "name": str(row[10]) if row[10] is not None else None,
    }


def _verify_backup(
    plan: ProviderTransactionCorrectionPlan,
    path: Path,
    expected_sha256: str,
) -> None:
    if _file_sha256(path) != expected_sha256:
        raise ProviderTransactionCorrectionError("backup_digest_mismatch")
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ProviderTransactionCorrectionError("backup_integrity_check_failed")
            for correction in plan.corrections:
                payload = _backup_transaction_payload(connection, correction.provider_record_id)
                if payload is None or _digest(payload) != correction.before_payload_sha256:
                    raise ProviderTransactionCorrectionError(
                        "backup_does_not_match_previewed_state"
                    )
    except ProviderTransactionCorrectionError:
        raise
    except sqlite3.Error as exc:
        raise ProviderTransactionCorrectionError("backup_not_valid_sqlite") from exc


def _validate_approval(
    plan: ProviderTransactionCorrectionPlan,
    approval: ProviderTransactionCorrectionApproval,
) -> datetime:
    if approval.expected_plan_digest != plan.plan_digest:
        raise ProviderTransactionCorrectionError("approved_plan_digest_mismatch")
    for digest in (
        approval.expected_plan_digest,
        approval.backup_sha256,
        approval.preview_sha256,
    ):
        if not _SHA256_RE.fullmatch(digest):
            raise ProviderTransactionCorrectionError("approval_digest_invalid")
    if not approval.software_revision or len(approval.software_revision) > 64:
        raise ProviderTransactionCorrectionError("software_revision_invalid")
    approved_at = approval.approved_at
    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise ProviderTransactionCorrectionError("approval_timestamp_timezone_missing")
    normalized = approved_at.astimezone(UTC)
    if (
        len(str(approval.backup_path.resolve())) > 512
        or len(str(approval.preview_path.resolve())) > 512
    ):
        raise ProviderTransactionCorrectionError("approval_reference_too_long")
    _verify_preview(plan, approval.preview_path, approval.preview_sha256)
    _verify_backup(plan, approval.backup_path, approval.backup_sha256)
    return normalized


def _verify_existing_apply(
    session: Session,
    plan: ProviderTransactionCorrectionPlan,
    run: CashFlowReconciliationRun,
) -> ProviderTransactionCorrectionApplyResult:
    receipts = tuple(
        session.scalars(
            select(ProviderTransactionCorrectionReceipt).where(
                ProviderTransactionCorrectionReceipt.run_id == run.run_id
            )
        )
    )
    if len(receipts) != len(plan.corrections):
        raise ProviderTransactionCorrectionError("correction_run_receipt_mismatch")
    by_record = {item.provider_record_id: item for item in plan.corrections}
    for receipt in receipts:
        correction = by_record.get(receipt.provider_record_id)
        stored = session.get(InvestmentTransaction, receipt.provider_record_id)
        if (
            correction is None
            or stored is None
            or _digest(_stored_payload(stored)) != correction.after_payload_sha256
            or receipt.before_payload_sha256 != correction.before_payload_sha256
            or receipt.after_payload_sha256 != correction.after_payload_sha256
        ):
            raise ProviderTransactionCorrectionError("correction_applied_state_mismatch")
    return ProviderTransactionCorrectionApplyResult(run.run_id, len(receipts), False)


def apply_provider_transaction_corrections(
    session: Session,
    capture: ProviderAccountTransactionCapture,
    plan: ProviderTransactionCorrectionPlan,
    approval: ProviderTransactionCorrectionApproval,
) -> ProviderTransactionCorrectionApplyResult:
    """Apply the exact preview atomically without committing the caller session."""
    if not plan.corrections:
        raise ProviderTransactionCorrectionError("correction_plan_has_no_mutations")
    approved_at = _validate_approval(plan, approval)
    applied_at = datetime.now(UTC)
    if approved_at > applied_at:
        raise ProviderTransactionCorrectionError("approval_timestamp_is_in_the_future")
    existing_run = session.scalar(
        select(CashFlowReconciliationRun).where(
            CashFlowReconciliationRun.plan_digest == plan.plan_digest
        )
    )
    if existing_run is not None:
        return _verify_existing_apply(session, plan, existing_run)

    current = preview_provider_transaction_corrections(session, capture)
    if current != plan:
        raise ProviderTransactionCorrectionError("correction_plan_stale")

    run_id = _digest(
        {
            "plan_digest": plan.plan_digest,
            "software_revision": approval.software_revision,
            "backup_sha256": approval.backup_sha256,
            "preview_sha256": approval.preview_sha256,
            "approved_at": approved_at.isoformat(),
        }
    )
    session.add(
        CashFlowReconciliationRun(
            run_id=run_id,
            plan_digest=plan.plan_digest,
            manifest_set_sha256=plan.delivery_record_set_sha256,
            software_revision=approval.software_revision,
            backup_reference=str(approval.backup_path.resolve()),
            preview_reference=str(approval.preview_path.resolve()),
            affected_start=min(
                date.fromisoformat(str(item.after_payload["date"])) for item in plan.corrections
            ),
            affected_end=max(
                date.fromisoformat(str(item.after_payload["date"])) for item in plan.corrections
            ),
            requested_return_start=plan.coverage_start,
            requested_return_end=plan.coverage_end,
            affected_account_count=len({item.account_id for item in plan.corrections}),
            source_event_count=0,
            planned_mutation_count=len(plan.corrections),
            applied_mutation_count=len(plan.corrections),
            status="applied",
            approved_at=approved_at,
            applied_at=applied_at,
        )
    )
    session.flush()

    source_by_id = {item.plaid_investment_transaction_id: item for item in capture.transactions}
    for correction in plan.corrections:
        stored = session.get(InvestmentTransaction, correction.provider_record_id)
        source = source_by_id.get(correction.provider_record_id)
        if stored is None or source is None:
            raise ProviderTransactionCorrectionError("correction_target_missing")
        provider_security_id = source.plaid_security_id
        stored.account_id = capture.account_id
        stored.security_id = (
            capture.security_ids_by_provider_id[provider_security_id]
            if provider_security_id is not None
            else None
        )
        stored.date = source.date
        stored.type = source.type
        stored.subtype = source.subtype
        stored.amount = source.amount
        stored.quantity = source.quantity
        stored.price = source.price
        stored.fees = source.fees
        stored.currency = source.currency
        stored.name = source.name
        session.add(
            ProviderTransactionCorrectionReceipt(
                run_id=run_id,
                provider_record_id=correction.provider_record_id,
                account_id=correction.account_id,
                source_provider=correction.source_provider,
                source_locator_kind=correction.source_locator_kind,
                changed_fields_json=json.dumps(
                    [change.field for change in correction.changed_fields],
                    separators=(",", ":"),
                ),
                before_payload_json=json.dumps(
                    correction.before_payload, sort_keys=True, separators=(",", ":")
                ),
                after_payload_json=json.dumps(
                    correction.after_payload, sort_keys=True, separators=(",", ":")
                ),
                before_payload_sha256=correction.before_payload_sha256,
                after_payload_sha256=correction.after_payload_sha256,
                delivery_record_set_sha256=plan.delivery_record_set_sha256,
                decision_authority=CashFlowDecisionAuthority.OWNER_APPROVED.value,
                backup_sha256=approval.backup_sha256,
                preview_sha256=approval.preview_sha256,
                approved_at=approved_at,
            )
        )
    session.flush()
    return ProviderTransactionCorrectionApplyResult(run_id, len(plan.corrections), True)
