"""Parity tests for the deprecated canonical cash-flow audit adapter."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    CashFlowSourceAttestation,
    InvestmentTransaction,
    Item,
    TransactionOverride,
)
from portfolio_tracker.services.cashflow_source_coverage import canonical_source_event_set_sha256

_START = date(2026, 1, 1)
_FLOW_DATE = date(2026, 1, 2)


def _seed_account(session) -> Account:
    item = Item(
        source="plaid",
        plaid_item_id="cashflow-audit-item",
        institution_name="Synthetic Broker",
        is_data_active=True,
    )
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id,
        plaid_account_id="cashflow-audit-account",
        name="Brokerage",
        type="investment",
        subtype="brokerage",
        currency="USD",
    )
    session.add(account)
    session.flush()
    return account


def _add_transaction(
    session,
    account: Account,
    *,
    transaction_id: str,
    subtype: str,
    amount: str,
) -> None:
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id=transaction_id,
            account_id=account.account_id,
            date=_FLOW_DATE,
            name=transaction_id,
            quantity=Decimal(0),
            amount=Decimal(amount),
            type="cash",
            subtype=subtype,
            currency="USD",
        )
    )


def _attest_complete_window(session, account: Account) -> None:
    session.add(
        CashFlowSourceAttestation(
            attestation_key="cashflow-audit-statement",
            account_id=account.account_id,
            coverage_start=_FLOW_DATE,
            coverage_end=_FLOW_DATE,
            source_type="brokerage_statement",
            source_reference="synthetic:cashflow-audit-statement",
            source_sha256="a" * 64,
            captured_at=datetime(2026, 1, 3, tzinfo=UTC),
            approved_at=datetime(2026, 1, 4, tzinfo=UTC),
            methodology_version="2",
            account_identity_sha256="b" * 64,
            account_mapping_basis="owner_confirmed",
            account_mapping_confidence="exact",
            source_format="synthetic",
            parser_version="test-v1",
            source_timezone="UTC",
            source_row_count=0,
            cashflow_candidate_count=0,
            source_event_set_sha256=canonical_source_event_set_sha256(()),
            manifest_sha256="c" * 64,
        )
    )


def test_cashflow_audit_uses_canonical_override_and_matches_v1(client, session):
    account = _seed_account(session)
    # This is the retained statement-supplement row after a provider row has
    # superseded it. The owner override prevents the retained evidence row from
    # becoming a second economic inflow.
    _add_transaction(
        session,
        account,
        transaction_id="statement-supplement-retained",
        subtype="transfer",
        amount="-100",
    )
    session.add(
        TransactionOverride(
            plaid_investment_transaction_id="statement-supplement-retained",
            classification="internal",
            notes="superseded by provider transaction",
        )
    )
    _add_transaction(
        session,
        account,
        transaction_id="provider-deposit",
        subtype="deposit",
        amount="100",
    )
    _attest_complete_window(session, account)
    session.commit()

    params = {"start_date": _START.isoformat(), "end_date": _FLOW_DATE.isoformat()}
    legacy_response = client.get("/api/portfolio/cashflow-audit", params=params)
    v1_response = client.get(
        "/api/v1/cash-flows",
        params={**params, "include_internal": True},
    )

    assert legacy_response.status_code == 200
    assert v1_response.status_code == 200
    legacy = legacy_response.json()
    v1 = v1_response.json()
    assert Decimal(legacy["net_external_cashflow_in"]) == Decimal("100")
    assert Decimal(legacy["net_external_cashflow_in"]) == Decimal(v1["net_external_cashflow_in"])
    groups = {(row["type"], row["subtype"]): row for row in legacy["groups"]}
    assert groups[("cash", "transfer")]["classified_as_external_cashflow"] is False
    assert groups[("cash", "deposit")]["classified_as_external_cashflow"] is True


def test_cashflow_audit_fails_closed_when_source_coverage_is_missing(client, session):
    account = _seed_account(session)
    _add_transaction(
        session,
        account,
        transaction_id="unattested-provider-deposit",
        subtype="deposit",
        amount="100",
    )
    session.commit()

    response = client.get(
        "/api/portfolio/cashflow-audit",
        params={"start_date": _START.isoformat(), "end_date": _FLOW_DATE.isoformat()},
    )

    assert response.status_code == 409
    assert "net_external_cashflow_in" not in response.json()
