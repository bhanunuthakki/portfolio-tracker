"""Write and query immutable whole-account valuation evidence.

The provider/statement adapter is the sole writer.  This module normalizes its
input, derives a stable content key, and flushes without committing so callers
retain ownership of the transaction boundary.  Return calculations are
readers: they may select only complete observations and must retain the
returned observation key in their boundary provenance.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    AccountValuationObservation,
    AccountValuationSourceKind,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_NORMALIZATION_VERSION = "1"
_MAX_ABSOLUTE_VALUE = Decimal("100000000000000")
_STORAGE_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class NewAccountValuationObservation:
    """Validated adapter payload before persistence."""

    account_id: int
    as_of_date: date
    total_value: Decimal
    cash_value: Decimal | None
    currency: str
    source_kind: AccountValuationSourceKind
    source_provider: str
    source_reference: str
    fetched_at: datetime
    is_complete: bool
    is_empty: bool
    as_of_at: datetime | None = None
    source_record_id: str | None = None
    source_payload_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class AccountValuationWriteResult:
    observation: AccountValuationObservation
    created: bool


@dataclass(frozen=True, slots=True)
class AccountValuationBoundaryEvidence:
    """Stable receipt fields for one return boundary value."""

    valuation_observation_id: int
    observation_key: str
    account_id: int
    as_of_date: date
    as_of_at: datetime | None
    total_value: Decimal
    cash_value: Decimal | None
    currency: str
    source_kind: str
    source_provider: str
    source_reference: str
    source_record_id: str | None
    source_payload_sha256: str | None
    normalization_version: str
    fetched_at: datetime
    is_complete: bool
    is_empty: bool


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_account_balance_source_sha256(
    *,
    source_provider: str,
    provider_account_id: str,
    source_reference: str,
    as_of_date: date,
    total_value: Decimal,
    cash_value: Decimal | None,
    currency: str,
) -> str:
    """Commit to the normalized provider balance fields used by the writer.

    Provider responses can contain credentials or unrelated personal data, so
    ingest paths hash this explicit, reproducible subset instead of retaining
    or serializing an entire SDK response.
    """
    payload = {
        "source_provider": source_provider,
        "provider_account_id": provider_account_id,
        "source_reference": source_reference,
        "as_of_date": as_of_date.isoformat(),
        "total_value": _decimal_text(total_value),
        "cash_value": _decimal_text(cash_value),
        "currency": currency,
        "normalization_version": _NORMALIZATION_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_money(name: str, value: Decimal) -> None:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if abs(value) >= _MAX_ABSOLUTE_VALUE:
        raise ValueError(f"{name} exceeds Numeric(20, 6) capacity")
    if value.quantize(_STORAGE_QUANTUM) != value:
        raise ValueError(f"{name} cannot have more than six decimal places")


def _validate_input(value: NewAccountValuationObservation) -> None:
    if value.account_id <= 0:
        raise ValueError("account_id must be positive")
    _validate_money("total_value", value.total_value)
    if value.cash_value is not None:
        _validate_money("cash_value", value.cash_value)
    if not re.fullmatch(r"[A-Z]{3}", value.currency):
        raise ValueError("currency must be a three-letter uppercase code")
    try:
        AccountValuationSourceKind(value.source_kind)
    except (TypeError, ValueError) as exc:
        raise ValueError("source_kind must be an AccountValuationSourceKind") from exc
    if len(value.source_provider) > 64 or not _PROVIDER_RE.fullmatch(value.source_provider):
        raise ValueError("source_provider must be a lowercase stable identifier")
    if (
        not value.source_reference
        or value.source_reference != value.source_reference.strip()
        or len(value.source_reference) > 512
    ):
        raise ValueError("source_reference must contain at most 512 non-blank characters")
    if value.source_record_id is not None and (
        not value.source_record_id
        or value.source_record_id != value.source_record_id.strip()
        or len(value.source_record_id) > 512
    ):
        raise ValueError("source_record_id must contain at most 512 non-blank characters")
    if value.source_record_id is None and value.source_payload_sha256 is None:
        raise ValueError("source_record_id or source_payload_sha256 is required")
    if value.source_payload_sha256 is not None and not _SHA256_RE.fullmatch(
        value.source_payload_sha256
    ):
        raise ValueError("source_payload_sha256 must be a lowercase SHA-256 digest")
    if value.fetched_at.tzinfo is None or value.fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    if value.as_of_at is not None and (
        value.as_of_at.tzinfo is None or value.as_of_at.utcoffset() is None
    ):
        raise ValueError("as_of_at must be timezone-aware when supplied")
    if value.as_of_at is not None and value.fetched_at < value.as_of_at:
        raise ValueError("fetched_at cannot precede as_of_at")
    if value.is_empty and (
        not value.is_complete
        or value.total_value != 0
        or (value.cash_value is not None and value.cash_value != 0)
    ):
        raise ValueError("empty account observation must be complete with zero total and cash")


def account_valuation_observation_key(value: NewAccountValuationObservation) -> str:
    """Return the stable identity of one exact provider capture occurrence."""
    _validate_input(value)
    encoded = json.dumps(
        account_valuation_observation_payload(value),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def account_valuation_observation_payload(
    value: NewAccountValuationObservation,
) -> dict[str, object]:
    """Return the canonical semantic payload committed by an observation key."""
    _validate_input(value)
    return {
        "account_id": value.account_id,
        "as_of_date": value.as_of_date.isoformat(),
        "as_of_at": _utc_text(value.as_of_at),
        "total_value": _decimal_text(value.total_value),
        "cash_value": _decimal_text(value.cash_value),
        "currency": value.currency,
        "source_kind": AccountValuationSourceKind(value.source_kind).value,
        "source_provider": value.source_provider,
        "source_reference": value.source_reference,
        "source_record_id": value.source_record_id,
        "source_payload_sha256": value.source_payload_sha256,
        "normalization_version": _NORMALIZATION_VERSION,
        "fetched_at": _utc_text(value.fetched_at),
        "is_complete": value.is_complete,
        "is_empty": value.is_empty,
    }


def _stored_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    # SQLite's DateTime adapter preserves the normalized UTC wall time but
    # drops tzinfo. Other engines can return an aware value.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def stored_account_valuation_observation_payload(
    observation: AccountValuationObservation,
) -> dict[str, object]:
    """Reconstruct and validate the canonical payload of a persisted row."""
    if observation.normalization_version != _NORMALIZATION_VERSION:
        raise ValueError("stored account valuation uses an unsupported normalization version")
    fetched_at = _stored_utc(observation.fetched_at)
    assert fetched_at is not None
    value = NewAccountValuationObservation(
        account_id=observation.account_id,
        as_of_date=observation.as_of_date,
        as_of_at=_stored_utc(observation.as_of_at),
        total_value=observation.total_value,
        cash_value=observation.cash_value,
        currency=observation.currency,
        source_kind=AccountValuationSourceKind(observation.source_kind),
        source_provider=observation.source_provider,
        source_reference=observation.source_reference,
        source_record_id=observation.source_record_id,
        source_payload_sha256=observation.source_payload_sha256,
        fetched_at=fetched_at,
        is_complete=observation.is_complete,
        is_empty=observation.is_empty,
    )
    payload = account_valuation_observation_payload(value)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    actual_key = hashlib.sha256(encoded).hexdigest()
    if actual_key != observation.observation_key:
        raise ValueError("stored account valuation payload does not match observation_key")
    return payload


def record_account_valuation_observation(
    session: Session,
    value: NewAccountValuationObservation,
) -> AccountValuationWriteResult:
    """Insert immutable evidence once; caller owns commit/rollback."""
    observation_key = account_valuation_observation_key(value)
    existing = session.scalar(
        select(AccountValuationObservation).where(
            AccountValuationObservation.observation_key == observation_key
        )
    )
    if existing is not None:
        # Idempotency is safe only when the supposedly immutable stored row
        # still hashes to the requested key. Never bless a tampered payload.
        stored_account_valuation_observation_payload(existing)
        return AccountValuationWriteResult(observation=existing, created=False)

    row = AccountValuationObservation(
        observation_key=observation_key,
        account_id=value.account_id,
        as_of_date=value.as_of_date,
        as_of_at=value.as_of_at.astimezone(UTC) if value.as_of_at is not None else None,
        total_value=value.total_value,
        cash_value=value.cash_value,
        currency=value.currency,
        source_kind=AccountValuationSourceKind(value.source_kind).value,
        source_provider=value.source_provider,
        source_reference=value.source_reference,
        source_record_id=value.source_record_id,
        source_payload_sha256=value.source_payload_sha256,
        normalization_version=_NORMALIZATION_VERSION,
        fetched_at=value.fetched_at.astimezone(UTC),
        is_complete=value.is_complete,
        is_empty=value.is_empty,
    )
    session.add(row)
    # Production SessionLocal has autoflush disabled. Flush here so the ID is
    # available to provenance receipts and constraint failures are immediate.
    session.flush()
    return AccountValuationWriteResult(observation=row, created=True)


def latest_complete_account_valuation_on_or_before(
    session: Session,
    *,
    account_id: int,
    boundary_date: date,
    earliest_date: date | None = None,
    currency: str | None = None,
    source_kinds: tuple[AccountValuationSourceKind, ...] | None = None,
) -> AccountValuationObservation | None:
    """Find the freshest complete evidence in an explicit boundary interval.

    No implicit staleness tolerance is applied. Callers requiring an exact
    boundary pass ``earliest_date=boundary_date``; callers permitting an older
    statement must make that interval explicit.
    """
    return latest_complete_account_valuations_on_or_before(
        session,
        account_ids=(account_id,),
        boundary_date=boundary_date,
        earliest_date=earliest_date,
        currency=currency,
        source_kinds=source_kinds,
    ).get(account_id)


def latest_complete_account_valuations_on_or_before(
    session: Session,
    *,
    account_ids: Iterable[int],
    boundary_date: date,
    earliest_date: date | None = None,
    currency: str | None = None,
    source_kinds: tuple[AccountValuationSourceKind, ...] | None = None,
) -> dict[int, AccountValuationObservation]:
    """Find one freshest complete observation for each requested account.

    Missing dictionary keys are deliberate, fail-closed evidence that a
    requested account lacks a qualifying observation in the caller's interval.
    """
    requested_accounts = tuple(sorted(set(account_ids)))
    if any(account_id <= 0 for account_id in requested_accounts):
        raise ValueError("account_ids must be positive")
    if earliest_date is not None and earliest_date > boundary_date:
        raise ValueError("earliest_date cannot follow boundary_date")
    if currency is not None and not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be a three-letter uppercase code")
    if source_kinds is not None and not source_kinds:
        raise ValueError("source_kinds cannot be empty when supplied")
    try:
        source_kind_values = (
            tuple(AccountValuationSourceKind(kind).value for kind in source_kinds)
            if source_kinds is not None
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("source_kinds contains an invalid source kind") from exc
    if not requested_accounts:
        return {}

    statement = (
        select(AccountValuationObservation)
        .where(AccountValuationObservation.account_id.in_(requested_accounts))
        .where(AccountValuationObservation.as_of_date <= boundary_date)
        .where(AccountValuationObservation.is_complete.is_(True))
    )
    if earliest_date is not None:
        statement = statement.where(AccountValuationObservation.as_of_date >= earliest_date)
    if currency is not None:
        statement = statement.where(AccountValuationObservation.currency == currency)
    if source_kind_values is not None:
        statement = statement.where(AccountValuationObservation.source_kind.in_(source_kind_values))
    statement = statement.order_by(
        AccountValuationObservation.account_id,
        AccountValuationObservation.as_of_date.desc(),
        AccountValuationObservation.fetched_at.desc(),
        AccountValuationObservation.valuation_observation_id.desc(),
    )
    selected: dict[int, AccountValuationObservation] = {}
    invalid_accounts: set[int] = set()
    for observation in session.scalars(statement):
        try:
            stored_account_valuation_observation_payload(observation)
        except (TypeError, ValueError):
            invalid_accounts.add(observation.account_id)
            selected.pop(observation.account_id, None)
            continue
        if observation.account_id in invalid_accounts:
            continue
        selected.setdefault(observation.account_id, observation)
    return selected


def account_valuation_boundary_evidence(
    observation: AccountValuationObservation,
) -> AccountValuationBoundaryEvidence:
    """Project a selected complete observation into a durable return receipt."""
    if not observation.is_complete:
        raise ValueError("partial account valuation cannot certify a return boundary")
    return AccountValuationBoundaryEvidence(
        valuation_observation_id=observation.valuation_observation_id,
        observation_key=observation.observation_key,
        account_id=observation.account_id,
        as_of_date=observation.as_of_date,
        as_of_at=observation.as_of_at,
        total_value=observation.total_value,
        cash_value=observation.cash_value,
        currency=observation.currency,
        source_kind=observation.source_kind,
        source_provider=observation.source_provider,
        source_reference=observation.source_reference,
        source_record_id=observation.source_record_id,
        source_payload_sha256=observation.source_payload_sha256,
        normalization_version=observation.normalization_version,
        fetched_at=observation.fetched_at,
        is_complete=observation.is_complete,
        is_empty=observation.is_empty,
    )
