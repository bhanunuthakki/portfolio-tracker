from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portfolio_tracker import plaid_client
from portfolio_tracker.jobs import backfill
from portfolio_tracker.models import Item
from portfolio_tracker.plaid_client import (
    InvestmentsTransactionsResponse,
    PlaidAccount,
    PlaidInvestmentTransaction,
)
from portfolio_tracker.provider_delivery import (
    ProviderPayloadError,
    build_provider_delivery_metadata,
)


def _transaction(account_id: str) -> PlaidInvestmentTransaction:
    return PlaidInvestmentTransaction(
        plaid_investment_transaction_id="provider-transaction-1",
        plaid_account_id=account_id,
        date=date(2025, 1, 2),
        amount=Decimal("25.00"),
        type="cash",
        subtype="deposit",
    )


def _response(
    accounts: list[PlaidAccount],
    transactions: list[PlaidInvestmentTransaction],
) -> InvestmentsTransactionsResponse:
    delivery = build_provider_delivery_metadata(
        provider="plaid",
        source_format="plaid_investment_transactions_api",
        parser_version="plaid_investment_tx.v1",
        requested_start_date=date(2025, 1, 1),
        requested_end_date=date(2025, 1, 31),
        page_count=1,
        provider_reported_total=len(transactions),
        record_ids=[row.plaid_investment_transaction_id for row in transactions],
        normalized_records=[row.model_dump(mode="json") for row in transactions],
    )
    return InvestmentsTransactionsResponse(
        accounts=accounts,
        securities=[],
        transactions=transactions,
        total_transactions=len(transactions),
        delivery=delivery,
    )


def _item(session) -> Item:
    item = Item(
        source="plaid",
        plaid_item_id="item-1",
        plaid_access_token_encrypted="opaque-encrypted-token",
    )
    session.add(item)
    session.flush()
    return item


def test_backfill_exposes_account_slices_of_count_verified_delivery(session, monkeypatch):
    item = _item(session)
    response = _response(
        [
            PlaidAccount(plaid_account_id="account-1", name="Brokerage", type="investment"),
            PlaidAccount(plaid_account_id="account-2", name="IRA", type="investment"),
        ],
        [_transaction("account-1")],
    )
    monkeypatch.setattr(backfill, "decrypt_token", lambda _value: "opaque-access-token")
    monkeypatch.setattr(plaid_client, "get_investment_transactions", lambda *_args: response)

    receipt = backfill._backfill_item_with_delivery_receipt(
        session, item, date(2025, 1, 1), date(2025, 1, 31)
    )

    assert receipt is not None
    assert receipt.item_id == item.item_id
    assert receipt.rows_written == 1
    assert receipt.delivery.fetched_count == 1
    assert [row.source_row_count for row in receipt.accounts] == [1, 0]
    assert all(len(row.record_set_sha256) == 64 for row in receipt.accounts)


def test_backfill_fails_instead_of_skipping_transaction_with_unmapped_account(session, monkeypatch):
    item = _item(session)
    response = _response(
        [PlaidAccount(plaid_account_id="account-1", name="Checking", type="depository")],
        [_transaction("account-1")],
    )
    monkeypatch.setattr(backfill, "decrypt_token", lambda _value: "opaque-access-token")
    monkeypatch.setattr(plaid_client, "get_investment_transactions", lambda *_args: response)

    with pytest.raises(ProviderPayloadError):
        backfill._backfill_item_with_delivery_receipt(
            session, item, date(2025, 1, 1), date(2025, 1, 31)
        )
