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

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import CashFlowSourceAttestation, CashFlowSourceGap
from portfolio_tracker.schemas import (
    CashFlowAccountSourceCoverageOut,
    CashFlowSourceAttestationOut,
    CashFlowSourceCoverageOut,
    CashFlowSourceGapOut,
    SourceCoverageRangeOut,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DateRange = tuple[date, date]


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
    source_reference: str
    source_sha256: str
    captured_at: datetime
    approved_at: datetime | None
    lifecycle_status: str
    superseded_at: datetime | None
    superseded_by_attestation_key: str | None
    methodology_version: str
    gaps: tuple[SourceGapEvidence, ...]
    validation_reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class AccountSourceCoverage:
    account_id: int
    status: str
    covered_ranges: tuple[DateRange, ...]
    uncovered_ranges: tuple[DateRange, ...]
    attestation_keys: tuple[str, ...]


@dataclass(frozen=True)
class CashFlowSourceCoverageAssessment:
    status: str
    is_complete: bool
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
                source_reference=row.source_reference,
                source_sha256=row.source_sha256,
                captured_at=row.captured_at,
                approved_at=row.approved_at,
                lifecycle_status=row.lifecycle_status,
                superseded_at=row.superseded_at,
                superseded_by_attestation_key=row.superseded_by_attestation_key,
                methodology_version=row.methodology_version,
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
    attestation: CashFlowSourceAttestation, gaps: tuple[CashFlowSourceGap, ...]
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if not _SHA256_RE.fullmatch(attestation.source_sha256):
        reasons.add("source_attestation_invalid_sha256")
    if any(
        gap.gap_start < attestation.coverage_start or gap.gap_end > attestation.coverage_end
        for gap in gaps
    ):
        reasons.add("source_attestation_gap_outside_coverage")
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
    keys_by_account: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        raw_gaps = tuple(gaps_by_attestation[row.attestation_id])
        validation_reasons = _validation_reason_codes(row, raw_gaps)
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
        keys_by_account[row.account_id].append(row.attestation_key)

    account_results: list[AccountSourceCoverage] = []
    for account_id in sorted(account_ids):
        covered = _merge_ranges(coverable_by_account[account_id])
        uncovered = _uncovered_ranges((required_start, end_date), covered)
        if not uncovered:
            status = "complete"
        elif covered:
            status = "partial"
        else:
            status = "missing"
        account_results.append(
            AccountSourceCoverage(
                account_id=account_id,
                status=status,
                covered_ranges=covered,
                uncovered_ranges=uncovered,
                attestation_keys=tuple(keys_by_account[account_id]),
            )
        )

    complete = all(account.status == "complete" for account in account_results)
    if complete:
        overall_status = "complete"
    elif any(account.covered_ranges for account in account_results):
        overall_status = "partial"
    else:
        overall_status = "missing"
    return CashFlowSourceCoverageAssessment(
        status=overall_status,
        is_complete=complete,
        requested_start_date=start_date,
        requested_end_date=end_date,
        required_start_date=required_start,
        required_end_date=end_date,
        accounts=tuple(account_results),
        attestations=tuple(evidence),
    )
