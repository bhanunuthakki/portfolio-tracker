"""Privacy-safe, read-only reconciliation audit for a Portfolio Tracker database.

The private JSON output contains evidence dates, amounts, opaque database-row
tokens, and reconciliation statuses.  Standard output is deliberately limited
to counts and SHA-256 digests so an operator can retain ordinary task logs
without publishing account or transaction details.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

# Running this file directly from ``scripts/`` does not otherwise place ``src``
# on sys.path.  The path contains no live configuration and imports no settings.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from portfolio_tracker.models import (  # noqa: E402
    Account,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
)
from portfolio_tracker.services.active_items import valued_account_ids  # noqa: E402
from portfolio_tracker.services.cashflow_source_coverage import (  # noqa: E402
    assess_cashflow_source_coverage,
)
from portfolio_tracker.services.external_flow_ledger import (  # noqa: E402
    classify_transaction_cashflow,
    load_transaction_overrides,
)
from portfolio_tracker.services.performance import (  # noqa: E402
    partial_snapshot_dates,
    unpriceable_snapshot_dates,
)

EXPECTED_ALEMBIC_REVISION = "0025"
PREVIEW_SCHEMA_VERSION = "1"
SHA256_PREFIX = "sha256:"

ResolutionStatus = Literal[
    "existing_exact",
    "override_required",
    "missing_insert",
    "conflict",
]

_STATUS_ORDER: tuple[ResolutionStatus, ...] = (
    "existing_exact",
    "override_required",
    "missing_insert",
    "conflict",
)

# Credentials are intentionally absent.  These are the financial and lineage
# tables whose content fingerprint is useful when comparing a live database to
# a restored backup.  Unknown tables still receive a row count, never a row read.
_CHECKSUM_COLUMNS: dict[str, tuple[str, ...]] = {
    "items": (
        "item_id",
        "source",
        "plaid_institution_id",
        "institution_name",
        "linked_at",
        "last_refreshed_at",
        "is_data_active",
    ),
    "accounts": (
        "account_id",
        "item_id",
        "plaid_account_id",
        "name",
        "official_name",
        "type",
        "subtype",
        "mask",
        "currency",
    ),
    "securities": (
        "security_id",
        "plaid_security_id",
        "ticker",
        "cusip",
        "isin",
        "name",
        "type",
        "currency",
        "is_cash_equivalent",
    ),
    "holdings_snapshots": (
        "snapshot_date",
        "account_id",
        "security_id",
        "quantity",
        "institution_price",
        "institution_value",
        "cost_basis",
        "currency",
        "origin",
    ),
    "investment_transactions": (
        "plaid_investment_transaction_id",
        "account_id",
        "security_id",
        "date",
        "name",
        "quantity",
        "amount",
        "price",
        "fees",
        "type",
        "subtype",
        "currency",
        "origin",
    ),
    "transaction_overrides": (
        "plaid_investment_transaction_id",
        "classification",
        "notes",
        "updated_at",
    ),
    "cashflow_source_attestations": (
        "attestation_id",
        "attestation_key",
        "account_id",
        "coverage_start",
        "coverage_end",
        "source_type",
        "source_reference",
        "source_sha256",
        "captured_at",
        "approved_at",
        "methodology_version",
        "created_at",
        "superseded_at",
        "superseded_by_attestation_id",
    ),
    "cashflow_source_gaps": (
        "gap_id",
        "attestation_id",
        "gap_start",
        "gap_end",
        "reason_code",
    ),
    "prices": ("security_id", "date", "close", "source", "adjustment_basis"),
    "benchmarks": ("symbol", "date", "close", "total_return_close"),
    "portfolio_values_daily": ("date", "total_value", "total_cost_basis", "source"),
}


class AuditError(RuntimeError):
    """Expected failure whose stable code is safe for ordinary logs."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EvidenceEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    source_row_ordinal: int | None = Field(default=None, gt=0)
    date: date
    signed_external_amount: Decimal
    classification: Literal["external_in", "external_out", "internal"]

    @model_validator(mode="after")
    def validate_direction(self) -> EvidenceEvent:
        amount = self.signed_external_amount
        if not amount.is_finite():
            raise ValueError("amount must be finite")
        if self.classification == "external_in" and amount <= 0:
            raise ValueError("external_in requires a positive signed amount")
        if self.classification == "external_out" and amount >= 0:
            raise ValueError("external_out requires a negative signed amount")
        if self.classification == "internal" and amount != 0:
            raise ValueError("internal requires a zero signed amount")
        return self


class EvidenceGap(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    start_date: date
    end_date: date
    reason_code: str

    @model_validator(mode="after")
    def validate_range(self) -> EvidenceGap:
        if self.start_date > self.end_date:
            raise ValueError("gap dates are reversed")
        return self


class EvidenceManifest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    account_id: int
    account_identity_sha256: str | None = None
    coverage_start: date
    coverage_end: date
    source_type: str | None = None
    source_document_sha256: str
    events: list[EvidenceEvent] = Field(min_length=1)
    gaps: list[EvidenceGap] = Field(default_factory=list[EvidenceGap])

    @field_validator("account_id")
    @classmethod
    def validate_account_id(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("account_id must be a positive integer")
        return value

    @field_validator("source_document_sha256", "account_identity_sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("expected lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_manifest(self) -> EvidenceManifest:
        if self.coverage_start > self.coverage_end:
            raise ValueError("coverage dates are reversed")
        ordinals = [event.source_row_ordinal for event in self.events if event.source_row_ordinal]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("source row ordinals must be unique")
        if any(not self.coverage_start <= event.date <= self.coverage_end for event in self.events):
            raise ValueError("event falls outside manifest coverage")
        if any(
            gap.start_date < self.coverage_start or gap.end_date > self.coverage_end
            for gap in self.gaps
        ):
            raise ValueError("gap falls outside manifest coverage")
        return self


class TargetEventSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    date: date
    signed_external_amount: Decimal
    classification: Literal["external_in", "external_out", "internal"]

    @model_validator(mode="after")
    def validate_direction(self) -> TargetEventSpec:
        event = EvidenceEvent(
            date=self.date,
            signed_external_amount=self.signed_external_amount,
            classification=self.classification,
        )
        if event.signed_external_amount != self.signed_external_amount:
            raise ValueError("target amount is invalid")
        return self


class NamedTransferUniverseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_search_terms: list[str] = Field(min_length=1)
    destination_search_terms: list[str] = Field(min_length=1)
    authoritative_amount: Decimal
    date_window_start: date
    date_window_end: date

    @field_validator("source_search_terms", "destination_search_terms")
    @classmethod
    def normalize_search_terms(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().casefold() for value in values]
        if any(len(value) < 2 or len(value) > 200 for value in normalized):
            raise ValueError("search terms must contain 2-200 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("search terms must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_review(self) -> NamedTransferUniverseSpec:
        if not self.authoritative_amount.is_finite() or self.authoritative_amount <= 0:
            raise ValueError("authoritative amount must be finite and positive")
        if self.date_window_start > self.date_window_end:
            raise ValueError("named-transfer date window is reversed")
        return self


class ReviewSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_events: list[TargetEventSpec] = Field(default_factory=list[TargetEventSpec])
    named_transfer_universe: NamedTransferUniverseSpec | None = None

    @model_validator(mode="after")
    def validate_review(self) -> ReviewSpec:
        if not self.target_events and self.named_transfer_universe is None:
            raise ValueError("review spec must request at least one review")
        labels = [event.label for event in self.target_events]
        if len(labels) != len(set(labels)):
            raise ValueError("target labels must be unique")
        return self


def _digest(payload: bytes | str) -> str:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    return f"{SHA256_PREFIX}{hashlib.sha256(raw).hexdigest()}"


def _canonical_json(value: object, *, indent: int | None = None) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), indent=indent)
        + ("\n" if indent is not None else "")
    ).encode("utf-8")


def _readonly_uri(path: Path) -> str:
    absolute = path.resolve()
    if not absolute.is_file():
        raise AuditError("database_unavailable")
    return f"file:{quote(absolute.as_posix(), safe='/:')}?mode=ro"


def _readonly_engine(path: Path) -> Engine:
    uri = _readonly_uri(path)

    def connect_readonly() -> sqlite3.Connection:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection

    return create_engine("sqlite://", creator=connect_readonly, poolclass=NullPool, future=True)


def _load_manifest(path: Path) -> tuple[EvidenceManifest, str]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise AuditError("manifest_unavailable") from None
    try:
        model = EvidenceManifest.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise AuditError("manifest_invalid") from None
    return model, _digest(raw)


def _load_review_spec(path: Path) -> tuple[ReviewSpec, str]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise AuditError("review_spec_unavailable") from None
    try:
        model = ReviewSpec.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise AuditError("review_spec_invalid") from None
    return model, _digest(raw)


def _verify_database(session: Session) -> tuple[str, bool]:
    try:
        query_only = int(session.execute(text("PRAGMA query_only")).scalar_one()) == 1
        integrity_rows = list(session.execute(text("PRAGMA integrity_check")).scalars())
        revisions = list(session.execute(text("SELECT version_num FROM alembic_version")).scalars())
    except Exception:
        raise AuditError("database_schema_unavailable") from None
    if not query_only:
        raise AuditError("query_only_not_enforced")
    if integrity_rows != ["ok"]:
        raise AuditError("database_integrity_failed")
    if revisions != [EXPECTED_ALEMBIC_REVISION]:
        raise AuditError("alembic_revision_mismatch")
    return revisions[0], query_only


def _table_inventory(session: Session) -> tuple[dict[str, int], dict[str, str], str]:
    connection = session.connection()
    table_names = sorted(
        str(name)
        for name in connection.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ).scalars()
    )
    counts: dict[str, int] = {}
    checksums: dict[str, str] = {}
    for table in table_names:
        quoted_table = table.replace('"', '""')
        counts[table] = int(
            connection.execute(text(f'SELECT COUNT(*) FROM "{quoted_table}"')).scalar_one()
        )
        columns = _CHECKSUM_COLUMNS.get(table)
        if columns is None:
            continue
        existing = {
            str(row[1])
            for row in connection.execute(text(f'PRAGMA table_info("{quoted_table}")')).all()
        }
        if any(column not in existing for column in columns):
            raise AuditError("database_schema_unavailable")
        quoted_columns = [f'"{column.replace(chr(34), chr(34) * 2)}"' for column in columns]
        statement = f'SELECT {", ".join(quoted_columns)} FROM "{quoted_table}"'
        encoded_rows = sorted(
            _canonical_json([_json_scalar(value) for value in row])
            for row in connection.execute(text(statement)).all()
        )
        hasher = hashlib.sha256()
        for row in encoded_rows:
            hasher.update(len(row).to_bytes(8, "big"))
            hasher.update(row)
        checksums[table] = f"{SHA256_PREFIX}{hasher.hexdigest()}"
    fingerprint = _digest(_canonical_json({"counts": counts, "checksums": checksums}))
    return counts, checksums, fingerprint


def _json_scalar(value: object) -> object:
    if isinstance(value, bytes):
        return _digest(value)
    if isinstance(value, float):
        return format(Decimal(str(value)), "f")
    return value


def _latest_complete_snapshot(session: Session, valued: frozenset[int]) -> date:
    if not valued:
        raise AuditError("no_valued_accounts")
    candidates = set(
        session.execute(
            select(HoldingSnapshot.snapshot_date)
            .where(HoldingSnapshot.account_id.in_(valued))
            .distinct()
        )
        .scalars()
        .all()
    )
    complete = (
        candidates
        - set(partial_snapshot_dates(session, candidates))
        - unpriceable_snapshot_dates(session, candidates)
    )
    if not complete:
        raise AuditError("no_complete_observed_snapshot")
    return max(complete)


def _two_year_start(end_date: date) -> date:
    try:
        return end_date.replace(year=end_date.year - 2)
    except ValueError:
        return end_date.replace(year=end_date.year - 2, day=28)


def _opaque_token(key: bytes, namespace: str, raw: object) -> str:
    digest = hmac.new(key, f"{namespace}\x1f{raw}".encode(), hashlib.sha256).hexdigest()
    return f"{namespace}_{digest[:24]}"


def _account_tokenizer(key: bytes) -> Callable[[int], str]:
    return lambda account_id: _opaque_token(key, "acct", account_id)


def _verify_manifest_accounts(session: Session, manifests: Sequence[EvidenceManifest]) -> None:
    for manifest in manifests:
        account = session.get(Account, manifest.account_id)
        if account is None:
            raise AuditError("manifest_account_unresolved")
        if manifest.account_identity_sha256 is not None:
            actual = hashlib.sha256(account.plaid_account_id.encode()).hexdigest()
            if not hmac.compare_digest(actual, manifest.account_identity_sha256):
                raise AuditError("manifest_account_identity_mismatch")


def _candidate_summary(
    key: bytes,
    transaction: InvestmentTransaction,
    provider: str,
    account_token: Callable[[int], str],
    classification: str | None,
) -> dict[str, object]:
    return {
        "transaction_token": _opaque_token(key, "txn", transaction.plaid_investment_transaction_id),
        "account_token": account_token(transaction.account_id),
        "source_provider": provider,
        "effective_classification": classification,
    }


def _resolve_events(
    session: Session,
    manifests: Sequence[tuple[EvidenceManifest, str]],
    key: bytes,
    account_token: Callable[[int], str],
    review_spec: ReviewSpec | None,
) -> dict[str, object]:
    account_ids = {manifest.account_id for manifest, _digest_value in manifests}
    event_dates = [event.date for manifest, _ in manifests for event in manifest.events]
    rows = session.execute(
        select(InvestmentTransaction, Item.source)
        .join(Account, Account.account_id == InvestmentTransaction.account_id)
        .join(Item, Item.item_id == Account.item_id)
        .where(InvestmentTransaction.account_id.in_(account_ids))
        .where(InvestmentTransaction.date >= min(event_dates))
        .where(InvestmentTransaction.date <= max(event_dates))
    ).all()
    overrides = load_transaction_overrides(session)
    prepared: list[tuple[InvestmentTransaction, str, str | None, Decimal | None]] = []
    for transaction, provider in rows:
        decision = classify_transaction_cashflow(
            transaction.type,
            transaction.subtype,
            Decimal(transaction.amount or 0),
            override=overrides.get(transaction.plaid_investment_transaction_id),
            name=transaction.name,
        )
        prepared.append(
            (
                transaction,
                provider,
                decision.classification if decision is not None else None,
                decision.signed_external_amount if decision is not None else None,
            )
        )

    results: list[dict[str, object]] = []
    status_counts: Counter[str] = Counter()
    target_specs = review_spec.target_events if review_spec is not None else []
    targeted: dict[str, list[dict[str, object]]] = {target.label: [] for target in target_specs}
    for manifest, manifest_digest in manifests:
        for index, event in enumerate(manifest.events, start=1):
            exact = [
                (transaction, provider, classification)
                for transaction, provider, classification, signed in prepared
                if transaction.account_id == manifest.account_id
                and transaction.date == event.date
                and classification == event.classification
                and signed == event.signed_external_amount
            ]
            raw = [
                (transaction, provider, classification)
                for transaction, provider, classification, signed in prepared
                if transaction.account_id == manifest.account_id
                and transaction.date == event.date
                and signed is not None
                and abs(signed) == abs(event.signed_external_amount)
            ]
            if len(exact) == 1:
                status: ResolutionStatus = "existing_exact"
                reason_code = "existing_transaction_exact"
                selected = exact
            elif len(exact) > 1:
                status = "conflict"
                reason_code = "multiple_exact_candidates"
                selected = exact
            elif len(raw) == 1:
                status = "override_required"
                reason_code = "explicit_classification_override_required"
                selected = raw
            elif len(raw) > 1:
                status = "conflict"
                reason_code = "multiple_amount_candidates"
                selected = raw
            else:
                status = "missing_insert"
                reason_code = "manual_transaction_absent"
                selected = []
            status_counts[status] += 1
            ordinal = event.source_row_ordinal or index
            evidence_id = _digest(
                "\x1f".join(
                    (
                        manifest_digest,
                        str(ordinal),
                        event.date.isoformat(),
                        format(event.signed_external_amount, "f"),
                        event.classification,
                    )
                )
            )
            result: dict[str, object] = {
                "evidence_id": evidence_id,
                "manifest_digest": manifest_digest,
                "source_row_ordinal": ordinal,
                "account_token": account_token(manifest.account_id),
                "date": event.date.isoformat(),
                "signed_external_amount": format(event.signed_external_amount, "f"),
                "classification": event.classification,
                "status": status,
                "reason_code": reason_code,
                "candidate_count": len(selected),
                "candidates": [
                    _candidate_summary(key, transaction, provider, account_token, classification)
                    for transaction, provider, classification in selected
                ],
            }
            results.append(result)
            for target in target_specs:
                if (
                    event.date,
                    event.signed_external_amount,
                    event.classification,
                ) == (
                    target.date,
                    target.signed_external_amount,
                    target.classification,
                ):
                    targeted[target.label].append(
                        {
                            "evidence_id": evidence_id,
                            "account_token": account_token(manifest.account_id),
                            "status": status,
                            "candidate_count": len(selected),
                        }
                    )
    return {
        "event_count": len(results),
        "status_counts": {status: status_counts[status] for status in _STATUS_ORDER},
        "targeted_events": targeted,
        "events": results,
    }


def _account_snapshot_counts(session: Session) -> dict[int, int]:
    return {
        int(account_id): int(count)
        for account_id, count in session.execute(
            select(HoldingSnapshot.account_id, func.count()).group_by(HoldingSnapshot.account_id)
        ).all()
    }


def _named_universe_review(
    session: Session,
    valued: frozenset[int],
    key: bytes,
    account_token: Callable[[int], str],
    spec: NamedTransferUniverseSpec | None,
) -> dict[str, object]:
    if spec is None:
        return {
            "status": "not_requested",
            "source_state": "not_requested",
            "destination_state": "not_requested",
            "classification": "not_requested",
            "source_accounts": [],
            "destination_accounts": [],
            "authoritative_amount_candidate_count": 0,
            "authoritative_amount_candidates": [],
        }
    snapshot_counts = _account_snapshot_counts(session)
    source_accounts: list[dict[str, object]] = []
    destination_accounts: list[dict[str, object]] = []
    for account, item in session.execute(select(Account, Item).join(Item)).all():
        searchable = " ".join(
            value.casefold()
            for value in (account.name, account.official_name, item.institution_name)
            if value
        )
        row: dict[str, object] = {
            "account_token": account_token(account.account_id),
            "active": bool(item.is_data_active),
            "valued": account.account_id in valued,
            "snapshot_count": snapshot_counts.get(account.account_id, 0),
        }
        if any(term in searchable for term in spec.source_search_terms):
            source_accounts.append(row)
        if any(term in searchable for term in spec.destination_search_terms):
            destination_accounts.append(row)

    source_state = _universe_state(source_accounts)
    destination_state = _universe_state(destination_accounts)
    classifications = {
        ("inside", "inside"): "source_inside_destination_inside",
        ("outside", "inside"): "source_outside_destination_inside",
        ("inside", "outside"): "source_inside_destination_outside",
        ("outside", "outside"): "source_outside_destination_outside",
    }
    classification = classifications.get(
        (source_state, destination_state), "unresolved_or_ambiguous"
    )

    amount_candidates: list[dict[str, object]] = []
    for transaction in session.execute(
        select(InvestmentTransaction)
        .where(InvestmentTransaction.date >= spec.date_window_start)
        .where(InvestmentTransaction.date <= spec.date_window_end)
    ).scalars():
        if abs(Decimal(transaction.amount or 0)) != spec.authoritative_amount:
            continue
        amount_candidates.append(
            {
                "transaction_token": _opaque_token(
                    key, "txn", transaction.plaid_investment_transaction_id
                ),
                "account_token": account_token(transaction.account_id),
                "date": transaction.date.isoformat(),
            }
        )
    return {
        "status": "reviewed",
        "source_state": source_state,
        "destination_state": destination_state,
        "classification": classification,
        "source_accounts": source_accounts,
        "destination_accounts": destination_accounts,
        "authoritative_amount": format(spec.authoritative_amount, "f"),
        "date_window_start": spec.date_window_start.isoformat(),
        "date_window_end": spec.date_window_end.isoformat(),
        "authoritative_amount_candidate_count": len(amount_candidates),
        "authoritative_amount_candidates": amount_candidates,
    }


def _universe_state(accounts: Sequence[dict[str, object]]) -> str:
    if not accounts:
        return "not_found"
    states = {bool(account["valued"]) for account in accounts}
    if states == {True}:
        return "inside"
    if states == {False}:
        return "outside"
    return "ambiguous"


def _coverage_preview(
    session: Session,
    valued: frozenset[int],
    start_date: date,
    end_date: date,
    account_token: Callable[[int], str],
) -> dict[str, object]:
    assessment = assess_cashflow_source_coverage(session, start_date, end_date, account_ids=valued)
    status_counts = Counter(account.status for account in assessment.accounts)
    return {
        "status": assessment.status,
        "is_complete": assessment.is_complete,
        "requested_start_date": assessment.requested_start_date.isoformat(),
        "requested_end_date": assessment.requested_end_date.isoformat(),
        "required_start_date": (
            assessment.required_start_date.isoformat()
            if assessment.required_start_date is not None
            else None
        ),
        "required_end_date": (
            assessment.required_end_date.isoformat()
            if assessment.required_end_date is not None
            else None
        ),
        "account_status_counts": {
            status: status_counts[status] for status in ("complete", "partial", "missing")
        },
        "accounts": [
            {
                "account_token": account_token(account.account_id),
                "status": account.status,
                "covered_ranges": [
                    {"start_date": start.isoformat(), "end_date": end.isoformat()}
                    for start, end in account.covered_ranges
                ],
                "uncovered_ranges": [
                    {"start_date": start.isoformat(), "end_date": end.isoformat()}
                    for start, end in account.uncovered_ranges
                ],
                "attestation_count": len(account.attestation_keys),
            }
            for account in assessment.accounts
        ],
        "attestation_count": len(assessment.attestations),
    }


def audit_database(
    database: Path,
    manifest_paths: Sequence[Path],
    review_spec_path: Path | None = None,
) -> dict[str, object]:
    """Return a detailed private preview without mutating the database."""
    if not manifest_paths:
        raise AuditError("manifest_required")
    manifests = [_load_manifest(path) for path in manifest_paths]
    review_spec_entry = (
        _load_review_spec(review_spec_path) if review_spec_path is not None else None
    )
    review_spec = review_spec_entry[0] if review_spec_entry is not None else None
    review_spec_digest = review_spec_entry[1] if review_spec_entry is not None else None
    engine = _readonly_engine(database)
    try:
        with Session(engine) as session:
            revision, query_only = _verify_database(session)
            table_counts, table_checksums, database_fingerprint = _table_inventory(session)
            models = [manifest for manifest, _manifest_digest in manifests]
            _verify_manifest_accounts(session, models)
            valued = valued_account_ids(session)
            latest = _latest_complete_snapshot(session, valued)
            start = _two_year_start(latest)
            token_key = hashlib.sha256(
                _canonical_json(
                    {
                        "database_fingerprint": database_fingerprint,
                        "manifest_digests": sorted(digest for _manifest, digest in manifests),
                        "review_spec_digest": review_spec_digest,
                    }
                )
            ).digest()
            account_token = _account_tokenizer(token_key)
            resolution = _resolve_events(session, manifests, token_key, account_token, review_spec)
            universe = _named_universe_review(
                session,
                valued,
                token_key,
                account_token,
                review_spec.named_transfer_universe if review_spec is not None else None,
            )
            coverage = _coverage_preview(session, valued, start, latest, account_token)
            return {
                "schema_version": PREVIEW_SCHEMA_VERSION,
                "review_spec": {
                    "status": "provided" if review_spec is not None else "not_provided",
                    "digest": review_spec_digest,
                    "target_event_count": len(review_spec.target_events) if review_spec else 0,
                    "named_transfer_review_requested": (
                        review_spec is not None and review_spec.named_transfer_universe is not None
                    ),
                },
                "database": {
                    "path_digest": _digest(str(database.resolve())),
                    "query_only": query_only,
                    "integrity": "ok",
                    "alembic_revision": revision,
                    "table_counts": table_counts,
                    "table_checksums": table_checksums,
                    "fingerprint": database_fingerprint,
                },
                "window": {
                    "latest_complete_observed_snapshot_date": latest.isoformat(),
                    "two_year_start_date": start.isoformat(),
                    "elapsed_days": (latest - start).days,
                    "cashflow_boundary": "(start, end]",
                    "source_coverage_required_start_date": (start + timedelta(days=1)).isoformat(),
                },
                "valued_accounts": {
                    "count": len(valued),
                    "account_tokens": [account_token(account_id) for account_id in sorted(valued)],
                },
                "manifests": [
                    {
                        "manifest_digest": manifest_digest,
                        "source_document_digest": f"{SHA256_PREFIX}{manifest.source_document_sha256}",
                        "account_token": account_token(manifest.account_id),
                        "account_identity_attested": manifest.account_identity_sha256 is not None,
                        "coverage_start": manifest.coverage_start.isoformat(),
                        "coverage_end": manifest.coverage_end.isoformat(),
                        "event_count": len(manifest.events),
                        "declared_gap_count": len(manifest.gaps),
                    }
                    for manifest, manifest_digest in manifests
                ],
                "resolution": resolution,
                "named_universe_review": universe,
                "source_coverage": coverage,
            }
    finally:
        engine.dispose()


def _write_private_preview(path: Path, preview: dict[str, object]) -> str:
    payload = _canonical_json(preview, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary_name = handle.name
            os.chmod(temporary_name, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return _digest(payload)


def _summary(preview: dict[str, object], preview_digest: str) -> dict[str, int | str]:
    database = cast("dict[str, object]", preview["database"])
    resolution = cast("dict[str, object]", preview["resolution"])
    status_counts = cast("dict[str, int]", resolution["status_counts"])
    coverage = cast("dict[str, object]", preview["source_coverage"])
    coverage_counts = cast("dict[str, int]", coverage["account_status_counts"])
    universe = cast("dict[str, object]", preview["named_universe_review"])
    valued_accounts = cast("dict[str, object]", preview["valued_accounts"])
    return {
        "manifest_count": len(cast("list[object]", preview["manifests"])),
        "evidence_event_count": int(cast("int", resolution["event_count"])),
        "existing_exact_count": status_counts["existing_exact"],
        "override_required_count": status_counts["override_required"],
        "missing_insert_count": status_counts["missing_insert"],
        "conflict_count": status_counts["conflict"],
        "valued_account_count": cast("int", valued_accounts["count"]),
        "source_coverage_missing_account_count": coverage_counts["missing"],
        "named_account_candidate_count": len(cast("list[object]", universe["source_accounts"]))
        + len(cast("list[object]", universe["destination_accounts"])),
        "table_count": len(cast("dict[str, int]", database["table_counts"])),
        "database_digest": cast("str", database["fingerprint"]),
        "preview_digest": preview_digest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--review-spec", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        preview = audit_database(args.db, args.manifest, args.review_spec)
        preview_digest = _write_private_preview(args.output, preview)
    except AuditError as exc:
        print(
            json.dumps(
                {"error_count": 1, "error_digest": _digest(exc.code)},
                sort_keys=True,
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"error_count": 1, "error_digest": _digest("internal_error")},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(_summary(preview, preview_digest), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
