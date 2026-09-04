from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select

from portfolio_tracker import plaid_client
from portfolio_tracker.jobs import snapshot
from portfolio_tracker.models import Account, AccountValuationObservation, Item
from portfolio_tracker.plaid_client import (
    HoldingsResponse,
    PlaidAccount,
    PlaidHolding,
    PlaidSecurity,
)


def _response() -> HoldingsResponse:
    return HoldingsResponse(
        accounts=[
            PlaidAccount(
                plaid_account_id="brokerage-1",
                name="Brokerage",
                type="investment",
                currency="USD",
                provider_balance_currency="USD",
                provider_total_value=Decimal("1234.56"),
                provider_available_cash=Decimal("34.56"),
                provider_balance_as_of=datetime(2026, 9, 3, 20, tzinfo=UTC),
            ),
            PlaidAccount(
                plaid_account_id="empty-1",
                name="Empty IRA",
                type="investment",
                currency="USD",
                provider_balance_currency="USD",
                provider_total_value=Decimal("0"),
                provider_available_cash=Decimal("0"),
                provider_balance_as_of=datetime(2026, 9, 3, 20, tzinfo=UTC),
            ),
            PlaidAccount(
                plaid_account_id="missing-total-1",
                name="Missing Total",
                type="investment",
                currency="USD",
                provider_balance_currency="USD",
                provider_total_value=None,
            ),
        ],
        securities=[
            PlaidSecurity(plaid_security_id="security-1", ticker="ONE"),
            PlaidSecurity(plaid_security_id="security-2", ticker="TWO"),
        ],
        holdings=[
            PlaidHolding(
                plaid_account_id="brokerage-1",
                plaid_security_id="security-1",
                quantity=Decimal("1"),
                institution_price=Decimal("900"),
                institution_value=Decimal("900"),
            ),
            # A holding cannot manufacture an account total when Plaid did
            # not report balances.current for the account.
            PlaidHolding(
                plaid_account_id="missing-total-1",
                plaid_security_id="security-2",
                quantity=Decimal("1"),
                institution_price=Decimal("500"),
                institution_value=Decimal("500"),
            ),
        ],
        item_id="plaid-item-1",
        institution_id="institution-1",
    )


def test_plaid_snapshot_records_direct_account_totals_and_explicit_empty_account(
    session, monkeypatch
):
    item = Item(
        source="plaid",
        plaid_item_id="plaid-item-1",
        plaid_access_token_encrypted="encrypted-test-token",
    )
    session.add(item)
    session.flush()
    monkeypatch.setattr(snapshot, "decrypt_token", lambda _encrypted: "test-token")
    monkeypatch.setattr(plaid_client, "get_holdings", lambda _token: _response())
    snapshot_date = date(2026, 9, 3)

    assert snapshot._snapshot_item(session, item, snapshot_date) == 2
    accounts = {
        account.plaid_account_id: account for account in session.scalars(select(Account)).all()
    }
    valuations = {
        row.account_id: row for row in session.scalars(select(AccountValuationObservation)).all()
    }
    brokerage = valuations[accounts["brokerage-1"].account_id]
    assert brokerage.total_value == Decimal("1234.56")
    assert brokerage.cash_value == Decimal("34.56")
    assert brokerage.total_value != Decimal("900")
    assert brokerage.is_complete is True
    assert brokerage.is_empty is False
    assert brokerage.source_provider == "plaid"
    assert brokerage.source_record_id == "brokerage-1"
    assert len(brokerage.source_payload_sha256 or "") == 64

    empty = valuations[accounts["empty-1"].account_id]
    assert empty.total_value == Decimal("0")
    assert empty.cash_value == Decimal("0")
    assert empty.is_complete is True
    assert empty.is_empty is True

    assert accounts["missing-total-1"].account_id not in valuations
    assert session.scalar(select(func.count()).select_from(AccountValuationObservation)) == 2


def test_plaid_total_without_provider_as_of_is_non_certifying(session, monkeypatch) -> None:
    response = _response()
    response.accounts[0] = response.accounts[0].model_copy(update={"provider_balance_as_of": None})
    item = Item(
        source="plaid",
        plaid_item_id="plaid-item-1",
        plaid_access_token_encrypted="encrypted-test-token",
    )
    session.add(item)
    session.flush()
    monkeypatch.setattr(snapshot, "decrypt_token", lambda _encrypted: "test-token")
    monkeypatch.setattr(plaid_client, "get_holdings", lambda _token: response)

    snapshot._snapshot_item(session, item, date(2026, 9, 3))

    account = session.scalar(select(Account).where(Account.plaid_account_id == "brokerage-1"))
    assert account is not None
    valuation = session.scalar(
        select(AccountValuationObservation).where(
            AccountValuationObservation.account_id == account.account_id
        )
    )
    assert valuation is not None
    assert valuation.is_complete is False
    assert valuation.is_empty is False
    assert valuation.as_of_at is None
    assert "cached_as_fetched_no_provider_as_of" in valuation.source_reference


def test_plaid_total_without_provider_iso_currency_is_not_certified(session, monkeypatch) -> None:
    response = _response()
    response.accounts[0] = response.accounts[0].model_copy(
        update={"provider_balance_currency": None}
    )
    item = Item(
        source="plaid",
        plaid_item_id="plaid-item-1",
        plaid_access_token_encrypted="encrypted-test-token",
    )
    session.add(item)
    session.flush()
    monkeypatch.setattr(snapshot, "decrypt_token", lambda _encrypted: "test-token")
    monkeypatch.setattr(plaid_client, "get_holdings", lambda _token: response)

    snapshot._snapshot_item(session, item, date(2026, 9, 3))

    account = session.scalar(select(Account).where(Account.plaid_account_id == "brokerage-1"))
    assert account is not None
    assert (
        session.scalar(
            select(AccountValuationObservation).where(
                AccountValuationObservation.account_id == account.account_id
            )
        )
        is None
    )
