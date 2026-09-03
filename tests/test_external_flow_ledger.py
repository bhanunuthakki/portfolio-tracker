from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_tracker.models import (
    Account,
    CashFlowReconciliationDecision,
    CashFlowSourceAttestation,
    CashFlowSourceEvent,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Price,
    PriceAdjustmentBasis,
    PriceSource,
    Security,
    StockSplit,
    TransactionOverride,
)
from portfolio_tracker.services.cashflow_source_coverage import (
    canonical_decision_payload_sha256,
    canonical_source_event_set_sha256,
)
from portfolio_tracker.services.external_flow_ledger import (
    IncompleteExternalFlowLedgerError,
    build_external_flow_ledger,
)
from portfolio_tracker.services.performance import (
    _daily_external_cashflow_assessment,
    _daily_external_cashflows,
)


def _account(session, suffix: str, source: str = "plaid") -> Account:
    item = Item(
        source=source,
        plaid_item_id=f"item-{suffix}",
        institution_name=f"Broker {suffix}",
        is_data_active=True,
    )
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id,
        plaid_account_id=f"account-{suffix}",
        name=f"Account {suffix}",
        type="investment",
    )
    session.add(account)
    session.flush()
    return account


def _security(session, suffix: str, ticker: str) -> Security:
    security = Security(plaid_security_id=f"security-{suffix}", ticker=ticker, type="cs")
    session.add(security)
    session.flush()
    return security


def _mark_valued(session, account: Account, security: Security) -> None:
    session.add(
        HoldingSnapshot(
            snapshot_date=date(2026, 9, 3),
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal(1),
            institution_value=Decimal(100),
        )
    )


def _transaction(
    session,
    *,
    transaction_id: str,
    account: Account,
    on_date: date,
    type_: str,
    subtype: str,
    amount: Decimal,
    quantity: Decimal = Decimal(0),
    security: Security | None = None,
    name: str | None = None,
) -> None:
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id=transaction_id,
            account_id=account.account_id,
            security_id=security.security_id if security is not None else None,
            date=on_date,
            name=name,
            quantity=quantity,
            amount=amount,
            type=type_,
            subtype=subtype,
            currency="USD",
        )
    )


def _approved_statement_decision(
    session,
    *,
    account: Account,
    transaction_id: str,
    event_id: str,
    decision_key: str,
    on_date: date,
) -> tuple[CashFlowSourceAttestation, CashFlowReconciliationDecision]:
    captured = datetime(2026, 1, 1, tzinfo=UTC)
    approved = datetime(2026, 1, 2, tzinfo=UTC)
    attestation = CashFlowSourceAttestation(
        attestation_key=f"attestation-{event_id[:8]}",
        account_id=account.account_id,
        coverage_start=on_date,
        coverage_end=on_date,
        source_type="brokerage_statement",
        source_reference=f"private:statement:{event_id[:8]}",
        source_sha256="a" * 64,
        captured_at=captured,
        approved_at=approved,
        methodology_version="2",
        account_identity_sha256="b" * 64,
        account_mapping_basis="owner_confirmed",
        account_mapping_confidence="exact",
        source_format="pdf",
        parser_version="test-v1",
        source_timezone="America/New_York",
        source_row_count=1,
        cashflow_candidate_count=1,
        source_event_set_sha256="c" * 64,
        manifest_sha256="d" * 64,
    )
    session.add(attestation)
    session.flush()
    event = CashFlowSourceEvent(
        source_event_id=event_id,
        attestation_id=attestation.attestation_id,
        source_locator_kind="page_line",
        source_locator="page:1:line:2",
        source_page=1,
        source_line=2,
        source_row_sha256="e" * 64,
        activity_date=on_date,
        source_amount=Decimal(100),
        source_amount_sign_basis="statement_printed",
        currency="USD",
    )
    decision = CashFlowReconciliationDecision(
        decision_key=decision_key,
        source_event_id=event_id,
        target_transaction_id=transaction_id,
        resolution_kind="statement_supplement",
        classification="external_in",
        signed_external_amount=Decimal(100),
        effective_date=on_date,
        effective_date_basis="source_activity",
        effective_timezone="America/New_York",
        decision_authority="brokerage_statement",
        confidence="exact",
        assumption_code="statement_activity_date",
        methodology_version="2",
        decision_payload_sha256="f" * 64,
        approved_at=approved,
    )
    attestation.source_event_set_sha256 = canonical_source_event_set_sha256((event,))
    decision.decision_payload_sha256 = canonical_decision_payload_sha256(decision)
    session.add_all([event, decision])
    return attestation, decision


def test_statement_supplement_exposes_manual_origin_and_decision_lineage(session):
    account = _account(session, "statement")
    security = _security(session, "statement", "SRC")
    _mark_valued(session, account, security)
    flow_date = date(2026, 1, 5)
    transaction_id = "manual:cashflow:v1:event"
    _transaction(
        session,
        transaction_id=transaction_id,
        account=account,
        on_date=flow_date,
        type_="cash",
        subtype="deposit",
        amount=Decimal(100),
    )
    _approved_statement_decision(
        session,
        account=account,
        transaction_id=transaction_id,
        event_id="1" * 64,
        decision_key="2" * 64,
        on_date=flow_date,
    )
    session.commit()

    entry = build_external_flow_ledger(session, flow_date - timedelta(days=1), flow_date).entries[0]

    assert entry.transaction_origin == "statement_supplement"
    assert entry.source_provider == "brokerage_statement"
    assert entry.source_event_ids == ("1" * 64,)
    assert entry.active_decision_keys == ("2" * 64,)
    assert entry.decision_authorities == ("brokerage_statement",)
    assert entry.assumption_codes == ("statement_activity_date",)
    assessment = _daily_external_cashflow_assessment(
        session, flow_date - timedelta(days=1), flow_date
    )
    assert assessment.cashflows == {flow_date: Decimal(100)}
    assert assessment.entries[0].source_event_ids == ("1" * 64,)


def test_provider_supersedes_statement_supplement_without_double_counting(session):
    account = _account(session, "supersede")
    security = _security(session, "supersede", "SUP")
    _mark_valued(session, account, security)
    flow_date = date(2026, 1, 5)
    manual_id = "manual:cashflow:v1:superseded"
    provider_id = "provider-replacement"
    for transaction_id in (manual_id, provider_id):
        _transaction(
            session,
            transaction_id=transaction_id,
            account=account,
            on_date=flow_date,
            type_="cash",
            subtype="deposit",
            amount=Decimal(100),
        )
    _, old_decision = _approved_statement_decision(
        session,
        account=account,
        transaction_id=manual_id,
        event_id="3" * 64,
        decision_key="4" * 64,
        on_date=flow_date,
    )
    replacement_key = "5" * 64
    replaced_at = datetime(2026, 1, 3, tzinfo=UTC)
    old_decision.superseded_at = replaced_at
    old_decision.superseded_by_decision_key = replacement_key
    replacement = CashFlowReconciliationDecision(
        decision_key=replacement_key,
        source_event_id="3" * 64,
        target_transaction_id=provider_id,
        resolution_kind="provider_supersedes_supplement",
        classification="external_in",
        signed_external_amount=Decimal(100),
        effective_date=flow_date,
        effective_date_basis="source_activity",
        effective_timezone="America/New_York",
        decision_authority="provider",
        confidence="exact",
        assumption_code="provider_record_arrived_later",
        methodology_version="2",
        decision_payload_sha256="6" * 64,
        approved_at=replaced_at,
    )
    replacement.decision_payload_sha256 = canonical_decision_payload_sha256(replacement)
    session.add(replacement)
    session.commit()

    ledger = build_external_flow_ledger(session, flow_date - timedelta(days=1), flow_date)

    assert ledger.net_external_cashflow_in == Decimal(100)
    assert [entry.transaction_id for entry in ledger.entries] == [provider_id]
    assert ledger.entries[0].transaction_origin == "aggregator_transaction"
    assert ledger.entries[0].active_decision_keys == (replacement_key,)


def test_matching_source_events_corroborate_one_target_without_double_counting(session):
    account = _account(session, "corroborated")
    security = _security(session, "corroborated", "COR")
    _mark_valued(session, account, security)
    flow_date = date(2026, 1, 5)
    transaction_id = "manual:cashflow:v1:corroborated"
    _transaction(
        session,
        transaction_id=transaction_id,
        account=account,
        on_date=flow_date,
        type_="cash",
        subtype="deposit",
        amount=Decimal(100),
    )
    for event_id, decision_key in (("9" * 64, "a" * 64), ("b" * 64, "c" * 64)):
        _approved_statement_decision(
            session,
            account=account,
            transaction_id=transaction_id,
            event_id=event_id,
            decision_key=decision_key,
            on_date=flow_date,
        )
    session.commit()

    ledger = build_external_flow_ledger(session, flow_date - timedelta(days=1), flow_date)

    assert ledger.issues == ()
    assert ledger.net_external_cashflow_in == Decimal(100)
    assert len(ledger.entries) == 1
    assert ledger.entries[0].source_event_ids == ("9" * 64, "b" * 64)
    assert ledger.entries[0].active_decision_keys == ("a" * 64, "c" * 64)


def test_provisional_current_decision_fails_structurally(session):
    account = _account(session, "provisional")
    security = _security(session, "provisional", "PROV")
    _mark_valued(session, account, security)
    flow_date = date(2026, 1, 5)
    transaction_id = "manual:cashflow:v1:provisional"
    _transaction(
        session,
        transaction_id=transaction_id,
        account=account,
        on_date=flow_date,
        type_="cash",
        subtype="deposit",
        amount=Decimal(100),
    )
    _, decision = _approved_statement_decision(
        session,
        account=account,
        transaction_id=transaction_id,
        event_id="7" * 64,
        decision_key="8" * 64,
        on_date=flow_date,
    )
    decision.confidence = "provisional"
    decision.decision_payload_sha256 = canonical_decision_payload_sha256(decision)
    session.commit()

    ledger = build_external_flow_ledger(session, flow_date - timedelta(days=1), flow_date)

    assert ledger.entries == ()
    assert [issue.code for issue in ledger.issues] == ["provenance_current_decision_provisional"]


def test_decision_payload_digest_drift_fails_structurally(session):
    account = _account(session, "digest-drift")
    security = _security(session, "digest-drift", "DRIFT")
    _mark_valued(session, account, security)
    flow_date = date(2026, 1, 5)
    transaction_id = "manual:cashflow:v1:digest-drift"
    _transaction(
        session,
        transaction_id=transaction_id,
        account=account,
        on_date=flow_date,
        type_="cash",
        subtype="deposit",
        amount=Decimal(100),
    )
    _, decision = _approved_statement_decision(
        session,
        account=account,
        transaction_id=transaction_id,
        event_id="d" * 64,
        decision_key="e" * 64,
        on_date=flow_date,
    )
    decision.assumption_code = "mutated_without_new_decision"
    session.commit()

    ledger = build_external_flow_ledger(session, flow_date - timedelta(days=1), flow_date)

    assert [issue.code for issue in ledger.issues] == [
        "provenance_decision_payload_digest_mismatch"
    ]


def test_source_date_basis_mismatch_fails_even_with_recomputed_digest(session):
    account = _account(session, "date-basis-drift")
    security = _security(session, "date-basis-drift", "DATE")
    _mark_valued(session, account, security)
    flow_date = date(2026, 1, 5)
    transaction_id = "manual:cashflow:v1:date-basis-drift"
    _transaction(
        session,
        transaction_id=transaction_id,
        account=account,
        on_date=flow_date,
        type_="cash",
        subtype="deposit",
        amount=Decimal(100),
    )
    _, decision = _approved_statement_decision(
        session,
        account=account,
        transaction_id=transaction_id,
        event_id="f" * 64,
        decision_key="0" * 64,
        on_date=flow_date,
    )
    decision.effective_date = flow_date + timedelta(days=1)
    decision.decision_payload_sha256 = canonical_decision_payload_sha256(decision)
    session.commit()

    ledger = build_external_flow_ledger(
        session, flow_date - timedelta(days=1), flow_date + timedelta(days=1)
    )

    assert ledger.entries == ()
    assert [issue.code for issue in ledger.issues] == ["provenance_effective_date_basis_mismatch"]


def test_ledger_uses_end_of_day_window_boundary(session):
    account = _account(session, "boundary")
    security = _security(session, "boundary", "ABC")
    _mark_valued(session, account, security)
    _transaction(
        session,
        transaction_id="opening-day",
        account=account,
        on_date=date(2026, 1, 1),
        type_="cash",
        subtype="deposit",
        amount=Decimal(100),
    )
    _transaction(
        session,
        transaction_id="after-opening",
        account=account,
        on_date=date(2026, 1, 2),
        type_="cash",
        subtype="deposit",
        amount=Decimal(250),
    )
    session.commit()

    ledger = build_external_flow_ledger(session, date(2026, 1, 1), date(2026, 1, 2))

    assert [entry.transaction_id for entry in ledger.entries] == ["after-opening"]
    assert ledger.net_external_cashflow_in == Decimal(250)


def test_ledger_excludes_flows_from_accounts_absent_from_value_series(session):
    valued_account = _account(session, "valued")
    flow_only_account = _account(session, "flow-only")
    security = _security(session, "valued", "VALU")
    _mark_valued(session, valued_account, security)
    for transaction_id, account, amount in (
        ("valued-flow", valued_account, Decimal(100)),
        ("flow-without-value", flow_only_account, Decimal(500)),
    ):
        _transaction(
            session,
            transaction_id=transaction_id,
            account=account,
            on_date=date(2026, 1, 2),
            type_="cash",
            subtype="deposit",
            amount=amount,
        )
    session.commit()

    ledger = build_external_flow_ledger(session, date(2026, 1, 1), date(2026, 1, 2))

    assert [entry.transaction_id for entry in ledger.entries] == ["valued-flow"]
    assert ledger.account_ids == frozenset({valued_account.account_id})
    assert ledger.net_external_cashflow_in == Decimal(100)


def test_transfer_matching_normalizes_ticker_and_split_adjusted_quantity(session):
    outgoing_account = _account(session, "out", "plaid")
    incoming_account = _account(session, "in", "snaptrade")
    outgoing_security = _security(session, "out", "ACME")
    incoming_security = _security(session, "in", " acme ")
    _mark_valued(session, outgoing_account, outgoing_security)
    _mark_valued(session, incoming_account, incoming_security)
    transfer_date = date(2026, 1, 5)
    session.add(
        StockSplit(
            security_id=outgoing_security.security_id,
            split_date=date(2026, 2, 1),
            ratio=Decimal(2),
        )
    )
    # Two pre-split shares leaving are four shares in the adjusted units used
    # by prices; they match the receiving provider's four adjusted shares.
    _transaction(
        session,
        transaction_id="plain-zero-dollar-out",
        account=outgoing_account,
        on_date=transfer_date,
        type_="transfer",
        subtype="transfer",
        amount=Decimal(0),
        quantity=Decimal(-2),
        security=outgoing_security,
    )
    _transaction(
        session,
        transaction_id="cash-share-in",
        account=incoming_account,
        on_date=transfer_date,
        type_="cash",
        subtype="external_asset_transfer_in",
        amount=Decimal(0),
        quantity=Decimal(4),
        security=incoming_security,
    )
    session.commit()

    ledger = build_external_flow_ledger(session, date(2026, 1, 4), transfer_date)

    assert ledger.entries == ()
    assert ledger.issues == ()


def test_owner_override_and_name_rule_apply_to_share_components(session):
    account = _account(session, "rules")
    security = _security(session, "rules", "RULE")
    _mark_valued(session, account, security)
    transfer_date = date(2026, 1, 5)
    _transaction(
        session,
        transaction_id="owner-internal",
        account=account,
        on_date=transfer_date,
        type_="cash",
        subtype="external_asset_transfer_in",
        amount=Decimal(0),
        quantity=Decimal(2),
        security=security,
    )
    _transaction(
        session,
        transaction_id="name-internal",
        account=account,
        on_date=transfer_date,
        type_="transfer",
        subtype="transfer",
        amount=Decimal(0),
        quantity=Decimal(3),
        security=security,
        name="Dividend reinvestment purchase",
    )
    session.flush()
    session.add(
        TransactionOverride(
            plaid_investment_transaction_id="owner-internal",
            classification="internal",
            notes="owner-approved test rule",
        )
    )
    session.commit()

    ledger = build_external_flow_ledger(session, date(2026, 1, 4), transfer_date)

    assert ledger.entries == ()
    assert ledger.issues == ()


def test_unpriceable_share_transfer_fails_closed_with_reason(client, session):
    account = _account(session, "unpriced")
    security = _security(session, "unpriced", "NOPX")
    _mark_valued(session, account, security)
    transfer_date = date(2026, 1, 5)
    _transaction(
        session,
        transaction_id="unpriced-transfer",
        account=account,
        on_date=transfer_date,
        type_="cash",
        subtype="external_asset_transfer_in",
        amount=Decimal(0),
        quantity=Decimal(2),
        security=security,
    )
    session.commit()

    ledger = build_external_flow_ledger(session, date(2026, 1, 4), transfer_date)

    assert ledger.entries == ()
    assert [issue.code for issue in ledger.issues] == ["share_transfer_price_unavailable"]
    assert ledger.issues[0].component_transaction_ids == ("unpriced-transfer",)
    with pytest.raises(IncompleteExternalFlowLedgerError, match="share_transfer_price_unavailable"):
        _ = ledger.daily_external_cashflows
    assessment = _daily_external_cashflow_assessment(session, date(2026, 1, 4), transfer_date)
    assert assessment.cashflows == {}
    assert assessment.calculation_reason_codes == ("external_share_movement_price_unavailable",)
    assert _daily_external_cashflows(session, date(2026, 1, 4), transfer_date) == {}

    response = client.get(
        "/api/v1/cash-flows",
        params={"start_date": "2026-01-04", "end_date": "2026-01-05"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["is_complete"] is False
    assert payload["net_external_cashflow_in"] is None
    assert payload["issues"][0]["code"] == "share_transfer_price_unavailable"


@pytest.mark.parametrize(
    ("source", "basis"),
    [
        (PriceSource.UNKNOWN.value, PriceAdjustmentBasis.UNKNOWN.value),
        (PriceSource.STOOQ.value, PriceAdjustmentBasis.SPLIT_ADJUSTED.value),
        (PriceSource.YFINANCE.value, PriceAdjustmentBasis.RAW_UNADJUSTED.value),
    ],
)
def test_share_transfer_rejects_ineligible_price_provenance(session, source, basis):
    account = _account(session, f"ineligible-{source}-{basis}")
    security = _security(session, f"ineligible-{source}-{basis}", "BADPX")
    _mark_valued(session, account, security)
    transfer_date = date(2026, 1, 5)
    _transaction(
        session,
        transaction_id=f"transfer-{source}-{basis}",
        account=account,
        on_date=transfer_date,
        type_="cash",
        subtype="external_asset_transfer_in",
        amount=Decimal(0),
        quantity=Decimal(2),
        security=security,
    )
    session.add(
        Price(
            security_id=security.security_id,
            date=transfer_date,
            close=Decimal(100),
            source=source,
            adjustment_basis=basis,
        )
    )
    session.commit()

    ledger = build_external_flow_ledger(session, date(2026, 1, 4), transfer_date)

    assert ledger.entries == ()
    assert [issue.code for issue in ledger.issues] == ["share_transfer_price_unavailable"]


def test_share_transfer_accepts_eligible_price_provenance(session):
    account = _account(session, "eligible")
    security = _security(session, "eligible", "GOODPX")
    _mark_valued(session, account, security)
    transfer_date = date(2026, 1, 5)
    _transaction(
        session,
        transaction_id="eligible-transfer",
        account=account,
        on_date=transfer_date,
        type_="cash",
        subtype="external_asset_transfer_in",
        amount=Decimal(0),
        quantity=Decimal(2),
        security=security,
    )
    session.add(
        Price(
            security_id=security.security_id,
            date=transfer_date,
            close=Decimal(100),
            source=PriceSource.YFINANCE.value,
            adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
        )
    )
    session.commit()

    ledger = build_external_flow_ledger(session, date(2026, 1, 4), transfer_date)

    assert ledger.issues == ()
    assert ledger.net_external_cashflow_in == Decimal(200)


def test_share_transfer_missing_security_is_structured_issue(session):
    account = _account(session, "missing-security")
    marker = _security(session, "missing-security-marker", "MARK")
    _mark_valued(session, account, marker)
    transfer_date = date(2026, 1, 5)
    _transaction(
        session,
        transaction_id="missing-security-transfer",
        account=account,
        on_date=transfer_date,
        type_="cash",
        subtype="external_asset_transfer_in",
        amount=Decimal(0),
        quantity=Decimal(2),
    )
    session.commit()

    ledger = build_external_flow_ledger(session, date(2026, 1, 4), transfer_date)

    assert [issue.code for issue in ledger.issues] == ["share_transfer_missing_security"]


def test_share_transfer_missing_ticker_is_structured_issue(session):
    account = _account(session, "missing-ticker")
    security = _security(session, "missing-ticker", "")
    _mark_valued(session, account, security)
    transfer_date = date(2026, 1, 5)
    _transaction(
        session,
        transaction_id="missing-ticker-transfer",
        account=account,
        on_date=transfer_date,
        type_="cash",
        subtype="external_asset_transfer_in",
        amount=Decimal(0),
        quantity=Decimal(2),
        security=security,
    )
    session.commit()

    ledger = build_external_flow_ledger(session, date(2026, 1, 4), transfer_date)

    assert [issue.code for issue in ledger.issues] == ["share_transfer_missing_ticker"]
