from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from portfolio_tracker import plaid_client, snaptrade_client
from portfolio_tracker.api.routes import snaptrade as snaptrade_route
from portfolio_tracker.provider_delivery import ProviderDeliveryError, ProviderPayloadError
from portfolio_tracker.snaptrade_client import (
    SnapTradeBrokerageAuthorization,
    SnapTradeUserCredentials,
)


class _PlaidObject:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, object]:
        return self._payload


def test_plaid_account_retains_direct_balance_last_updated_datetime() -> None:
    account = plaid_client._account_from_plaid(
        _PlaidObject(
            {
                "account_id": "plaid-account-1",
                "name": "Brokerage",
                "type": "investment",
                "balances": {
                    "current": "123.45",
                    "available": "23.45",
                    "iso_currency_code": "USD",
                    "last_updated_datetime": "2026-09-02T18:30:00Z",
                },
            }
        )
    )

    assert account.provider_balance_as_of == datetime(2026, 9, 2, 18, 30, tzinfo=UTC)
    assert account.provider_balance_currency == "USD"


def test_plaid_account_normalizes_provider_values_at_storage_precision() -> None:
    account = plaid_client._account_from_plaid(
        _PlaidObject(
            {
                "account_id": "private-account-id",
                "name": "Brokerage",
                "type": "investment",
                "balances": {
                    "current": 4572868.000000001,
                    "available": 29999.999999999996,
                    "iso_currency_code": "usd",
                    "last_updated_datetime": datetime(2026, 9, 2, 18, 30, tzinfo=UTC),
                },
            }
        )
    )

    assert account.provider_total_value == Decimal("4572868.000000")
    assert account.provider_available_cash == Decimal("30000.000000")
    assert account.provider_balance_currency == "USD"


def test_plaid_account_quantizes_genuine_extra_precision_half_even() -> None:
    account = plaid_client._account_from_plaid(
        _PlaidObject(
            {
                "account_id": "private-account-id",
                "name": "Brokerage",
                "type": "investment",
                "balances": {
                    "current": "123.1234567",
                    "available": "1.1234565",
                    "iso_currency_code": "USD",
                },
            }
        )
    )

    assert account.provider_total_value == Decimal("123.123457")
    assert account.provider_available_cash == Decimal("1.123456")


def test_plaid_balance_as_of_rejects_timestamp_without_timezone() -> None:
    with pytest.raises(
        plaid_client.PlaidAccountFieldNormalizationError,
        match=r"balances\.last_updated_datetime",
    ):
        plaid_client._account_from_plaid(
            _PlaidObject(
                {
                    "account_id": "private-account-id",
                    "name": "Brokerage",
                    "type": "investment",
                    "balances": {
                        "current": 100.0,
                        "iso_currency_code": "USD",
                        "last_updated_datetime": "2026-09-02T18:30:00",
                    },
                }
            )
        )


def test_snaptrade_normalization_retains_documented_sync_status() -> None:
    account = snaptrade_client._account_from_snaptrade(
        {
            "id": "snap-account-1",
            "name": "Brokerage",
            "balance": {"total": {"amount": "123.45", "currency": "USD"}},
            "status": "unavailable",
            "sync_status": {
                "holdings": {
                    "initial_sync_completed": False,
                    "last_successful_sync": "2026-09-01T08:15:00Z",
                },
                "transactions": {
                    "initial_sync_completed": True,
                    "last_successful_sync": "2026-08-31",
                    "first_transaction_date": "2025-01-15",
                },
            },
        }
    )

    assert account.provider_account_status == "unavailable"
    assert account.provider_holdings_initial_sync_completed is False
    assert account.provider_holdings_last_successful_sync == datetime(2026, 9, 1, 8, 15, tzinfo=UTC)
    assert account.provider_transactions_initial_sync_completed is True
    assert account.provider_transactions_last_successful_sync == date(2026, 8, 31)
    assert account.provider_first_transaction_date == date(2025, 1, 15)


def test_snaptrade_authorization_retains_disabled_state(monkeypatch) -> None:
    client = SimpleNamespace(
        connections=SimpleNamespace(
            list_brokerage_authorizations=lambda **_kwargs: SimpleNamespace(
                body=[
                    {
                        "id": "authorization-1",
                        "brokerage": {"name": "Broker"},
                        "disabled": True,
                        "disabled_date": "2026-09-01T07:00:00Z",
                    }
                ]
            )
        )
    )
    monkeypatch.setattr(snaptrade_client, "get_client", lambda: client)

    authorizations = snaptrade_client.list_brokerage_authorizations(
        SnapTradeUserCredentials(user_id="user", user_secret="dummy-value")
    )

    assert authorizations == [
        SnapTradeBrokerageAuthorization(
            authorization_id="authorization-1",
            brokerage_name="Broker",
            disabled=True,
            disabled_at=datetime(2026, 9, 1, 7, tzinfo=UTC),
        )
    ]


def test_snaptrade_authorization_rejects_malformed_disabled_flag(monkeypatch) -> None:
    client = SimpleNamespace(
        connections=SimpleNamespace(
            list_brokerage_authorizations=lambda **_kwargs: SimpleNamespace(
                body=[{"id": "authorization-1", "disabled": "yes"}]
            )
        )
    )
    monkeypatch.setattr(snaptrade_client, "get_client", lambda: client)

    with pytest.raises(ProviderPayloadError, match="offset 0"):
        snaptrade_client.list_brokerage_authorizations(
            SnapTradeUserCredentials(user_id="user", user_secret="dummy-value")
        )


def test_snaptrade_sync_dates_create_only_provider_disclosed_history_gaps() -> None:
    account = plaid_client.PlaidAccount(
        plaid_account_id="snap-account-1",
        name="Brokerage",
        type="investment",
        provider_transactions_initial_sync_completed=True,
        provider_first_transaction_date=date(2025, 1, 15),
        provider_transactions_last_successful_sync=date(2026, 8, 31),
    )
    gaps = snaptrade_route._transaction_history_gaps(
        SnapTradeBrokerageAuthorization(authorization_id="authorization-1", disabled=False),
        account,
        coverage_start=date(2025, 1, 1),
        coverage_end=date(2026, 9, 3),
    )

    assert [(gap.start, gap.end) for gap in gaps] == [
        (date(2025, 1, 1), date(2025, 1, 14)),
        (date(2026, 9, 1), date(2026, 9, 3)),
    ]


def test_plaid_holdings_failure_does_not_expose_access_token(monkeypatch) -> None:
    secret = "dummy-access-token-must-not-escape"

    class _Client:
        def investments_holdings_get(self, _request: object) -> object:
            raise RuntimeError(f"request contained {secret}")

    monkeypatch.setattr(plaid_client, "get_client", lambda: _Client())

    with pytest.raises(ProviderDeliveryError) as raised:
        plaid_client.get_holdings(secret)
    assert secret not in str(raised.value)


def test_snaptrade_login_failure_does_not_expose_user_secret(monkeypatch) -> None:
    secret = "dummy-user-secret-must-not-escape"
    client = SimpleNamespace(
        authentication=SimpleNamespace(
            login_snap_trade_user=lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeError(f"request contained {secret}")
            )
        )
    )
    monkeypatch.setattr(snaptrade_client, "get_client", lambda: client)

    with pytest.raises(ProviderDeliveryError) as raised:
        snaptrade_client.login_url(SnapTradeUserCredentials(user_id="user", user_secret=secret))
    assert secret not in str(raised.value)
