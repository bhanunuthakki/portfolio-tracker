from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from portfolio_tracker import plaid_client, snaptrade_client
from portfolio_tracker.provider_delivery import (
    ProviderDeliveryError,
    ProviderDeliveryIncompleteError,
    ProviderPayloadError,
    UnsupportedProviderParserError,
    assert_supported_provider_parser,
)
from portfolio_tracker.snaptrade_client import SnapTradeUserCredentials


class _PlaidObject:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, object]:
        return self._payload


def _plaid_transaction(tx_id: str, tx_date: str = "2025-01-02") -> _PlaidObject:
    return _PlaidObject(
        {
            "investment_transaction_id": tx_id,
            "account_id": "account-1",
            "date": tx_date,
            "amount": "10.00",
            "type": "cash",
            "subtype": "deposit",
        }
    )


class _PlaidApi:
    def __init__(self, pages: list[tuple[list[_PlaidObject], int]]) -> None:
        self._pages = iter(pages)
        self.offsets: list[int] = []

    def investments_transactions_get(self, request: object) -> object:
        request_dict = request.to_dict()
        self.offsets.append(int(request_dict["options"]["offset"]))
        rows, total = next(self._pages)
        return SimpleNamespace(
            accounts=[],
            securities=[],
            investment_transactions=rows,
            total_investment_transactions=total,
        )


def test_plaid_transactions_paginate_and_emit_complete_delivery_metadata(monkeypatch):
    api = _PlaidApi(
        [
            ([_plaid_transaction("tx-1"), _plaid_transaction("tx-2")], 3),
            ([_plaid_transaction("tx-3")], 3),
        ]
    )
    monkeypatch.setattr(plaid_client, "get_client", lambda: api)

    response = plaid_client.get_investment_transactions(
        "opaque-access-token", date(2025, 1, 1), date(2025, 1, 31)
    )

    assert api.offsets == [0, 2]
    assert response.total_transactions == 3
    assert [row.plaid_investment_transaction_id for row in response.transactions] == [
        "tx-1",
        "tx-2",
        "tx-3",
    ]
    assert response.delivery.provider == "plaid"
    assert response.delivery.page_count == 2
    assert response.delivery.provider_reported_total == 3
    assert response.delivery.fetched_count == 3
    assert response.delivery.unique_record_count == 3
    assert len(response.delivery.record_set_sha256) == 64
    assert response.delivery.is_complete is True


def test_plaid_transactions_fail_when_provider_stops_before_reported_total(monkeypatch):
    api = _PlaidApi([([_plaid_transaction("tx-1")], 2), ([], 2)])
    monkeypatch.setattr(plaid_client, "get_client", lambda: api)

    with pytest.raises(ProviderDeliveryIncompleteError):
        plaid_client.get_investment_transactions(
            "opaque-access-token", date(2025, 1, 1), date(2025, 1, 31)
        )


def test_plaid_transactions_fail_on_duplicate_provider_record(monkeypatch):
    api = _PlaidApi(
        [
            ([_plaid_transaction("tx-1")], 2),
            ([_plaid_transaction("tx-1")], 2),
        ]
    )
    monkeypatch.setattr(plaid_client, "get_client", lambda: api)

    with pytest.raises(ProviderDeliveryIncompleteError):
        plaid_client.get_investment_transactions(
            "opaque-access-token", date(2025, 1, 1), date(2025, 1, 31)
        )


def test_plaid_transactions_fail_instead_of_coercing_missing_record_id(monkeypatch):
    malformed = _plaid_transaction("sensitive-provider-id")
    malformed._payload["investment_transaction_id"] = None
    api = _PlaidApi([([malformed], 1)])
    monkeypatch.setattr(plaid_client, "get_client", lambda: api)

    with pytest.raises(ProviderPayloadError) as exc_info:
        plaid_client.get_investment_transactions(
            "opaque-access-token", date(2025, 1, 1), date(2025, 1, 31)
        )
    assert "sensitive-provider-id" not in str(exc_info.value)


def test_provider_request_failure_does_not_expose_plaid_access_token(monkeypatch):
    class _FailingPlaidApi:
        def investments_transactions_get(self, request: object) -> object:
            raise RuntimeError(f"request failed: {request!r} opaque-access-token")

    monkeypatch.setattr(plaid_client, "get_client", lambda: _FailingPlaidApi())

    with pytest.raises(ProviderDeliveryError) as exc_info:
        plaid_client.get_investment_transactions(
            "opaque-access-token", date(2025, 1, 1), date(2025, 1, 31)
        )
    assert "opaque-access-token" not in str(exc_info.value)


class _SnapTradeAccountInformation:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self._pages = iter(pages)
        self.offsets: list[int] = []
        self.limits: list[int] = []

    def get_account_activities(self, **kwargs: object) -> object:
        self.offsets.append(int(kwargs["offset"]))
        self.limits.append(int(kwargs["limit"]))
        return SimpleNamespace(body=next(self._pages))


def _snaptrade_activity(tx_id: str, tx_date: str = "2025-01-02") -> dict[str, object]:
    return {
        "id": tx_id,
        "trade_date": tx_date,
        "type": "DEPOSIT",
        "amount": "10.00",
        "currency": {"code": "USD"},
    }


def _snaptrade_page(
    rows: list[dict[str, object]], total: int, *, offset: int = 0
) -> dict[str, object]:
    return {
        "data": rows,
        "pagination": {"offset": offset, "limit": 1000, "total": total},
    }


def test_snaptrade_activities_paginate_and_emit_complete_delivery_metadata(monkeypatch):
    account_information = _SnapTradeAccountInformation(
        [
            _snaptrade_page([_snaptrade_activity("activity-1")], 2),
            _snaptrade_page([_snaptrade_activity("activity-2")], 2, offset=1),
        ]
    )
    monkeypatch.setattr(
        snaptrade_client,
        "get_client",
        lambda: SimpleNamespace(account_information=account_information),
    )

    response = snaptrade_client.get_account_activities(
        SnapTradeUserCredentials(user_id="user", user_secret="dummy-value"),
        "account-1",
        date(2025, 1, 1),
        date(2025, 1, 31),
    )

    assert account_information.offsets == [0, 1]
    assert account_information.limits == [1000, 1000]
    assert len(response.transactions) == 2
    assert response.total_transactions == 2
    assert response.delivery.provider == "snaptrade"
    assert response.delivery.page_count == 2
    assert response.delivery.provider_reported_total == 2
    assert response.delivery.fetched_count == 2
    assert response.delivery.unique_record_count == 2
    assert response.delivery.is_complete is True


def test_snaptrade_activities_fail_when_pagination_metadata_is_missing(monkeypatch):
    account_information = _SnapTradeAccountInformation(
        [{"data": [_snaptrade_activity("activity-1")]}]
    )
    monkeypatch.setattr(
        snaptrade_client,
        "get_client",
        lambda: SimpleNamespace(account_information=account_information),
    )

    with pytest.raises(ProviderDeliveryIncompleteError):
        snaptrade_client.get_account_activities(
            SnapTradeUserCredentials(user_id="user", user_secret="dummy-value"),
            "account-1",
            date(2025, 1, 1),
            date(2025, 1, 31),
        )


def test_snaptrade_activities_fail_instead_of_skipping_malformed_row(monkeypatch):
    malformed = _snaptrade_activity("activity-without-date")
    malformed.pop("trade_date")
    account_information = _SnapTradeAccountInformation([_snaptrade_page([malformed], 1)])
    monkeypatch.setattr(
        snaptrade_client,
        "get_client",
        lambda: SimpleNamespace(account_information=account_information),
    )

    with pytest.raises(ProviderPayloadError) as exc_info:
        snaptrade_client.get_account_activities(
            SnapTradeUserCredentials(user_id="user", user_secret="dummy-value"),
            "account-1",
            date(2025, 1, 1),
            date(2025, 1, 31),
        )
    assert "activity-without-date" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", None),
        ("type", None),
    ],
)
def test_snaptrade_activities_fail_closed_on_required_or_unknown_fields(
    monkeypatch, field: str, value: object
):
    malformed = _snaptrade_activity("sensitive-provider-id")
    malformed[field] = value
    account_information = _SnapTradeAccountInformation([_snaptrade_page([malformed], 1)])
    monkeypatch.setattr(
        snaptrade_client,
        "get_client",
        lambda: SimpleNamespace(account_information=account_information),
    )

    with pytest.raises(ProviderPayloadError) as exc_info:
        snaptrade_client.get_account_activities(
            SnapTradeUserCredentials(user_id="user", user_secret="dummy-value"),
            "account-1",
            date(2025, 1, 1),
            date(2025, 1, 31),
        )
    assert "sensitive-provider-id" not in str(exc_info.value)


def test_snaptrade_unknown_activity_type_is_preserved_without_becoming_cash(monkeypatch):
    activity = _snaptrade_activity("activity-1")
    activity["type"] = "BROKER_SPECIFIC_EVENT"
    account_information = _SnapTradeAccountInformation([_snaptrade_page([activity], 1)])
    monkeypatch.setattr(
        snaptrade_client,
        "get_client",
        lambda: SimpleNamespace(account_information=account_information),
    )

    response = snaptrade_client.get_account_activities(
        SnapTradeUserCredentials(user_id="user", user_secret="dummy-value"),
        "account-1",
        date(2025, 1, 1),
        date(2025, 1, 31),
    )

    assert response.transactions[0].type == "broker_specific_event"
    assert response.transactions[0].subtype == "broker_specific_event"


def test_snaptrade_nullable_amount_preserves_valid_in_kind_transfer(monkeypatch):
    activity = _snaptrade_activity("activity-1")
    activity.update(
        {
            "type": "EXTERNAL_ASSET_TRANSFER_IN",
            "amount": None,
            "units": "5",
        }
    )
    account_information = _SnapTradeAccountInformation([_snaptrade_page([activity], 1)])
    monkeypatch.setattr(
        snaptrade_client,
        "get_client",
        lambda: SimpleNamespace(account_information=account_information),
    )

    response = snaptrade_client.get_account_activities(
        SnapTradeUserCredentials(user_id="user", user_secret="dummy-value"),
        "account-1",
        date(2025, 1, 1),
        date(2025, 1, 31),
    )

    transaction = response.transactions[0]
    assert transaction.amount == Decimal(0)
    assert transaction.quantity == Decimal(5)
    assert transaction.type == "cash"
    assert transaction.subtype == "external_asset_transfer_in"


def test_provider_request_failure_does_not_expose_snaptrade_secret(monkeypatch):
    class _FailingAccountInformation:
        def get_account_activities(self, **_kwargs: object) -> object:
            raise RuntimeError("https://api.example?userSecret=opaque-secret")

    monkeypatch.setattr(
        snaptrade_client,
        "get_client",
        lambda: SimpleNamespace(account_information=_FailingAccountInformation()),
    )

    with pytest.raises(ProviderDeliveryError) as exc_info:
        snaptrade_client.get_account_activities(
            SnapTradeUserCredentials(user_id="user", user_secret="dummy-value"),
            "account-1",
            date(2025, 1, 1),
            date(2025, 1, 31),
        )
    assert "opaque-secret" not in str(exc_info.value)


def test_provider_parser_allowlist_is_fail_closed():
    assert_supported_provider_parser("plaid_investment_transactions_api", "plaid_investment_tx.v1")

    with pytest.raises(UnsupportedProviderParserError):
        assert_supported_provider_parser("unrecognized_provider_export", "unrecognized.v1")


def test_plaid_account_preserves_direct_broker_total_and_available_cash():
    account = plaid_client._account_from_plaid(
        _PlaidObject(
            {
                "account_id": "account-1",
                "name": "Brokerage",
                "type": "investment",
                "balances": {
                    "current": "1250.50",
                    "available": "75.25",
                    "iso_currency_code": "USD",
                },
            }
        )
    )

    assert account.provider_total_value == Decimal("1250.50")
    assert account.provider_available_cash == Decimal("75.25")


def test_snaptrade_account_preserves_broker_total_without_inferring_cash():
    account = snaptrade_client._account_from_snaptrade(
        {
            "id": "account-1",
            "name": "Brokerage",
            "balance": {"total": {"amount": "1250.50", "currency": "USD"}},
        }
    )

    assert account.provider_total_value == Decimal("1250.50")
    assert account.provider_available_cash is None


def test_snaptrade_cash_uses_only_direct_same_currency_balances():
    cash = snaptrade_client._snaptrade_available_cash(
        [
            {"currency": {"code": "USD"}, "cash": "75.25"},
            {"currency": {"code": "CAD"}, "cash": "20.00"},
        ],
        "USD",
    )

    assert cash == Decimal("75.25")
