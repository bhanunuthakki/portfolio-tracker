"""Durable source-coverage assessment for the canonical external-flow ledger.

Ledger construction proves that stored rows are structurally usable. This
module answers a separate question: has every logical valued account been
reconciled to approved authoritative evidence for every source-history date
that can affect the requested return window?

Attestation ranges are inclusive. Whole-portfolio returns use an end-of-day
opening value, so a ``[start, end]`` return requires cash-flow source coverage
for ``[start + 1 day, end]``. No attestation means no source coverage; absence
of structural ledger issues is never treated as evidence of completeness.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    CashFlowReconciliationDecision,
    CashFlowSourceAttestation,
    CashFlowSourceAttestationEventLink,
    CashFlowSourceEvent,
    CashFlowSourceGap,
)
from portfolio_tracker.schemas import (
    CashFlowAccountSourceCoverageOut,
    CashFlowSourceAttestationOut,
    CashFlowSourceCoverageOut,
    CashFlowSourceGapOut,
    SourceCoverageRangeOut,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CERTIFYING_PARSER_VERSIONS: dict[str, frozenset[str]] = {
    "robinhood_activity_csv": frozenset({"robinhood_activity_csv.v4"}),
    "plaid_investment_transactions_api": frozenset(
        {"plaid_investment_tx.v1", "plaid_investment_tx.v2"}
    ),
    "snaptrade_account_activities_api": frozenset(
        {"snaptrade_account_activity.v1", "snaptrade_account_activity.v2"}
    ),
    # Existing deterministic fixtures exercise the same validation path.
    "synthetic": frozenset({"test-v1"}),
}

DateRange = tuple[date, date]
BrokerArchiveCoverage = Literal[
    "unasserted", "provider_asserted", "statement_attested", "owner_asserted"
]
BrokerArchiveStatus = Literal["complete", "partial", "unasserted"]


def _broker_archive_coverage(attestation: CashFlowSourceAttestation) -> BrokerArchiveCoverage:
    """Return archive assurance independently from API delivery completeness."""

    if attestation.source_type == "brokerage_statement":
        return "statement_attested"
    if attestation.source_type == "owner_reconciliation":
        return "owner_asserted"
    if attestation.broker_archive_coverage == "provider_asserted":
        return "provider_asserted"
    return "unasserted"


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _decimal_text(value: object) -> str | None:
    if value is None:
        return None
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    if decimal_value == 0:
        return "0"
    return format(decimal_value.normalize(), "f")


def canonical_source_event_set_sha256(events: tuple[CashFlowSourceEvent, ...]) -> str:
    """Recompute the producer's ordered source-row set commitment."""
    return _canonical_digest(sorted(event.source_row_sha256 for event in events))


def canonical_decision_payload_sha256(decision: CashFlowReconciliationDecision) -> str:
    """Recompute the immutable decision payload commitment."""
    return _canonical_digest(
        {
            "source_event_id": decision.source_event_id,
            "target_transaction_id": decision.target_transaction_id,
            "resolution_kind": decision.resolution_kind,
            "classification": decision.classification,
            "signed_external_amount": _decimal_text(decision.signed_external_amount),
            "effective_date": (
                decision.effective_date.isoformat() if decision.effective_date is not None else None
            ),
            "effective_date_basis": decision.effective_date_basis,
            "effective_timezone": decision.effective_timezone,
            "decision_authority": decision.decision_authority,
            "confidence": decision.confidence,
            "assumption_code": decision.assumption_code,
            "methodology_version": decision.methodology_version,
        }
    )


def decision_date_basis_matches_source(
    decision: CashFlowReconciliationDecision,
    event: CashFlowSourceEvent,
) -> bool:
    """Validate date-basis semantics that can be proven from source evidence."""
    basis = decision.effective_date_basis
    effective_date = decision.effective_date
    if decision.resolution_kind == "unresolved":
        return basis is None and effective_date is None
    if basis == "source_activity":
        return event.activity_date is not None and effective_date == event.activity_date
    if basis == "source_process":
        return event.process_date is not None and effective_date == event.process_date
    if basis == "source_settlement":
        return event.settlement_date is not None and effective_date == event.settlement_date
    if basis == "provider_posting":
        # The target transaction is loaded by the ledger and checked there.
        return effective_date is not None and decision.target_transaction_id is not None
    if basis == "owner_resolved":
        return (
            effective_date is not None
            and decision.decision_authority == "owner_approved"
            and bool(decision.assumption_code)
        )
    return False


@dataclass(frozen=True)
class SourceGapEvidence:
    start_date: date
    end_date: date
    reason_code: str


@dataclass(frozen=True)
class SourceAttestationEvidence:
    attestation_key: str
    account_id: int
    coverage_start: date
    coverage_end: date
    source_type: str
    broker_archive_coverage: BrokerArchiveCoverage
    source_reference: str
    source_sha256: str
    captured_at: datetime
    approved_at: datetime | None
    lifecycle_status: str
    superseded_at: datetime | None
    superseded_by_attestation_key: str | None
    methodology_version: str
    account_identity_sha256: str | None
    account_mapping_basis: str | None
    account_mapping_confidence: str | None
    source_format: str | None
    parser_version: str | None
    source_timezone: str | None
    source_row_count: int | None
    cashflow_candidate_count: int | None
    persisted_source_event_count: int
    source_event_set_sha256: str | None
    manifest_sha256: str | None
    gaps: tuple[SourceGapEvidence, ...]
    validation_reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class AccountSourceCoverage:
    account_id: int
    status: str
    covered_ranges: tuple[DateRange, ...]
    uncovered_ranges: tuple[DateRange, ...]
    attestation_keys: tuple[str, ...]
    broker_archive_status: BrokerArchiveStatus
    broker_archive_covered_ranges: tuple[DateRange, ...]
    broker_archive_uncovered_ranges: tuple[DateRange, ...]


@dataclass(frozen=True)
class CashFlowSourceCoverageAssessment:
    status: str
    is_complete: bool
    broker_archive_status: BrokerArchiveStatus
    broker_archive_is_complete: bool
    requested_start_date: date
    requested_end_date: date
    required_start_date: date | None
    required_end_date: date | None
    accounts: tuple[AccountSourceCoverage, ...]
    attestations: tuple[SourceAttestationEvidence, ...]


def source_coverage_out(
    assessment: CashFlowSourceCoverageAssessment,
) -> CashFlowSourceCoverageOut:
    """Convert the deterministic assessment to the public read model."""
    return CashFlowSourceCoverageOut(
        status=assessment.status,
        is_complete=assessment.is_complete,
        broker_archive_status=assessment.broker_archive_status,
        broker_archive_is_complete=assessment.broker_archive_is_complete,
        requested_start_date=assessment.requested_start_date,
        requested_end_date=assessment.requested_end_date,
        required_start_date=assessment.required_start_date,
        required_end_date=assessment.required_end_date,
        accounts=[
            CashFlowAccountSourceCoverageOut(
                account_id=account.account_id,
                status=account.status,
                covered_ranges=[
                    SourceCoverageRangeOut(start_date=start, end_date=end)
                    for start, end in account.covered_ranges
                ],
                uncovered_ranges=[
                    SourceCoverageRangeOut(start_date=start, end_date=end)
                    for start, end in account.uncovered_ranges
                ],
                attestation_keys=list(account.attestation_keys),
                broker_archive_status=account.broker_archive_status,
                broker_archive_covered_ranges=[
                    SourceCoverageRangeOut(start_date=start, end_date=end)
                    for start, end in account.broker_archive_covered_ranges
                ],
                broker_archive_uncovered_ranges=[
                    SourceCoverageRangeOut(start_date=start, end_date=end)
                    for start, end in account.broker_archive_uncovered_ranges
                ],
            )
            for account in assessment.accounts
        ],
        attestations=[
            CashFlowSourceAttestationOut(
                attestation_key=row.attestation_key,
                account_id=row.account_id,
                coverage_start=row.coverage_start,
                coverage_end=row.coverage_end,
                source_type=row.source_type,
                broker_archive_coverage=row.broker_archive_coverage,
                source_reference=row.source_reference,
                source_sha256=row.source_sha256,
                captured_at=row.captured_at,
                approved_at=row.approved_at,
                lifecycle_status=row.lifecycle_status,
                superseded_at=row.superseded_at,
                superseded_by_attestation_key=row.superseded_by_attestation_key,
                methodology_version=row.methodology_version,
                account_identity_sha256=row.account_identity_sha256,
                account_mapping_basis=row.account_mapping_basis,
                account_mapping_confidence=row.account_mapping_confidence,
                source_format=row.source_format,
                parser_version=row.parser_version,
                source_timezone=row.source_timezone,
                source_row_count=row.source_row_count,
                cashflow_candidate_count=row.cashflow_candidate_count,
                persisted_source_event_count=row.persisted_source_event_count,
                source_event_set_sha256=row.source_event_set_sha256,
                manifest_sha256=row.manifest_sha256,
                gaps=[
                    CashFlowSourceGapOut(
                        start_date=gap.start_date,
                        end_date=gap.end_date,
                        reason_code=gap.reason_code,
                    )
                    for gap in row.gaps
                ],
                validation_reason_codes=list(row.validation_reason_codes),
            )
            for row in assessment.attestations
        ],
    )


def _merge_ranges(ranges: list[DateRange]) -> tuple[DateRange, ...]:
    if not ranges:
        return ()
    ordered = sorted(ranges)
    merged: list[DateRange] = [ordered[0]]
    for start, end in ordered[1:]:
        prior_start, prior_end = merged[-1]
        if start <= prior_end + timedelta(days=1):
            merged[-1] = (prior_start, max(prior_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _subtract_ranges(base: DateRange, exclusions: tuple[DateRange, ...]) -> list[DateRange]:
    remaining = [base]
    for exclusion_start, exclusion_end in _merge_ranges(list(exclusions)):
        next_remaining: list[DateRange] = []
        for current_start, current_end in remaining:
            if exclusion_end < current_start or exclusion_start > current_end:
                next_remaining.append((current_start, current_end))
                continue
            if exclusion_start > current_start:
                next_remaining.append((current_start, exclusion_start - timedelta(days=1)))
            if exclusion_end < current_end:
                next_remaining.append((exclusion_end + timedelta(days=1), current_end))
        remaining = next_remaining
    return remaining


def _uncovered_ranges(required: DateRange, covered: tuple[DateRange, ...]) -> tuple[DateRange, ...]:
    return tuple(_subtract_ranges(required, covered))


def _validation_reason_codes(
    attestation: CashFlowSourceAttestation,
    gaps: tuple[CashFlowSourceGap, ...],
    events: tuple[CashFlowSourceEvent, ...],
    current_decisions: dict[str, tuple[CashFlowReconciliationDecision, ...]],
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if not _SHA256_RE.fullmatch(attestation.source_sha256):
        reasons.add("source_attestation_invalid_sha256")
    if any(
        gap.gap_start < attestation.coverage_start or gap.gap_end > attestation.coverage_end
        for gap in gaps
    ):
        reasons.add("source_attestation_gap_outside_coverage")
    enhanced_fields = (
        attestation.account_identity_sha256,
        attestation.account_mapping_basis,
        attestation.account_mapping_confidence,
        attestation.source_format,
        attestation.parser_version,
        attestation.source_timezone,
        attestation.source_row_count,
        attestation.cashflow_candidate_count,
        attestation.source_event_set_sha256,
        attestation.manifest_sha256,
    )
    if any(value is None for value in enhanced_fields):
        reasons.add("source_attestation_event_provenance_missing")
        return tuple(sorted(reasons))
    if not _SHA256_RE.fullmatch(attestation.account_identity_sha256 or ""):
        reasons.add("source_attestation_invalid_account_identity_sha256")
    if not _SHA256_RE.fullmatch(attestation.source_event_set_sha256 or ""):
        reasons.add("source_attestation_invalid_event_set_sha256")
    if not _SHA256_RE.fullmatch(attestation.manifest_sha256 or ""):
        reasons.add("source_attestation_invalid_manifest_sha256")
    if attestation.account_mapping_confidence == "provisional":
        reasons.add("source_attestation_account_mapping_provisional")
    accepted_parser_versions = _CERTIFYING_PARSER_VERSIONS.get(attestation.source_format or "")
    if accepted_parser_versions is None:
        reasons.add("source_attestation_source_format_unsupported")
    elif attestation.parser_version not in accepted_parser_versions:
        reasons.add("source_attestation_parser_version_unsupported")
    if attestation.cashflow_candidate_count != len(events):
        reasons.add("source_attestation_candidate_count_mismatch")
    if attestation.source_event_set_sha256 != canonical_source_event_set_sha256(events):
        reasons.add("source_attestation_event_set_digest_mismatch")
    for event in events:
        if not _SHA256_RE.fullmatch(event.source_row_sha256):
            reasons.add("source_attestation_event_invalid_row_sha256")
        event_dates = tuple(
            candidate
            for candidate in (event.activity_date, event.process_date, event.settlement_date)
            if candidate is not None
        )
        if not event_dates or all(
            candidate < attestation.coverage_start or candidate > attestation.coverage_end
            for candidate in event_dates
        ):
            reasons.add("source_attestation_event_outside_coverage")
        decisions = current_decisions.get(event.source_event_id, ())
        if not decisions:
            reasons.add("source_attestation_event_current_decision_missing")
            continue
        if len(decisions) != 1:
            reasons.add("source_attestation_event_current_decision_conflict")
            continue
        decision = decisions[0]
        if decision.approved_at is None:
            reasons.add("source_attestation_event_decision_unapproved")
        if decision.decision_payload_sha256 != canonical_decision_payload_sha256(decision):
            reasons.add("source_attestation_event_decision_digest_mismatch")
        if not decision_date_basis_matches_source(decision, event):
            reasons.add("source_attestation_event_effective_date_basis_mismatch")
        if decision.resolution_kind == "unresolved":
            event_date = event.activity_date or event.process_date or event.settlement_date
            if event_date is None or not any(
                gap.reason_code == "unresolved_classification"
                and gap.gap_start <= event_date <= gap.gap_end
                for gap in gaps
            ):
                reasons.add("source_attestation_event_unresolved_gap_missing")
            # A correctly localized unresolved decision is represented by the
            # explicit gap. It invalidates that date, not the attestation's
            # independently resolved coverage outside the gap.
            continue
        if decision.confidence == "provisional":
            reasons.add("source_attestation_event_decision_provisional")
    return tuple(sorted(reasons))


def assess_cashflow_source_coverage(
    session: Session,
    start_date: date,
    end_date: date,
    *,
    account_ids: frozenset[int],
) -> CashFlowSourceCoverageAssessment:
    """Assess approved evidence coverage for every account in a return window.

    Draft and superseded attestations remain visible for lineage but never
    contribute coverage. Invalid attestations also fail closed. Multiple
    current approved attestations may combine to cover one account; an
    explicit gap may be covered by another independent attestation.
    """
    required_start = start_date + timedelta(days=1)
    if required_start > end_date or not account_ids:
        return CashFlowSourceCoverageAssessment(
            status="complete",
            is_complete=True,
            broker_archive_status="complete",
            broker_archive_is_complete=True,
            requested_start_date=start_date,
            requested_end_date=end_date,
            required_start_date=None if required_start > end_date else required_start,
            required_end_date=None if required_start > end_date else end_date,
            accounts=tuple(
                AccountSourceCoverage(
                    account_id=account_id,
                    status="complete",
                    covered_ranges=(),
                    uncovered_ranges=(),
                    attestation_keys=(),
                    broker_archive_status="complete",
                    broker_archive_covered_ranges=(),
                    broker_archive_uncovered_ranges=(),
                )
                for account_id in sorted(account_ids)
            ),
            attestations=(),
        )

    rows = (
        session.execute(
            select(CashFlowSourceAttestation)
            .where(CashFlowSourceAttestation.account_id.in_(account_ids))
            .where(CashFlowSourceAttestation.coverage_end >= required_start)
            .where(CashFlowSourceAttestation.coverage_start <= end_date)
            .order_by(
                CashFlowSourceAttestation.account_id,
                CashFlowSourceAttestation.coverage_start,
                CashFlowSourceAttestation.attestation_id,
            )
        )
        .scalars()
        .all()
    )
    attestation_ids = [row.attestation_id for row in rows]
    gaps_by_attestation: dict[int, list[CashFlowSourceGap]] = defaultdict(list)
    event_map_by_attestation: dict[int, dict[str, CashFlowSourceEvent]] = defaultdict(dict)
    current_decisions: dict[str, list[CashFlowReconciliationDecision]] = defaultdict(list)
    if attestation_ids:
        gap_rows = (
            session.execute(
                select(CashFlowSourceGap)
                .where(CashFlowSourceGap.attestation_id.in_(attestation_ids))
                .order_by(
                    CashFlowSourceGap.attestation_id,
                    CashFlowSourceGap.gap_start,
                    CashFlowSourceGap.gap_id,
                )
            )
            .scalars()
            .all()
        )
        for gap in gap_rows:
            gaps_by_attestation[gap.attestation_id].append(gap)
        event_rows = (
            session.execute(
                select(CashFlowSourceEvent)
                .where(CashFlowSourceEvent.attestation_id.in_(attestation_ids))
                .order_by(
                    CashFlowSourceEvent.attestation_id,
                    CashFlowSourceEvent.source_event_id,
                )
            )
            .scalars()
            .all()
        )
        for event in event_rows:
            event_map_by_attestation[event.attestation_id][event.source_event_id] = event
        linked_event_rows = session.execute(
            select(CashFlowSourceAttestationEventLink.attestation_id, CashFlowSourceEvent)
            .join(
                CashFlowSourceEvent,
                CashFlowSourceEvent.source_event_id
                == CashFlowSourceAttestationEventLink.source_event_id,
            )
            .where(CashFlowSourceAttestationEventLink.attestation_id.in_(attestation_ids))
            .order_by(
                CashFlowSourceAttestationEventLink.attestation_id,
                CashFlowSourceEvent.source_event_id,
            )
        ).all()
        for attestation_id, event in linked_event_rows:
            event_map_by_attestation[attestation_id][event.source_event_id] = event
        event_ids = sorted(
            {event_id for events in event_map_by_attestation.values() for event_id in events}
        )
        if event_ids:
            decision_rows = (
                session.execute(
                    select(CashFlowReconciliationDecision)
                    .where(CashFlowReconciliationDecision.source_event_id.in_(event_ids))
                    .where(CashFlowReconciliationDecision.superseded_at.is_(None))
                    .order_by(
                        CashFlowReconciliationDecision.source_event_id,
                        CashFlowReconciliationDecision.decision_key,
                    )
                )
                .scalars()
                .all()
            )
            for decision in decision_rows:
                current_decisions[decision.source_event_id].append(decision)

    replacement_ids = {
        row.superseded_by_attestation_id
        for row in rows
        if row.superseded_by_attestation_id is not None
    }
    replacement_keys: dict[int, str] = {}
    if replacement_ids:
        replacement_rows = session.execute(
            select(
                CashFlowSourceAttestation.attestation_id,
                CashFlowSourceAttestation.attestation_key,
            ).where(CashFlowSourceAttestation.attestation_id.in_(replacement_ids))
        ).all()
        for replacement_id, replacement_key in replacement_rows:
            replacement_keys[replacement_id] = replacement_key

    evidence: list[SourceAttestationEvidence] = []
    coverable_by_account: dict[int, list[DateRange]] = defaultdict(list)
    archive_coverable_by_account: dict[int, list[DateRange]] = defaultdict(list)
    keys_by_account: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        raw_gaps = tuple(gaps_by_attestation[row.attestation_id])
        raw_events = tuple(event_map_by_attestation[row.attestation_id].values())
        validation_reasons = _validation_reason_codes(
            row,
            raw_gaps,
            raw_events,
            {key: tuple(value) for key, value in current_decisions.items()},
        )
        if row.superseded_at is not None or row.superseded_by_attestation_id is not None:
            lifecycle_status = "superseded"
        elif row.approved_at is None:
            lifecycle_status = "draft"
        else:
            lifecycle_status = "approved"
        evidence.append(
            SourceAttestationEvidence(
                attestation_key=row.attestation_key,
                account_id=row.account_id,
                coverage_start=row.coverage_start,
                coverage_end=row.coverage_end,
                source_type=row.source_type,
                broker_archive_coverage=_broker_archive_coverage(row),
                source_reference=row.source_reference,
                source_sha256=row.source_sha256,
                captured_at=row.captured_at,
                approved_at=row.approved_at,
                lifecycle_status=lifecycle_status,
                superseded_at=row.superseded_at,
                superseded_by_attestation_key=(
                    replacement_keys.get(row.superseded_by_attestation_id)
                    if row.superseded_by_attestation_id is not None
                    else None
                ),
                methodology_version=row.methodology_version,
                account_identity_sha256=row.account_identity_sha256,
                account_mapping_basis=row.account_mapping_basis,
                account_mapping_confidence=row.account_mapping_confidence,
                source_format=row.source_format,
                parser_version=row.parser_version,
                source_timezone=row.source_timezone,
                source_row_count=row.source_row_count,
                cashflow_candidate_count=row.cashflow_candidate_count,
                persisted_source_event_count=len(raw_events),
                source_event_set_sha256=row.source_event_set_sha256,
                manifest_sha256=row.manifest_sha256,
                gaps=tuple(
                    SourceGapEvidence(
                        start_date=gap.gap_start,
                        end_date=gap.gap_end,
                        reason_code=gap.reason_code,
                    )
                    for gap in raw_gaps
                ),
                validation_reason_codes=validation_reasons,
            )
        )
        if lifecycle_status != "approved" or validation_reasons:
            continue
        clipped = (max(row.coverage_start, required_start), min(row.coverage_end, end_date))
        gap_ranges = tuple((gap.gap_start, gap.gap_end) for gap in raw_gaps)
        coverable_by_account[row.account_id].extend(_subtract_ranges(clipped, gap_ranges))
        if _broker_archive_coverage(row) != "unasserted":
            archive_coverable_by_account[row.account_id].extend(
                _subtract_ranges(clipped, gap_ranges)
            )
        keys_by_account[row.account_id].append(row.attestation_key)

    account_results: list[AccountSourceCoverage] = []
    for account_id in sorted(account_ids):
        covered = _merge_ranges(coverable_by_account[account_id])
        uncovered = _uncovered_ranges((required_start, end_date), covered)
        archive_covered = _merge_ranges(archive_coverable_by_account[account_id])
        archive_uncovered = _uncovered_ranges((required_start, end_date), archive_covered)
        if not uncovered:
            status = "complete"
        elif covered:
            status = "partial"
        else:
            status = "missing"
        archive_status: BrokerArchiveStatus
        if not archive_uncovered:
            archive_status = "complete"
        elif archive_covered:
            archive_status = "partial"
        else:
            archive_status = "unasserted"
        account_results.append(
            AccountSourceCoverage(
                account_id=account_id,
                status=status,
                covered_ranges=covered,
                uncovered_ranges=uncovered,
                attestation_keys=tuple(keys_by_account[account_id]),
                broker_archive_status=archive_status,
                broker_archive_covered_ranges=archive_covered,
                broker_archive_uncovered_ranges=archive_uncovered,
            )
        )

    complete = all(account.status == "complete" for account in account_results)
    if complete:
        overall_status = "complete"
    elif any(account.covered_ranges for account in account_results):
        overall_status = "partial"
    else:
        overall_status = "missing"
    archive_complete = all(
        account.broker_archive_status == "complete" for account in account_results
    )
    archive_status: BrokerArchiveStatus
    if archive_complete:
        archive_status = "complete"
    elif any(account.broker_archive_covered_ranges for account in account_results):
        archive_status = "partial"
    else:
        archive_status = "unasserted"
    return CashFlowSourceCoverageAssessment(
        status=overall_status,
        is_complete=complete,
        broker_archive_status=archive_status,
        broker_archive_is_complete=archive_complete,
        requested_start_date=start_date,
        requested_end_date=end_date,
        required_start_date=required_start,
        required_end_date=end_date,
        accounts=tuple(account_results),
        attestations=tuple(evidence),
    )
