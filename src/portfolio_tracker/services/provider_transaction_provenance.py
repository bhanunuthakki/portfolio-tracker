"""Persist count-verified provider deliveries as cash-flow provenance.

This writer proves completeness only relative to the provider API response for
the requested account and date range. It does not claim that the provider has
the brokerage's complete lifetime archive. Ambiguous, in-kind, and unknown
activities remain explicit unresolved Source events with date-local gaps.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    CashFlowAccountMappingBasis,
    CashFlowBrokerArchiveCoverage,
    CashFlowDecisionAuthority,
    CashFlowEffectiveDateBasis,
    CashFlowEvidenceConfidence,
    CashFlowReconciliationDecision,
    CashFlowResolutionKind,
    CashFlowSourceAmountSignBasis,
    CashFlowSourceAttestation,
    CashFlowSourceAttestationEventLink,
    CashFlowSourceEvent,
    CashFlowSourceGap,
    CashFlowSourceGapReason,
    CashFlowSourceLocatorKind,
    CashFlowSourceType,
    InvestmentTransaction,
)
from portfolio_tracker.plaid_client import PlaidInvestmentTransaction
from portfolio_tracker.provider_delivery import (
    ProviderDeliveryError,
    ProviderDeliveryMetadata,
    canonical_normalized_record_set_sha256,
)
from portfolio_tracker.services.cashflow_source_coverage import (
    canonical_decision_payload_sha256,
)
from portfolio_tracker.services.external_flow_ledger import classify_by_name

if TYPE_CHECKING:
    from portfolio_tracker.services.provider_transaction_corrections import (
        ProviderTransactionCorrectionApproval,
        ProviderTransactionCorrectionPlan,
    )

_METHODOLOGY_VERSION = "provider-api-v1"
_SOURCE_TIMEZONE = "provider-date"
_MONEY_QUANTUM = Decimal("0.000001")
_QUANTITY_QUANTUM = Decimal("0.0000000001")
_MAX_MONEY = Decimal("100000000000000")
_MAX_QUANTITY = Decimal("1000000000000000000")
_KNOWN_NON_CANDIDATE_TYPES = frozenset({"buy", "sell", "cancel", "fee"})
_EXTERNAL_IN_CASH_SUBTYPES = frozenset({"contribution", "deposit"})
_EXTERNAL_OUT_CASH_SUBTYPES = frozenset({"withdrawal"})
_INTERNAL_CASH_SUBTYPES = frozenset(
    {
        "dividend",
        "interest",
        "optionassignment",
        "optionexpiration",
        "rei",
        "return of capital",
        "split",
        "substitute_dividend",
    }
)
_IN_KIND_CASH_SUBTYPES = frozenset({"external_asset_transfer_in", "external_asset_transfer_out"})
_INTERNAL_TRANSFER_SUBTYPES = frozenset(
    {"assignment", "exercise", "merger", "spin off", "split", "stock distribution"}
)


class ProviderTransactionConflictError(ProviderDeliveryError):
    """Stored normalized economics disagree with the newly delivered record."""

    def __init__(
        self,
        message: str,
        *,
        correction_plan: ProviderTransactionCorrectionPlan | None = None,
    ) -> None:
        self.correction_plan = correction_plan
        super().__init__(message)


@dataclass(frozen=True)
class ProviderHistoryGap:
    """Provider-disclosed interval whose broker history is not available."""

    start: date
    end: date


@dataclass(frozen=True)
class ProviderAccountTransactionCapture:
    account_id: int
    provider_account_id: str
    coverage_start: date
    coverage_end: date
    delivery: ProviderDeliveryMetadata
    transactions: tuple[PlaidInvestmentTransaction, ...]
    security_ids_by_provider_id: Mapping[str, int]
    captured_at: datetime
    provider_history_gaps: tuple[ProviderHistoryGap, ...] = ()
    # Count parity establishes response delivery, never archive completeness.
    # A caller may affirm coverage only from a distinct provider fact that
    # explicitly asserts the full requested history range.
    broker_archive_coverage_basis: Literal["provider_explicit_full_range"] | None = None


@dataclass(frozen=True)
class ProviderAttestationWriteResult:
    attestation_key: str
    created: bool
    source_row_count: int
    cashflow_candidate_count: int
    unresolved_event_count: int
    provider_history_gap_count: int

    @property
    def provider_response_has_no_known_gaps(self) -> bool:
        """Whether this capture has no explicit classification/history gap.

        This is deliberately not named broker-history completeness: even a
        count-verified response cannot prove the provider holds the broker's
        complete archive.
        """

        return self.unresolved_event_count == 0 and self.provider_history_gap_count == 0

    @property
    def provider_attestation_is_certifying(self) -> bool:
        """Backward-compatible alias for source-coverage certification only."""

        return self.provider_response_has_no_known_gaps


@dataclass(frozen=True)
class _DecisionSpec:
    resolution_kind: str
    classification: str | None
    signed_external_amount: Decimal | None
    effective_date: date | None
    effective_date_basis: str | None
    effective_timezone: str | None
    target_transaction_id: str | None
    confidence: str
    assumption_code: str


@dataclass(frozen=True)
class _EventSpec:
    source_event_id: str
    source_row_sha256: str
    transaction: PlaidInvestmentTransaction
    decision: _DecisionSpec


def _normalized_history_gaps(
    gaps: tuple[ProviderHistoryGap, ...],
) -> tuple[ProviderHistoryGap, ...]:
    merged: list[ProviderHistoryGap] = []
    for gap in sorted(set(gaps), key=lambda value: (value.start, value.end)):
        if not merged or gap.start > merged[-1].end + timedelta(days=1):
            merged.append(gap)
            continue
        previous = merged[-1]
        merged[-1] = ProviderHistoryGap(previous.start, max(previous.end, gap.end))
    return tuple(merged)


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_captured_at(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_storage_economics(transaction: PlaidInvestmentTransaction) -> None:
    money_values = (transaction.amount, transaction.price, transaction.fees)
    if any(
        value is not None
        and (
            not value.is_finite()
            or abs(value) >= _MAX_MONEY
            or value.quantize(_MONEY_QUANTUM) != value
        )
        for value in money_values
    ):
        raise ProviderTransactionConflictError(
            "provider transaction exceeds normalized money storage precision"
        )
    quantity = transaction.quantity
    if (
        not quantity.is_finite()
        or abs(quantity) >= _MAX_QUANTITY
        or quantity.quantize(_QUANTITY_QUANTUM) != quantity
    ):
        raise ProviderTransactionConflictError(
            "provider transaction exceeds normalized quantity storage precision"
        )


def _classify_candidate(transaction: PlaidInvestmentTransaction) -> _DecisionSpec | None:
    tx_type = transaction.type.lower().strip()
    subtype = (transaction.subtype or "").lower().strip()
    target = transaction.plaid_investment_transaction_id
    if tx_type in _KNOWN_NON_CANDIDATE_TYPES:
        return None
    if tx_type not in {"cash", "transfer"}:
        return _unresolved("provider_activity_type_unresolved")
    if subtype in _IN_KIND_CASH_SUBTYPES:
        return _unresolved("provider_in_kind_requires_reconciliation")

    # Some brokers publish portfolio income under a withdrawal subtype. The
    # existing owner-reviewed name rules are intentionally evaluated before
    # subtype direction, and their lower confidence is retained explicitly.
    name_classification = classify_by_name(transaction.name)
    if name_classification == "internal":
        return _DecisionSpec(
            CashFlowResolutionKind.INTERNAL,
            "internal",
            Decimal(0),
            transaction.date,
            CashFlowEffectiveDateBasis.PROVIDER_POSTING,
            _SOURCE_TIMEZONE,
            target,
            CashFlowEvidenceConfidence.HIGH,
            "provider_name_rule_internal",
        )
    if name_classification in {"external_in", "external_out"}:
        if transaction.amount == 0:
            return _unresolved("provider_external_cash_amount_zero")
        direction = Decimal(1) if name_classification == "external_in" else Decimal(-1)
        return _DecisionSpec(
            CashFlowResolutionKind.PROVIDER_EXACT,
            name_classification,
            direction * abs(transaction.amount),
            transaction.date,
            CashFlowEffectiveDateBasis.PROVIDER_POSTING,
            _SOURCE_TIMEZONE,
            target,
            CashFlowEvidenceConfidence.HIGH,
            f"provider_name_rule_{name_classification}",
        )
    if tx_type == "cash":
        if subtype in _EXTERNAL_IN_CASH_SUBTYPES:
            if transaction.amount == 0:
                return _unresolved("provider_external_cash_amount_zero")
            return _DecisionSpec(
                CashFlowResolutionKind.PROVIDER_EXACT,
                "external_in",
                abs(transaction.amount),
                transaction.date,
                CashFlowEffectiveDateBasis.PROVIDER_POSTING,
                _SOURCE_TIMEZONE,
                target,
                CashFlowEvidenceConfidence.EXACT,
                f"provider_cash_{subtype}",
            )
        if subtype in _EXTERNAL_OUT_CASH_SUBTYPES:
            if transaction.amount == 0:
                return _unresolved("provider_external_cash_amount_zero")
            return _DecisionSpec(
                CashFlowResolutionKind.PROVIDER_EXACT,
                "external_out",
                -abs(transaction.amount),
                transaction.date,
                CashFlowEffectiveDateBasis.PROVIDER_POSTING,
                _SOURCE_TIMEZONE,
                target,
                CashFlowEvidenceConfidence.EXACT,
                f"provider_cash_{subtype}",
            )
        if subtype in _INTERNAL_CASH_SUBTYPES:
            return _DecisionSpec(
                CashFlowResolutionKind.INTERNAL,
                "internal",
                Decimal(0),
                transaction.date,
                CashFlowEffectiveDateBasis.PROVIDER_POSTING,
                _SOURCE_TIMEZONE,
                target,
                CashFlowEvidenceConfidence.EXACT,
                f"provider_internal_{subtype.replace(' ', '_')}",
            )
        return _unresolved("provider_cash_classification_unresolved")
    if tx_type == "transfer":
        if subtype in _INTERNAL_TRANSFER_SUBTYPES:
            return _DecisionSpec(
                CashFlowResolutionKind.INTERNAL,
                "internal",
                Decimal(0),
                transaction.date,
                CashFlowEffectiveDateBasis.PROVIDER_POSTING,
                _SOURCE_TIMEZONE,
                target,
                CashFlowEvidenceConfidence.EXACT,
                f"provider_internal_{subtype.replace(' ', '_')}",
            )
        return _unresolved("provider_transfer_classification_unresolved")
    return _unresolved("provider_activity_type_unresolved")


def _unresolved(reason: str) -> _DecisionSpec:
    return _DecisionSpec(
        CashFlowResolutionKind.UNRESOLVED,
        None,
        None,
        None,
        None,
        None,
        None,
        CashFlowEvidenceConfidence.PROVISIONAL,
        reason,
    )


def _expected_security_id(
    capture: ProviderAccountTransactionCapture,
    transaction: PlaidInvestmentTransaction,
) -> int | None:
    provider_security_id = transaction.plaid_security_id
    if provider_security_id is None:
        return None
    security_id = capture.security_ids_by_provider_id.get(provider_security_id)
    if security_id is None:
        raise ProviderTransactionConflictError(
            "provider transaction references an unmapped normalized security"
        )
    return security_id


def _stored_transaction_matches(
    stored: InvestmentTransaction,
    source: PlaidInvestmentTransaction,
    *,
    account_id: int,
    security_id: int | None,
) -> bool:
    return (
        stored.account_id == account_id
        and stored.security_id == security_id
        and stored.date == source.date
        and stored.type == source.type
        and stored.subtype == source.subtype
        and Decimal(stored.amount) == source.amount
        and Decimal(stored.quantity) == source.quantity
        and (Decimal(stored.price) if stored.price is not None else None) == source.price
        and (Decimal(stored.fees) if stored.fees is not None else None) == source.fees
        and stored.currency == source.currency
        and stored.name == source.name
    )


def _event_specs(
    provider: str,
    account_identity_sha256: str,
    transactions: tuple[PlaidInvestmentTransaction, ...],
) -> tuple[_EventSpec, ...]:
    events: list[_EventSpec] = []
    for transaction in transactions:
        decision = _classify_candidate(transaction)
        if decision is None:
            continue
        row_sha256 = _digest(transaction.model_dump(mode="json"))
        source_event_id = _digest(
            {
                "identity_version": "provider_source_event.v2",
                "provider": provider,
                "account_identity_sha256": account_identity_sha256,
                "provider_record_id": transaction.plaid_investment_transaction_id,
            }
        )
        events.append(_EventSpec(source_event_id, row_sha256, transaction, decision))
    return tuple(sorted(events, key=lambda event: event.source_event_id))


def _events_for_attestation(
    session: Session,
    attestation: CashFlowSourceAttestation,
) -> tuple[CashFlowSourceEvent, ...]:
    if attestation.source_type == CashFlowSourceType.PROVIDER_API:
        return tuple(
            session.scalars(
                select(CashFlowSourceEvent)
                .join(
                    CashFlowSourceAttestationEventLink,
                    CashFlowSourceAttestationEventLink.source_event_id
                    == CashFlowSourceEvent.source_event_id,
                )
                .where(
                    CashFlowSourceAttestationEventLink.attestation_id == attestation.attestation_id
                )
                .order_by(CashFlowSourceEvent.source_event_id)
            )
        )
    return tuple(
        session.scalars(
            select(CashFlowSourceEvent)
            .where(CashFlowSourceEvent.attestation_id == attestation.attestation_id)
            .order_by(CashFlowSourceEvent.source_event_id)
        )
    )


def _decision_row(event: _EventSpec, approved_at: datetime) -> CashFlowReconciliationDecision:
    spec = event.decision
    row = CashFlowReconciliationDecision(
        decision_key="0" * 64,
        source_event_id=event.source_event_id,
        target_transaction_id=spec.target_transaction_id,
        resolution_kind=spec.resolution_kind,
        classification=spec.classification,
        signed_external_amount=spec.signed_external_amount,
        effective_date=spec.effective_date,
        effective_date_basis=spec.effective_date_basis,
        effective_timezone=spec.effective_timezone,
        decision_authority=CashFlowDecisionAuthority.PROVIDER,
        confidence=spec.confidence,
        assumption_code=spec.assumption_code,
        methodology_version=_METHODOLOGY_VERSION,
        decision_payload_sha256="0" * 64,
        approved_at=approved_at,
        superseded_at=None,
        superseded_by_decision_key=None,
    )
    row.decision_payload_sha256 = canonical_decision_payload_sha256(row)
    row.decision_key = _digest(
        {
            "identity_version": "provider_reconciliation_decision.v1",
            "source_event_id": row.source_event_id,
            "decision_payload_sha256": row.decision_payload_sha256,
        }
    )
    return row


def _decision_matches(
    current: CashFlowReconciliationDecision,
    desired: CashFlowReconciliationDecision,
) -> bool:
    return all(
        getattr(current, field) == getattr(desired, field)
        for field in (
            "decision_key",
            "target_transaction_id",
            "resolution_kind",
            "classification",
            "signed_external_amount",
            "effective_date",
            "effective_date_basis",
            "effective_timezone",
            "decision_authority",
            "confidence",
            "assumption_code",
            "methodology_version",
            "decision_payload_sha256",
        )
    )


def _stored_event_matches(stored: CashFlowSourceEvent, event: _EventSpec) -> bool:
    transaction = event.transaction
    return (
        stored.source_record_id == transaction.plaid_investment_transaction_id
        and stored.source_locator_kind == CashFlowSourceLocatorKind.PROVIDER_RECORD
        and stored.source_locator == transaction.plaid_investment_transaction_id
        and stored.source_row_sha256 == event.source_row_sha256
        and stored.activity_date == transaction.date
        and stored.process_date is None
        and stored.settlement_date is None
        and Decimal(stored.source_amount) == transaction.amount
        and stored.source_amount_sign_basis == CashFlowSourceAmountSignBasis.PROVIDER_REPORTED
        and stored.currency == transaction.currency
        and stored.source_code == (transaction.type if len(transaction.type) <= 32 else None)
    )


def _existing_capture_matches(
    session: Session,
    attestation: CashFlowSourceAttestation,
    *,
    source_sha256: str,
    source_event_set_sha256: str,
    manifest_sha256: str,
    source_row_count: int,
    events: tuple[_EventSpec, ...],
    gap_specs: tuple[tuple[date, date, str], ...],
    broker_archive_coverage: str,
) -> bool:
    if (
        attestation.source_sha256 != source_sha256
        or attestation.source_event_set_sha256 != source_event_set_sha256
        or attestation.manifest_sha256 != manifest_sha256
        or attestation.source_row_count != source_row_count
        or attestation.cashflow_candidate_count != len(events)
        or attestation.broker_archive_coverage != broker_archive_coverage
        or attestation.approved_at is None
        or attestation.superseded_at is not None
        or attestation.superseded_by_attestation_id is not None
    ):
        return False
    stored_events = _events_for_attestation(session, attestation)
    if {(event.source_event_id, event.source_row_sha256) for event in stored_events} != {
        (event.source_event_id, event.source_row_sha256) for event in events
    }:
        return False
    stored_gaps = tuple(
        session.scalars(
            select(CashFlowSourceGap).where(
                CashFlowSourceGap.attestation_id == attestation.attestation_id
            )
        )
    )
    if {(gap.gap_start, gap.gap_end, gap.reason_code) for gap in stored_gaps} != set(gap_specs):
        return False
    for event in events:
        decisions = tuple(
            session.scalars(
                select(CashFlowReconciliationDecision).where(
                    CashFlowReconciliationDecision.source_event_id == event.source_event_id,
                    CashFlowReconciliationDecision.superseded_at.is_(None),
                )
            )
        )
        if len(decisions) != 1:
            return False
        desired = _decision_row(event, decisions[0].approved_at or datetime.min)
        current = decisions[0]
        if not _decision_matches(current, desired):
            return False
    return True


def persist_provider_account_attestation(
    session: Session,
    capture: ProviderAccountTransactionCapture,
    *,
    transaction_correction_approval: ProviderTransactionCorrectionApproval | None = None,
) -> ProviderAttestationWriteResult:
    """Persist one account/range capture without committing the caller's transaction."""
    if session.get(Account, capture.account_id) is None:
        raise ProviderTransactionConflictError("provider capture account mapping is missing")
    if not capture.provider_account_id:
        raise ProviderTransactionConflictError("provider capture account locator is missing")
    if capture.coverage_start > capture.coverage_end:
        raise ProviderTransactionConflictError("provider capture date range is invalid")
    if (
        capture.delivery.requested_start_date != capture.coverage_start
        or capture.delivery.requested_end_date != capture.coverage_end
        or not capture.delivery.is_complete
    ):
        raise ProviderTransactionConflictError(
            "provider capture range does not match its count-verified delivery"
        )
    for transaction in capture.transactions:
        _validate_storage_economics(transaction)
        if not transaction.plaid_investment_transaction_id:
            raise ProviderTransactionConflictError("provider transaction locator is missing")
        if transaction.plaid_account_id != capture.provider_account_id:
            raise ProviderTransactionConflictError(
                "provider capture contains a transaction from another account"
            )
        if not capture.coverage_start <= transaction.date <= capture.coverage_end:
            raise ProviderTransactionConflictError(
                "provider capture contains a transaction outside its requested range"
            )
        if len(transaction.plaid_investment_transaction_id) > 512:
            raise ProviderTransactionConflictError("provider transaction locator is too long")
    for gap in capture.provider_history_gaps:
        if (
            gap.start > gap.end
            or gap.start < capture.coverage_start
            or gap.end > capture.coverage_end
        ):
            raise ProviderTransactionConflictError(
                "provider history gap falls outside the requested range"
            )
    history_gaps = _normalized_history_gaps(capture.provider_history_gaps)

    # SessionLocal has autoflush=False. Materialize all normalized transaction
    # parents before verifying their exact economics and inserting FK-dependent
    # Source events and decisions.
    session.flush()
    conflicting_transaction_found = False
    for transaction in capture.transactions:
        stored = session.get(
            InvestmentTransaction,
            transaction.plaid_investment_transaction_id,
        )
        expected_security_id = _expected_security_id(capture, transaction)
        if stored is None or not _stored_transaction_matches(
            stored,
            transaction,
            account_id=capture.account_id,
            security_id=expected_security_id,
        ):
            conflicting_transaction_found = True
            break
    if conflicting_transaction_found:
        from portfolio_tracker.services.provider_transaction_corrections import (
            apply_provider_transaction_corrections,
            preview_provider_transaction_corrections,
        )

        correction_plan = preview_provider_transaction_corrections(session, capture)
        if transaction_correction_approval is None:
            raise ProviderTransactionConflictError(
                "stored transaction conflicts with the count-verified provider payload",
                correction_plan=correction_plan,
            )
        apply_provider_transaction_corrections(
            session,
            capture,
            correction_plan,
            transaction_correction_approval,
        )

    captured_at = _normalize_captured_at(capture.captured_at)
    normalized_rows = [transaction.model_dump(mode="json") for transaction in capture.transactions]
    source_sha256 = canonical_normalized_record_set_sha256(normalized_rows)
    account_identity_sha256 = _digest(
        {
            "provider": capture.delivery.provider,
            "provider_account_id": capture.provider_account_id,
        }
    )
    attestation_key = _digest(
        {
            "identity_version": "provider_api_attestation.v1",
            "account_identity_sha256": account_identity_sha256,
            "coverage_start": capture.coverage_start.isoformat(),
            "coverage_end": capture.coverage_end.isoformat(),
            "source_sha256": source_sha256,
            "source_format": capture.delivery.source_format,
            "parser_version": capture.delivery.parser_version,
            "provider_history_gaps": [
                [gap.start.isoformat(), gap.end.isoformat()] for gap in history_gaps
            ],
            "broker_archive_coverage_basis": capture.broker_archive_coverage_basis,
        }
    )
    events = _event_specs(
        capture.delivery.provider,
        account_identity_sha256,
        capture.transactions,
    )
    unresolved_gap_dates = tuple(
        sorted(
            {
                event.transaction.date
                for event in events
                if event.decision.resolution_kind == CashFlowResolutionKind.UNRESOLVED
            }
        )
    )
    gap_specs = tuple(
        sorted(
            {
                *(
                    (
                        gap.start,
                        gap.end,
                        CashFlowSourceGapReason.PROVIDER_HISTORY_UNAVAILABLE.value,
                    )
                    for gap in history_gaps
                ),
                *(
                    (
                        gap_date,
                        gap_date,
                        CashFlowSourceGapReason.UNRESOLVED_CLASSIFICATION.value,
                    )
                    for gap_date in unresolved_gap_dates
                ),
            }
        )
    )
    source_event_set_sha256 = _digest(sorted(event.source_row_sha256 for event in events))
    manifest_sha256 = _digest(
        {
            "manifest_version": "provider_api_capture.v1",
            "attestation_key": attestation_key,
            "delivery_record_set_sha256": capture.delivery.record_set_sha256,
            "account_record_set_sha256": source_sha256,
            "source_event_ids": [event.source_event_id for event in events],
            "source_gaps": [
                [gap_start.isoformat(), gap_end.isoformat(), reason]
                for gap_start, gap_end, reason in gap_specs
            ],
            "broker_archive_coverage_basis": capture.broker_archive_coverage_basis,
        }
    )
    existing = session.scalar(
        select(CashFlowSourceAttestation).where(
            CashFlowSourceAttestation.attestation_key == attestation_key
        )
    )
    broker_archive_coverage = (
        CashFlowBrokerArchiveCoverage.PROVIDER_ASSERTED.value
        if capture.broker_archive_coverage_basis is not None
        else CashFlowBrokerArchiveCoverage.UNASSERTED.value
    )
    if existing is not None:
        if not _existing_capture_matches(
            session,
            existing,
            source_sha256=source_sha256,
            source_event_set_sha256=source_event_set_sha256,
            manifest_sha256=manifest_sha256,
            source_row_count=len(capture.transactions),
            events=events,
            gap_specs=gap_specs,
            broker_archive_coverage=broker_archive_coverage,
        ):
            raise ProviderTransactionConflictError("stored provider attestation has drifted")
        return ProviderAttestationWriteResult(
            attestation_key,
            False,
            len(capture.transactions),
            len(events),
            len(unresolved_gap_dates),
            len(history_gaps),
        )

    attestation = CashFlowSourceAttestation(
        attestation_key=attestation_key,
        account_id=capture.account_id,
        coverage_start=capture.coverage_start,
        coverage_end=capture.coverage_end,
        source_type=CashFlowSourceType.PROVIDER_API,
        source_reference=(
            f"provider_api:{capture.delivery.provider}:count_verified_response;"
            + (
                "broker_archive_coverage=provider_explicit_full_range"
                if capture.broker_archive_coverage_basis is not None
                else "broker_archive_coverage=unasserted"
            )
        ),
        broker_archive_coverage=broker_archive_coverage,
        source_sha256=source_sha256,
        captured_at=captured_at,
        approved_at=captured_at,
        methodology_version=_METHODOLOGY_VERSION,
        account_identity_sha256=account_identity_sha256,
        account_mapping_basis=CashFlowAccountMappingBasis.PROVIDER_ACCOUNT_ID,
        account_mapping_confidence=CashFlowEvidenceConfidence.EXACT,
        source_format=capture.delivery.source_format,
        parser_version=capture.delivery.parser_version,
        source_timezone=_SOURCE_TIMEZONE,
        source_row_count=len(capture.transactions),
        cashflow_candidate_count=len(events),
        source_event_set_sha256=source_event_set_sha256,
        manifest_sha256=manifest_sha256,
        superseded_at=None,
        superseded_by_attestation_id=None,
    )
    session.add(attestation)
    session.flush()

    for gap_start, gap_end, reason_code in gap_specs:
        session.add(
            CashFlowSourceGap(
                attestation_id=attestation.attestation_id,
                gap_start=gap_start,
                gap_end=gap_end,
                reason_code=reason_code,
            )
        )
    for event in events:
        transaction = event.transaction
        stored_event = session.get(CashFlowSourceEvent, event.source_event_id)
        if stored_event is None:
            stored_event = CashFlowSourceEvent(
                source_event_id=event.source_event_id,
                attestation_id=attestation.attestation_id,
                source_record_id=transaction.plaid_investment_transaction_id,
                source_locator_kind=CashFlowSourceLocatorKind.PROVIDER_RECORD,
                source_locator=transaction.plaid_investment_transaction_id,
                source_row_ordinal=None,
                source_page=None,
                source_line=None,
                source_row_sha256=event.source_row_sha256,
                activity_date=transaction.date,
                process_date=None,
                settlement_date=None,
                source_amount=transaction.amount,
                source_amount_sign_basis=CashFlowSourceAmountSignBasis.PROVIDER_REPORTED,
                currency=transaction.currency,
                source_code=(transaction.type if len(transaction.type) <= 32 else None),
            )
            session.add(stored_event)
            session.flush()
        elif not _stored_event_matches(stored_event, event):
            raise ProviderTransactionConflictError(
                "canonical provider source event conflicts with the delivered record"
            )

        session.add(
            CashFlowSourceAttestationEventLink(
                attestation_id=attestation.attestation_id,
                source_event_id=event.source_event_id,
            )
        )
        desired_decision = _decision_row(event, captured_at)
        current_decisions = tuple(
            session.scalars(
                select(CashFlowReconciliationDecision).where(
                    CashFlowReconciliationDecision.source_event_id == event.source_event_id,
                    CashFlowReconciliationDecision.superseded_at.is_(None),
                )
            )
        )
        if not current_decisions:
            session.add(desired_decision)
        elif len(current_decisions) != 1 or not _decision_matches(
            current_decisions[0], desired_decision
        ):
            raise ProviderTransactionConflictError(
                "canonical provider source event has conflicting decision lineage"
            )

    prior_attestations = tuple(
        session.scalars(
            select(CashFlowSourceAttestation).where(
                CashFlowSourceAttestation.account_id == capture.account_id,
                CashFlowSourceAttestation.source_type == CashFlowSourceType.PROVIDER_API,
                CashFlowSourceAttestation.source_format == capture.delivery.source_format,
                CashFlowSourceAttestation.coverage_start == capture.coverage_start,
                CashFlowSourceAttestation.coverage_end == capture.coverage_end,
                CashFlowSourceAttestation.attestation_id != attestation.attestation_id,
                CashFlowSourceAttestation.superseded_at.is_(None),
            )
        )
    )
    for prior in prior_attestations:
        prior.superseded_at = captured_at
        prior.superseded_by_attestation_id = attestation.attestation_id

    # Make link/event/decision identity immediately reusable even under the
    # application's production autoflush=False session configuration.
    session.flush()
    return ProviderAttestationWriteResult(
        attestation_key,
        True,
        len(capture.transactions),
        len(events),
        len(unresolved_gap_dates),
        len(history_gaps),
    )
