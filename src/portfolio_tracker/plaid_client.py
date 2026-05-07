"""Thin wrapper around the official `plaid-python` SDK.

Centralizes:
  * environment selection (sandbox vs production)
  * product / country code defaults
  * a typed surface for the handful of endpoints we actually call

Every Plaid response is converted to a Pydantic model before leaving this
module — nothing else in the app touches `plaid.*` types.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import plaid
from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration
from plaid.model.country_code import CountryCode
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.investments_transactions_get_request import (
    InvestmentsTransactionsGetRequest,
)
from plaid.model.investments_transactions_get_request_options import (
    InvestmentsTransactionsGetRequestOptions,
)
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from pydantic import BaseModel, ConfigDict, Field

from portfolio_tracker.config import PlaidEnvironment, get_settings


class PlaidAccount(BaseModel):
    plaid_account_id: str
    name: str
    official_name: str | None = None
    type: str
    subtype: str | None = None
    mask: str | None = None
    currency: str = "USD"


class PlaidSecurity(BaseModel):
    plaid_security_id: str
    ticker: str | None = None
    cusip: str | None = None
    isin: str | None = None
    name: str | None = None
    type: str | None = None
    currency: str = "USD"
    is_cash_equivalent: bool = False
    close_price: Decimal | None = None
    close_price_as_of: date | None = None


class PlaidHolding(BaseModel):
    plaid_account_id: str
    plaid_security_id: str
    quantity: Decimal
    institution_price: Decimal | None = None
    institution_value: Decimal | None = None
    cost_basis: Decimal | None = None
    currency: str = "USD"


class PlaidInvestmentTransaction(BaseModel):
    plaid_investment_transaction_id: str
    plaid_account_id: str
    plaid_security_id: str | None = None
    date: date
    name: str | None = None
    quantity: Decimal = Decimal(0)
    amount: Decimal
    price: Decimal | None = None
    fees: Decimal | None = None
    type: str
    subtype: str | None = None
    currency: str = "USD"


class HoldingsResponse(BaseModel):
    accounts: list[PlaidAccount]
    securities: list[PlaidSecurity]
    holdings: list[PlaidHolding]
    item_id: str
    institution_id: str | None = None


class InvestmentsTransactionsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    accounts: list[PlaidAccount]
    securities: list[PlaidSecurity]
    transactions: list[PlaidInvestmentTransaction] = Field(default_factory=list)
    total_transactions: int


def _build_client() -> plaid_api.PlaidApi:
    settings = get_settings()
    host = (
        plaid.Environment.Sandbox
        if settings.plaid_env == PlaidEnvironment.SANDBOX
        else plaid.Environment.Production
    )
    config = Configuration(
        host=host,
        api_key={
            "clientId": settings.plaid_client_id,
            "secret": settings.plaid_secret,
        },
    )
    return plaid_api.PlaidApi(ApiClient(config))


_client: plaid_api.PlaidApi | None = None


def get_client() -> plaid_api.PlaidApi:
    global _client
    if _client is None:
        _client = _build_client()
    return _client


def create_link_token(client_user_id: str) -> str:
    settings = get_settings()
    products = [Products(p) for p in settings.plaid_products_list]
    countries = [CountryCode(c) for c in settings.plaid_country_codes_list]
    request = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=client_user_id),
        client_name="Portfolio Tracker",
        products=products,
        country_codes=countries,
        language="en",
    )
    response = get_client().link_token_create(request)
    return response.link_token


def exchange_public_token(public_token: str) -> tuple[str, str]:
    """Exchange a Link `public_token` for `(access_token, item_id)`."""
    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = get_client().item_public_token_exchange(request)
    return response.access_token, response.item_id


def _to_plaid_dict(raw: object) -> dict[str, object]:
    """Convert a plaid-python model to a plain dict.

    The auto-generated SDK's `__getattr__` raises `ApiValueError` for any
    composed-schema property whose value differs between `self` and its
    composed instances (a recurring quirk on `InvestmentAccount.balances`).
    `.to_dict()` walks the underlying datastore and returns a clean dict,
    sidestepping the validation entirely.
    """
    to_dict = getattr(raw, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, dict):
            return result
    raise TypeError(f"Plaid object {type(raw)!r} has no callable to_dict()")


def _account_from_plaid(raw: object) -> PlaidAccount:
    data = _to_plaid_dict(raw)
    balances = data.get("balances") or {}
    if not isinstance(balances, dict):
        balances = {}
    return PlaidAccount(
        plaid_account_id=str(data["account_id"]),
        name=str(data["name"]),
        official_name=_opt_str(data.get("official_name")),
        type=str(data["type"]),
        subtype=_opt_str(data.get("subtype")),
        mask=_opt_str(data.get("mask")),
        currency=str(balances.get("iso_currency_code") or "USD"),
    )


def _security_from_plaid(raw: object) -> PlaidSecurity:
    data = _to_plaid_dict(raw)
    return PlaidSecurity(
        plaid_security_id=str(data["security_id"]),
        ticker=_opt_str(data.get("ticker_symbol")),
        cusip=_opt_str(data.get("cusip")),
        isin=_opt_str(data.get("isin")),
        name=_opt_str(data.get("name")),
        type=_opt_str(data.get("type")),
        currency=str(data.get("iso_currency_code") or "USD"),
        is_cash_equivalent=bool(data.get("is_cash_equivalent", False)),
        close_price=_opt_decimal(data.get("close_price")),
        close_price_as_of=_opt_date(data.get("close_price_as_of")),
    )


def _holding_from_plaid(raw: object) -> PlaidHolding:
    data = _to_plaid_dict(raw)
    return PlaidHolding(
        plaid_account_id=str(data["account_id"]),
        plaid_security_id=str(data["security_id"]),
        quantity=Decimal(str(data["quantity"])),
        institution_price=_opt_decimal(data.get("institution_price")),
        institution_value=_opt_decimal(data.get("institution_value")),
        cost_basis=_opt_decimal(data.get("cost_basis")),
        currency=str(data.get("iso_currency_code") or "USD"),
    )


def _investment_tx_from_plaid(raw: object) -> PlaidInvestmentTransaction:
    data = _to_plaid_dict(raw)
    tx_date = _opt_date(data.get("date"))
    if tx_date is None:
        raise ValueError(
            f"investment transaction missing date: {data.get('investment_transaction_id')}"
        )
    return PlaidInvestmentTransaction(
        plaid_investment_transaction_id=str(data["investment_transaction_id"]),
        plaid_account_id=str(data["account_id"]),
        plaid_security_id=_opt_str(data.get("security_id")),
        date=tx_date,
        name=_opt_str(data.get("name")),
        quantity=Decimal(str(data.get("quantity", 0))),
        amount=Decimal(str(data["amount"])),
        price=_opt_decimal(data.get("price")),
        fees=_opt_decimal(data.get("fees")),
        type=str(data["type"]),
        subtype=_opt_str(data.get("subtype")),
        currency=str(data.get("iso_currency_code") or "USD"),
    )


def _opt_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _opt_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    return None


def get_holdings(access_token: str) -> HoldingsResponse:
    request = InvestmentsHoldingsGetRequest(access_token=access_token)
    response = get_client().investments_holdings_get(request)
    item_dict = _to_plaid_dict(response.item)
    return HoldingsResponse(
        accounts=[_account_from_plaid(a) for a in response.accounts],
        securities=[_security_from_plaid(s) for s in response.securities],
        holdings=[_holding_from_plaid(h) for h in response.holdings],
        item_id=str(item_dict["item_id"]),
        institution_id=_opt_str(item_dict.get("institution_id")),
    )


def get_investment_transactions(
    access_token: str,
    start_date: date,
    end_date: date,
) -> InvestmentsTransactionsResponse:
    """Pull every investment transaction in [start_date, end_date], paginating internally."""
    page_size = 500
    all_tx: list[PlaidInvestmentTransaction] = []
    accounts: list[PlaidAccount] = []
    securities: list[PlaidSecurity] = []
    total = 0
    offset = 0
    client = get_client()
    while True:
        request = InvestmentsTransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=InvestmentsTransactionsGetRequestOptions(count=page_size, offset=offset),
        )
        response = client.investments_transactions_get(request)
        if offset == 0:
            accounts = [_account_from_plaid(a) for a in response.accounts]
            securities = [_security_from_plaid(s) for s in response.securities]
            total = response.total_investment_transactions
        all_tx.extend(_investment_tx_from_plaid(t) for t in response.investment_transactions)
        offset += len(response.investment_transactions)
        if offset >= total or not response.investment_transactions:
            break
    return InvestmentsTransactionsResponse(
        accounts=accounts,
        securities=securities,
        transactions=all_tx,
        total_transactions=total,
    )
