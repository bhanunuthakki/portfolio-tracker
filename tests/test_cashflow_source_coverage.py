from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    Benchmark,
    CashFlowReconciliationDecision,
    CashFlowSourceAttestation,
    CashFlowSourceEvent,
    CashFlowSourceGap,
    HoldingSnapshot,
    Item,
    Security,
)
from portfolio_tracker.services.cashflow_source_coverage import (
    assess_cashflow_source_coverage,
    canonical_decision_payload_sha256,
    canonical_source_event_set_sha256,
)
from portfolio_tracker.services.performance import compute_performance_series

_CAPTURED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
_APPROVED_AT = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def test_decision_digest_matches_reconciler_canonical_payload() -> None:
    decision = CashFlowReconciliationDecision(
        decision_key="1" * 64,
        source_event_id="2" * 64,
        target_transaction_id="provider-transaction",
        resolution_kind="provider_exact",
        classification="external_in",
        signed_external_amount=Decimal("100.00"),
        effective_date=date(2026, 1, 5),
        effective_date_basis="source_activity",
        effective_timezone="America/New_York",
        decision_authority="brokerage_statement",
        confidence="exact",
        assumption_code="statement_activity_date",
        methodology_version="2",
        decision_payload_sha256="3" * 64,
        approved_at=_APPROVED_AT,
    )
    producer_payload = {
        "source_event_id": "2" * 64,
        "target_transaction_id": "provider-transaction",
        "resolution_kind": "provider_exact",
        "classification": "external_in",
        "signed_external_amount": "100",
        "effective_date": "2026-01-05",
        "effective_date_basis": "source_activity",
        "effective_timezone": "America/New_York",
        "decision_authority": "brokerage_statement",
        "confidence": "exact",
        "assumption_code": "statement_activity_date",
        "methodology_version": "2",
    }
    expected = hashlib.sha256(
        json.dumps(
            producer_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()

    assert canonical_decision_payload_sha256(decision) == expected


def _valued_account(session, suffix: str) -> Account:
    item = Item(
        source="plaid",
        plaid_item_id=f"item-{suffix}",
        institution_name=f"Broker {suffix}",
        is_data_active=True,
    )
    account = Account(
        item=item,
        plaid_account_id=f"account-{suffix}",
        name=f"Account {suffix}",
        type="investment",
    )
    security = Security(plaid_security_id=f"security-{suffix}", ticker=f"T{suffix}")
    session.add_all([item, account, security])
    session.flush()
    session.add(
        HoldingSnapshot(
            snapshot_date=date(2026, 9, 1),
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal(1),
            institution_value=Decimal(100),
        )
    )
    return account


def _attestation(
    session,
    account: Account,
    *,
    key: str,
    start: date,
    end: date,
    approved: bool = True,
    digest_char: str = "a",
    enhanced: bool = True,
) -> CashFlowSourceAttestation:
    row = CashFlowSourceAttestation(
        attestation_key=key,
        account_id=account.account_id,
        coverage_start=start,
        coverage_end=end,
        source_type="brokerage_statement",
        source_reference=f"synthetic:{key}",
        source_sha256=digest_char * 64,
        captured_at=_CAPTURED_AT,
        approved_at=_APPROVED_AT if approved else None,
        methodology_version="1",
        account_identity_sha256=(digest_char * 64 if enhanced else None),
        account_mapping_basis=("owner_confirmed" if enhanced else None),
        account_mapping_confidence=("exact" if enhanced else None),
        source_format=("synthetic" if enhanced else None),
        parser_version=("test-v1" if enhanced else None),
        source_timezone=("UTC" if enhanced else None),
        source_row_count=(0 if enhanced else None),
        cashflow_candidate_count=(0 if enhanced else None),
        source_event_set_sha256=(canonical_source_event_set_sha256(()) if enhanced else None),
        manifest_sha256=("f" * 64 if enhanced else None),
    )
    session.add(row)
    session.flush()
    return row


def test_legacy_document_only_attestation_is_visible_but_non_certifying(session):
    account = _valued_account(session, "legacy")
    _attestation(
        session,
        account,
        key="legacy-document-only",
        start=date(2026, 1, 2),
        end=date(2026, 1, 31),
        enhanced=False,
    )
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({account.account_id}),
    )

    assert result.is_complete is False
    assert result.attestations[0].validation_reason_codes == (
        "source_attestation_event_provenance_missing",
    )


def test_pre_in_kind_parser_attestation_is_visible_but_non_certifying(session):
    account = _valued_account(session, "old-robinhood-parser")
    attestation = _attestation(
        session,
        account,
        key="old-robinhood-parser",
        start=date(2026, 1, 2),
        end=date(2026, 1, 31),
    )
    attestation.source_format = "robinhood_activity_csv"
    attestation.parser_version = "robinhood_activity_csv.v3"
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({account.account_id}),
    )

    assert result.is_complete is False
    assert result.attestations[0].validation_reason_codes == (
        "source_attestation_parser_version_unsupported",
    )


def test_declared_candidate_count_must_equal_persisted_source_events(session):
    account = _valued_account(session, "count")
    attestation = _attestation(
        session,
        account,
        key="candidate-count-mismatch",
        start=date(2026, 1, 2),
        end=date(2026, 1, 31),
    )
    attestation.source_row_count = 1
    attestation.cashflow_candidate_count = 1
    session.add(
        CashFlowSourceEvent(
            source_event_id="1" * 64,
            attestation_id=attestation.attestation_id,
            source_locator_kind="row",
            source_locator="row:1",
            source_row_ordinal=1,
            source_row_sha256="2" * 64,
            activity_date=date(2026, 1, 10),
            source_amount=Decimal(100),
            source_amount_sign_basis="statement_printed",
            currency="USD",
        )
    )
    # Delete the event after declaring it: the persisted candidate set is
    # incomplete even though the document-level attestation is approved.
    session.flush()
    session.query(CashFlowSourceEvent).delete()
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({account.account_id}),
    )

    assert result.is_complete is False
    assert "source_attestation_candidate_count_mismatch" in (
        result.attestations[0].validation_reason_codes
    )


def test_source_date_basis_mismatch_prevents_attestation_certification(session):
    account = _valued_account(session, "basis")
    attestation = _attestation(
        session,
        account,
        key="date-basis-mismatch",
        start=date(2026, 1, 2),
        end=date(2026, 1, 31),
    )
    event = CashFlowSourceEvent(
        source_event_id="4" * 64,
        attestation_id=attestation.attestation_id,
        source_locator_kind="row",
        source_locator="row:1",
        source_row_ordinal=1,
        source_row_sha256="5" * 64,
        activity_date=date(2026, 1, 10),
        source_amount=Decimal(100),
        source_amount_sign_basis="statement_printed",
        currency="USD",
    )
    decision = CashFlowReconciliationDecision(
        decision_key="6" * 64,
        source_event_id=event.source_event_id,
        target_transaction_id=None,
        resolution_kind="internal",
        classification="internal",
        signed_external_amount=Decimal(0),
        effective_date=date(2026, 1, 11),
        effective_date_basis="source_activity",
        effective_timezone="America/New_York",
        decision_authority="brokerage_statement",
        confidence="exact",
        assumption_code="statement_activity_date",
        methodology_version="2",
        decision_payload_sha256="7" * 64,
        approved_at=_APPROVED_AT,
    )
    decision.decision_payload_sha256 = canonical_decision_payload_sha256(decision)
    attestation.source_row_count = 1
    attestation.cashflow_candidate_count = 1
    attestation.source_event_set_sha256 = canonical_source_event_set_sha256((event,))
    session.add_all([event, decision])
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({account.account_id}),
    )

    assert result.is_complete is False
    assert "source_attestation_event_effective_date_basis_mismatch" in (
        result.attestations[0].validation_reason_codes
    )


def test_missing_attestation_is_not_inferred_complete(session):
    account = _valued_account(session, "missing")
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({account.account_id}),
    )

    assert result.is_complete is False
    assert result.status == "missing"
    assert result.required_start_date == date(2026, 1, 2)
    assert result.required_end_date == date(2026, 1, 31)
    assert result.accounts[0].uncovered_ranges == ((date(2026, 1, 2), date(2026, 1, 31)),)
    assert result.attestations == ()


def test_partial_and_explicit_gap_remain_uncovered(session):
    account = _valued_account(session, "partial")
    row = _attestation(
        session,
        account,
        key="partial-attestation",
        start=date(2026, 1, 2),
        end=date(2026, 1, 20),
    )
    session.add(
        CashFlowSourceGap(
            attestation_id=row.attestation_id,
            gap_start=date(2026, 1, 10),
            gap_end=date(2026, 1, 12),
            reason_code="provider_history_unavailable",
        )
    )
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({account.account_id}),
    )

    assert result.is_complete is False
    assert result.status == "partial"
    assert result.accounts[0].covered_ranges == (
        (date(2026, 1, 2), date(2026, 1, 9)),
        (date(2026, 1, 13), date(2026, 1, 20)),
    )
    assert result.accounts[0].uncovered_ranges == (
        (date(2026, 1, 10), date(2026, 1, 12)),
        (date(2026, 1, 21), date(2026, 1, 31)),
    )
    assert result.attestations[0].source_sha256 == "a" * 64
    assert result.attestations[0].gaps[0].reason_code == "provider_history_unavailable"


def test_resolved_decision_closes_only_unresolved_classification_gap(session):
    account = _valued_account(session, "resolved-provider-gap")
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    event_date = date(2026, 1, 10)
    attestation = _attestation(
        session,
        account,
        key="provider-gap",
        start=start + timedelta(days=1),
        end=end,
    )
    attestation.source_type = "provider_api"
    event = CashFlowSourceEvent(
        source_event_id="8" * 64,
        attestation_id=attestation.attestation_id,
        source_record_id="provider-record",
        source_locator_kind="provider_record",
        source_locator="provider-record",
        source_row_sha256="9" * 64,
        activity_date=event_date,
        source_amount=Decimal(0),
        source_amount_sign_basis="provider_reported",
        currency="USD",
    )
    unresolved = CashFlowReconciliationDecision(
        decision_key="a" * 64,
        source_event_id=event.source_event_id,
        target_transaction_id=None,
        resolution_kind="unresolved",
        classification=None,
        signed_external_amount=None,
        effective_date=None,
        effective_date_basis=None,
        effective_timezone=None,
        decision_authority="provider",
        confidence="provisional",
        assumption_code="provider_transfer_classification_unresolved",
        methodology_version="provider-api-v1",
        decision_payload_sha256="b" * 64,
        approved_at=_APPROVED_AT,
    )
    unresolved.decision_payload_sha256 = canonical_decision_payload_sha256(unresolved)
    attestation.source_row_count = 1
    attestation.cashflow_candidate_count = 1
    attestation.source_event_set_sha256 = canonical_source_event_set_sha256((event,))
    session.add_all(
        [
            event,
            unresolved,
            CashFlowSourceGap(
                attestation_id=attestation.attestation_id,
                gap_start=event_date,
                gap_end=event_date,
                reason_code="unresolved_classification",
            ),
            CashFlowSourceGap(
                attestation_id=attestation.attestation_id,
                gap_start=date(2026, 1, 20),
                gap_end=date(2026, 1, 21),
                reason_code="provider_history_unavailable",
            ),
        ]
    )
    session.commit()

    before = assess_cashflow_source_coverage(
        session, start, end, account_ids=frozenset({account.account_id})
    )
    assert before.accounts[0].uncovered_ranges == (
        (event_date, event_date),
        (date(2026, 1, 20), date(2026, 1, 21)),
    )

    resolved = CashFlowReconciliationDecision(
        decision_key="c" * 64,
        source_event_id=event.source_event_id,
        target_transaction_id=None,
        resolution_kind="internal",
        classification="internal",
        signed_external_amount=Decimal(0),
        effective_date=event_date,
        effective_date_basis="owner_resolved",
        effective_timezone="America/New_York",
        decision_authority="owner_approved",
        confidence="exact",
        assumption_code="corroborating_evidence_resolves_provider_unresolved",
        methodology_version="2",
        decision_payload_sha256="d" * 64,
        approved_at=_APPROVED_AT + timedelta(days=1),
    )
    resolved.decision_payload_sha256 = canonical_decision_payload_sha256(resolved)
    unresolved.superseded_at = _APPROVED_AT + timedelta(days=1)
    unresolved.superseded_by_decision_key = resolved.decision_key
    session.add(resolved)
    session.commit()

    after = assess_cashflow_source_coverage(
        session, start, end, account_ids=frozenset({account.account_id})
    )
    assert after.accounts[0].uncovered_ranges == ((date(2026, 1, 20), date(2026, 1, 21)),)
    assert after.attestations[0].gaps[0].reason_code == "unresolved_classification"


def test_unresolved_classification_gap_stays_open_until_every_event_is_resolved(session):
    account = _valued_account(session, "partially-resolved-provider-gap")
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    attestation = _attestation(
        session,
        account,
        key="multi-event-provider-gap",
        start=start + timedelta(days=1),
        end=end,
    )
    attestation.source_type = "provider_api"
    events: list[CashFlowSourceEvent] = []
    decisions: list[CashFlowReconciliationDecision] = []
    for index, event_date in enumerate((date(2026, 1, 10), date(2026, 1, 11)), start=1):
        event = CashFlowSourceEvent(
            source_event_id=str(index) * 64,
            attestation_id=attestation.attestation_id,
            source_record_id=f"provider-record-{index}",
            source_locator_kind="provider_record",
            source_locator=f"provider-record-{index}",
            source_row_sha256=str(index + 2) * 64,
            activity_date=event_date,
            source_amount=Decimal(0),
            source_amount_sign_basis="provider_reported",
            currency="USD",
        )
        decision = CashFlowReconciliationDecision(
            decision_key=str(index + 4) * 64,
            source_event_id=event.source_event_id,
            target_transaction_id=None,
            resolution_kind="internal" if index == 1 else "unresolved",
            classification="internal" if index == 1 else None,
            signed_external_amount=Decimal(0) if index == 1 else None,
            effective_date=event_date if index == 1 else None,
            effective_date_basis="owner_resolved" if index == 1 else None,
            effective_timezone="America/New_York" if index == 1 else None,
            decision_authority="owner_approved" if index == 1 else "provider",
            confidence="exact" if index == 1 else "provisional",
            assumption_code=(
                "corroborating_evidence_resolves_provider_unresolved"
                if index == 1
                else "provider_transfer_classification_unresolved"
            ),
            methodology_version="2" if index == 1 else "provider-api-v1",
            decision_payload_sha256="f" * 64,
            approved_at=_APPROVED_AT,
        )
        decision.decision_payload_sha256 = canonical_decision_payload_sha256(decision)
        events.append(event)
        decisions.append(decision)
    attestation.source_row_count = len(events)
    attestation.cashflow_candidate_count = len(events)
    attestation.source_event_set_sha256 = canonical_source_event_set_sha256(tuple(events))
    session.add_all(
        [
            *events,
            *decisions,
            CashFlowSourceGap(
                attestation_id=attestation.attestation_id,
                gap_start=date(2026, 1, 10),
                gap_end=date(2026, 1, 11),
                reason_code="unresolved_classification",
            ),
        ]
    )
    session.commit()

    result = assess_cashflow_source_coverage(
        session, start, end, account_ids=frozenset({account.account_id})
    )

    assert result.accounts[0].uncovered_ranges == ((date(2026, 1, 10), date(2026, 1, 11)),)


def test_superseded_attestation_does_not_count(session):
    account = _valued_account(session, "superseded")
    old = _attestation(
        session,
        account,
        key="old-attestation",
        start=date(2026, 1, 2),
        end=date(2026, 1, 31),
    )
    replacement = _attestation(
        session,
        account,
        key="replacement-attestation",
        start=date(2026, 1, 15),
        end=date(2026, 1, 31),
        digest_char="b",
    )
    old.superseded_at = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    old.superseded_by_attestation_id = replacement.attestation_id
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({account.account_id}),
    )

    assert result.is_complete is False
    assert result.accounts[0].uncovered_ranges == ((date(2026, 1, 2), date(2026, 1, 14)),)
    assert [row.attestation_key for row in result.attestations] == [
        "old-attestation",
        "replacement-attestation",
    ]
    assert result.attestations[0].lifecycle_status == "superseded"
    assert result.attestations[0].superseded_by_attestation_key == "replacement-attestation"


def test_all_accounts_must_be_fully_attested(session):
    first = _valued_account(session, "first")
    second = _valued_account(session, "second")
    for account, key, digest in (
        (first, "first-statement", "c"),
        (second, "second-provider-export", "d"),
    ):
        _attestation(
            session,
            account,
            key=key,
            start=date(2026, 1, 2),
            end=date(2026, 1, 31),
            digest_char=digest,
        )
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({first.account_id, second.account_id}),
    )

    assert result.is_complete is True
    assert result.status == "complete"
    assert all(not account.uncovered_ranges for account in result.accounts)


def test_draft_attestation_does_not_count(session):
    account = _valued_account(session, "draft")
    _attestation(
        session,
        account,
        key="draft-attestation",
        start=date(2026, 1, 2),
        end=date(2026, 1, 31),
        approved=False,
    )
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({account.account_id}),
    )

    assert result.is_complete is False
    assert result.status == "missing"
    assert len(result.attestations) == 1
    assert result.attestations[0].lifecycle_status == "draft"


def test_cash_flow_api_distinguishes_structural_and_source_completeness(client, session):
    account = _valued_account(session, "api")
    session.commit()

    response = client.get(
        "/api/v1/cash-flows",
        params={"start_date": "2026-01-01", "end_date": "2026-01-31"},
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["structural_is_complete"] is True
    assert payload["source_coverage"]["status"] == "missing"
    assert payload["source_coverage"]["is_complete"] is False
    assert payload["is_complete"] is False
    assert payload["net_external_cashflow_in"] is None

    _attestation(
        session,
        account,
        key="api-attestation",
        start=date(2026, 1, 2),
        end=date(2026, 1, 31),
    )
    session.commit()

    payload = client.get(
        "/api/v1/cash-flows",
        params={"start_date": "2026-01-01", "end_date": "2026-01-31"},
    ).json()
    assert payload["structural_is_complete"] is True
    assert payload["source_coverage"]["status"] == "complete"
    assert payload["is_complete"] is True
    assert Decimal(payload["net_external_cashflow_in"]) == 0


def test_performance_fails_closed_until_source_window_is_attested(session):
    account = _valued_account(session, "performance")
    security_id = session.query(Security.security_id).filter_by(ticker="Tperformance").scalar()
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=security_id,
                quantity=Decimal(1),
                institution_value=Decimal(100),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=security_id,
                quantity=Decimal(1),
                institution_value=Decimal(110),
            ),
            Benchmark(symbol="SPY", date=date(2025, 12, 31), close=Decimal(100)),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=date(2026, 1, 30), close=Decimal(110)),
            Benchmark(symbol="SPY", date=end, close=Decimal(110)),
            Benchmark(symbol="QQQ", date=date(2025, 12, 31), close=Decimal(100)),
            Benchmark(symbol="QQQ", date=start, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=date(2026, 1, 30), close=Decimal(110)),
            Benchmark(symbol="QQQ", date=end, close=Decimal(110)),
        ]
    )
    session.commit()

    unavailable = compute_performance_series(session, start, end)

    assert unavailable.calculation_status == "unavailable"
    assert unavailable.calculation_reason_codes == ["external_flow_source_coverage_incomplete"]
    assert unavailable.source_coverage.status == "missing"
    assert unavailable.net_external_cashflow_in is None

    _attestation(
        session,
        account,
        key="performance-attestation",
        start=start + timedelta(days=1),
        end=end,
    )
    session.commit()

    available = compute_performance_series(session, start, end)
    assert available.calculation_status == "available"
    assert available.calculation_reason_codes == []
    assert available.source_coverage.is_complete is True
    assert available.net_external_cashflow_in == 0
