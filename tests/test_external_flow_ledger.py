from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portfolio_tracker.models import (
    Account,
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
