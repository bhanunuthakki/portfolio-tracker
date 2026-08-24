"""Atomic, revisioned replacement of the policy benchmark allocation."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from portfolio_tracker.models import PolicyState, PolicyWeight, PolicyWriteReceipt
from portfolio_tracker.schemas import (
    PolicyOut,
    PolicyRecomputationOut,
    PolicyReplaceIn,
    PolicyWeightOut,
    PolicyWriteReceiptOut,
)

_BALANCE_TOLERANCE_PCT = Decimal("0.01")
_BALANCE_TOLERANCE_BPS = 1
_TARGET_TOTAL_BPS = 10_000
_SINGLETON_ID = 1


class PolicyWriteError(Exception):
    """Base class for safe policy-write failures."""


class PolicyValidationError(PolicyWriteError):
    def __init__(self, reason: str) -> None:
        self.reason = reason


class PolicyRevisionConflictError(PolicyWriteError):
    def __init__(self, expected_revision: int, current_revision: int) -> None:
        self.expected_revision = expected_revision
        self.current_revision = current_revision


class PolicyIdempotencyConflictError(PolicyWriteError):
    pass


class PolicyRecomputationError(PolicyWriteError):
    pass


def read_policy(session: Session) -> PolicyOut:
    """Read the current normalized weights and governed revision metadata."""
    rows = session.execute(select(PolicyWeight).order_by(PolicyWeight.ticker)).scalars().all()
    state = session.get(PolicyState, _SINGLETON_ID)
    weights = [_to_out(row) for row in rows]
    total = sum((weight.weight_pct for weight in weights), Decimal(0))
    if state is None:
        # Migration 0023 creates the singleton. This fallback keeps metadata-
        # created test databases and pre-migration read-only clients legible.
        latest_update = max((row.updated_at for row in rows), default=None)
        as_of = _as_utc(latest_update or datetime.now(UTC))
        return PolicyOut(
            weights=weights,
            total_pct=total,
            is_balanced=abs(total - Decimal(100)) <= _BALANCE_TOLERANCE_PCT,
            revision=0,
            source="legacy",
            as_of=as_of,
            recomputation=PolicyRecomputationOut(status="current", policy_revision=0),
        )
    benchmark_status: Literal["current", "required"] = (
        "required" if state.benchmark_status == "required" else "current"
    )
    return PolicyOut(
        weights=weights,
        total_pct=total,
        is_balanced=abs(total - Decimal(100)) <= _BALANCE_TOLERANCE_PCT,
        revision=state.revision,
        source=state.source,
        as_of=_as_utc(state.as_of),
        recomputation=PolicyRecomputationOut(
            status=benchmark_status,
            policy_revision=state.revision,
            reason=("policy_weights_changed" if state.benchmark_status == "required" else None),
        ),
    )


def replace_policy(session: Session, request: PolicyReplaceIn) -> tuple[PolicyOut, bool]:
    """Apply one full-policy PUT and return its receipt-backed response.

    The idempotency check happens before the optimistic revision check so a
    client can safely retry after losing the original HTTP response.
    """
    normalized = _normalized_rows(request)
    request_hash = _request_hash(request, normalized)
    existing_receipt = session.scalar(
        select(PolicyWriteReceipt).where(
            PolicyWriteReceipt.idempotency_key == request.idempotency_key
        )
    )
    if existing_receipt is not None:
        if existing_receipt.request_hash != request_hash:
            raise PolicyIdempotencyConflictError
        return PolicyOut.model_validate_json(existing_receipt.response_json), True

    state = _ensure_state(session)
    if state.revision != request.expected_revision:
        raise PolicyRevisionConflictError(request.expected_revision, state.revision)

    now = datetime.now(UTC)
    next_revision = state.revision + 1
    result = cast(
        CursorResult[Any],
        session.execute(
            update(PolicyState)
            .where(
                PolicyState.singleton_id == _SINGLETON_ID,
                PolicyState.revision == request.expected_revision,
            )
            .values(
                revision=next_revision,
                source=request.source,
                as_of=request.as_of,
                updated_at=now,
            )
        ),
    )
    if result.rowcount != 1:
        current_revision = session.scalar(
            select(PolicyState.revision).where(PolicyState.singleton_id == _SINGLETON_ID)
        )
        raise PolicyRevisionConflictError(
            request.expected_revision,
            current_revision if current_revision is not None else 0,
        )

    session.execute(delete(PolicyWeight))
    for ticker, weight_bps, notes in normalized:
        session.add(PolicyWeight(ticker=ticker, weight_bps=weight_bps, notes=notes))

    try:
        _mark_benchmark_recomputation_required(session, next_revision, now)
    except PolicyRecomputationError:
        session.rollback()
        raise

    receipt = PolicyWriteReceipt(
        receipt_id=str(uuid.uuid4()),
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
        expected_revision=request.expected_revision,
        accepted_revision=next_revision,
        source=request.source,
        as_of=request.as_of,
        outcome="applied",
        response_json="{}",
    )
    session.add(receipt)
    session.flush()

    response = read_policy(session)
    response.receipt = PolicyWriteReceiptOut(
        receipt_id=receipt.receipt_id,
        idempotency_key=receipt.idempotency_key,
        outcome="applied",
        recorded_at=_as_utc(receipt.created_at),
    )
    receipt.response_json = response.model_dump_json()
    session.commit()

    # Re-read after commit; callers never receive the submitted object as if
    # it were persisted state.
    session.expire_all()
    fresh = read_policy(session)
    persisted_receipt = session.get(PolicyWriteReceipt, receipt.receipt_id)
    if persisted_receipt is None:  # pragma: no cover - database invariant
        raise RuntimeError("policy write receipt disappeared after commit")
    fresh.receipt = PolicyWriteReceiptOut(
        receipt_id=persisted_receipt.receipt_id,
        idempotency_key=persisted_receipt.idempotency_key,
        outcome="applied",
        recorded_at=_as_utc(persisted_receipt.created_at),
    )
    return fresh, False


def _normalized_rows(request: PolicyReplaceIn) -> list[tuple[str, int, str | None]]:
    seen: set[str] = set()
    normalized: list[tuple[str, int, str | None]] = []
    for row in request.weights:
        if row.ticker in seen:
            raise PolicyValidationError("duplicate_ticker")
        seen.add(row.ticker)
        normalized.append((row.ticker, _pct_to_bps(row.weight_pct), row.notes))
    if not normalized:
        raise PolicyValidationError("policy_must_not_be_empty")
    total_bps = sum(weight_bps for _, weight_bps, _ in normalized)
    if abs(total_bps - _TARGET_TOTAL_BPS) > _BALANCE_TOLERANCE_BPS:
        raise PolicyValidationError("policy_total_must_equal_100_pct")
    return sorted(normalized)


def _request_hash(request: PolicyReplaceIn, normalized: list[tuple[str, int, str | None]]) -> str:
    canonical = {
        "weights": normalized,
        "expected_revision": request.expected_revision,
        "idempotency_key": request.idempotency_key,
        "source": request.source,
        "as_of": request.as_of.isoformat(),
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _ensure_state(session: Session) -> PolicyState:
    state = session.get(PolicyState, _SINGLETON_ID)
    if state is not None:
        return state
    state = PolicyState(
        singleton_id=_SINGLETON_ID,
        revision=0,
        source="legacy",
        as_of=datetime.now(UTC),
        benchmark_status="current",
    )
    session.add(state)
    session.flush()
    return state


def _mark_benchmark_recomputation_required(
    session: Session, revision: int, invalidated_at: datetime
) -> None:
    """Persist the deterministic handoff to the existing benchmark job."""
    try:
        result = cast(
            CursorResult[Any],
            session.execute(
                update(PolicyState)
                .where(
                    PolicyState.singleton_id == _SINGLETON_ID,
                    PolicyState.revision == revision,
                )
                .values(
                    benchmark_status="required",
                    benchmark_invalidated_at=invalidated_at,
                )
            ),
        )
    except SQLAlchemyError:
        raise PolicyRecomputationError from None
    if result.rowcount != 1:
        raise PolicyRecomputationError


def _to_out(row: PolicyWeight) -> PolicyWeightOut:
    return PolicyWeightOut(
        ticker=row.ticker,
        weight_pct=_bps_to_pct(row.weight_bps),
        notes=row.notes,
        updated_at=_as_utc(row.updated_at),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bps_to_pct(bps: int) -> Decimal:
    return (Decimal(bps) / Decimal(100)).quantize(Decimal("0.01"))


def _pct_to_bps(pct: Decimal) -> int:
    return int((pct * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
