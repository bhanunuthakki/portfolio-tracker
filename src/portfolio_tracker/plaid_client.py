"""Thin wrapper around the official `plaid-python` SDK.

Centralizes:
  * environment selection (sandbox vs production)
  * product / country code defaults
  * a typed surface for the handful of endpoints we actually call

Every Plaid response is converted to a Pydantic model before leaving this
module — nothing else in the app touches `plaid.*` types.

The generated `plaid-python` SDK is imported LAZILY, inside the five functions
that actually construct SDK objects. It is ~2.9s of import time — the bulk of
it hundreds of `plaid.model.*` modules at ~90ms each — and none of it is needed
to serve a request that doesn't call Plaid, which is nearly all of them. The
models and normalization helpers below are deliberately free of `plaid.*` at
runtime (the `_*_from_plaid` helpers duck-type through `.to_dict()`), so this
module stays cheap to import. Same rationale as `_ensure_snaptrade` in
`snaptrade_client.py`; see that module's docstring for the boot-time story.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict

from portfolio_tracker.config import PlaidEnvironment, get_settings
from portfolio_tracker.provider_delivery import (
    ProviderDeliveryError,
    ProviderDeliveryIncompleteError,
    ProviderDeliveryMetadata,
    ProviderPayloadError,
    build_provider_delivery_metadata,
)

if TYPE_CHECKING:
    # Type-only: `from __future__ import annotations` keeps every annotation
    # below a string at runtime, so naming `plaid_api` costs nothing at import.
    from plaid.api import plaid_api


class PlaidAccount(BaseModel):
    plaid_account_id: str
    name: str
    official_name: str | None = None
    type: str
    subtype: str | None = None
    mask: str | None = None
    currency: str = "USD"
    provider_total_value: Decimal | None = None
    provider_available_cash: Decimal | None = None
    # Direct provider freshness/availability facts only.  A missing timestamp
    # is not replaced with fetch time: Plaid documents holdings balances as
    # potentially cached and exposes last_updated_datetime only for a narrow
    # institution set.
    provider_balance_as_of: datetime | None = None
    provider_account_status: str | None = None
    provider_holdings_initial_sync_completed: bool | None = None
    provider_holdings_last_successful_sync: datetime | None = None
    provider_transactions_initial_sync_completed: bool | None = None
    provider_transactions_last_successful_sync: date | None = None
    provider_first_transaction_date: date | None = None


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
    transactions: list[PlaidInvestmentTransaction] = []
    total_transactions: int
    delivery: ProviderDeliveryMetadata | None = None


_INVESTMENT_TRANSACTIONS_SOURCE_FORMAT = "plaid_investment_transactions_api"
_INVESTMENT_TRANSACTIONS_PARSER_VERSION = "plaid_investment_tx.v1"


def _build_client() -> plaid_api.PlaidApi:
    import plaid
    from plaid.api import plaid_api
    from plaid.api_client import ApiClient
    from plaid.configuration import Configuration

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
    from plaid.model.country_code import CountryCode
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.products import Products

    settings = get_settings()
    products: list[Any] = [Products(p) for p in settings.plaid_products_list]
    countries: list[Any] = [CountryCode(c) for c in settings.plaid_country_codes_list]
    request = cast(
        Any,
        LinkTokenCreateRequest(
            user=LinkTokenCreateRequestUser(client_user_id=client_user_id),
            client_name="Portfolio Tracker",
            products=products,
            country_codes=countries,
            language="en",
        ),
    )
    try:
        response = cast(Any, get_client().link_token_create(request))
    except Exception:
        raise ProviderDeliveryError("Plaid link token request failed") from None
    return str(response.link_token)


def exchange_public_token(public_token: str) -> tuple[str, str]:
    """Exchange a Link `public_token` for `(access_token, item_id)`."""
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

    request = cast(Any, ItemPublicTokenExchangeRequest(public_token=public_token))
    try:
        response = cast(Any, get_client().item_public_token_exchange(request))
    except Exception:
        raise ProviderDeliveryError("Plaid public token exchange failed") from None
    return str(response.access_token), str(response.item_id)


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
            return cast("dict[str, object]", result)
    raise TypeError(f"Plaid object {type(raw)!r} has no callable to_dict()")


def _account_from_plaid(raw: object) -> PlaidAccount:
    data = _to_plaid_dict(raw)
    raw_balances = data.get("balances")
    balances: dict[str, object] = (
        cast("dict[str, object]", raw_balances) if isinstance(raw_balances, dict) else {}
    )
    return PlaidAccount(
        plaid_account_id=_required_text(data, "account_id"),
        name=_required_text(data, "name"),
        official_name=_opt_str(data.get("official_name")),
        type=_required_text(data, "type"),
        subtype=_opt_str(data.get("subtype")),
        mask=_opt_str(data.get("mask")),
        currency=str(balances.get("iso_currency_code") or "USD"),
        provider_total_value=_opt_decimal(balances.get("current")),
        provider_available_cash=_opt_decimal(balances.get("available")),
        provider_balance_as_of=_opt_datetime(balances.get("last_updated_datetime")),
    )


def _security_from_plaid(raw: object) -> PlaidSecurity:
    data = _to_plaid_dict(raw)
    return PlaidSecurity(
        plaid_security_id=_required_text(data, "security_id"),
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
        plaid_investment_transaction_id=_required_text(data, "investment_transaction_id"),
        plaid_account_id=_required_text(data, "account_id"),
        plaid_security_id=_opt_str(data.get("security_id")),
        date=tx_date,
        name=_opt_str(data.get("name")),
        quantity=Decimal(str(data.get("quantity", 0))),
        amount=Decimal(str(data["amount"])),
        price=_opt_decimal(data.get("price")),
        fees=_opt_decimal(data.get("fees")),
        type=_required_text(data, "type"),
        subtype=_opt_str(data.get("subtype")),
        currency=str(data.get("iso_currency_code") or "USD"),
    )


def _opt_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _required_text(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if value is None:
        raise ValueError(f"provider payload is missing required field {field}")
    text = str(value).strip()
    if not text:
        raise ValueError(f"provider payload has empty required field {field}")
    return text


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _opt_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError("provider date field has invalid type")


def _opt_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    raise ValueError("provider datetime field has invalid type")


def get_holdings(access_token: str) -> HoldingsResponse:
    from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest

    request = cast(Any, InvestmentsHoldingsGetRequest(access_token=access_token))
    try:
        response = cast(Any, get_client().investments_holdings_get(request))
    except Exception:
        raise ProviderDeliveryError("Plaid holdings request failed") from None
    try:
        item_dict = _to_plaid_dict(response.item)
        return HoldingsResponse(
            accounts=[_account_from_plaid(a) for a in response.accounts],
            securities=[_security_from_plaid(s) for s in response.securities],
            holdings=[_holding_from_plaid(h) for h in response.holdings],
            item_id=str(item_dict["item_id"]),
            institution_id=_opt_str(item_dict.get("institution_id")),
        )
    except Exception:
        raise ProviderPayloadError("Plaid holdings payload failed validation") from None


def get_investment_transactions(
    access_token: str,
    start_date: date,
    end_date: date,
) -> InvestmentsTransactionsResponse:
    """Pull every investment transaction in [start_date, end_date], paginating internally."""
    from plaid.model.investments_transactions_get_request import (
        InvestmentsTransactionsGetRequest,
    )
    from plaid.model.investments_transactions_get_request_options import (
        InvestmentsTransactionsGetRequestOptions,
    )

    page_size = 500
    all_tx: list[PlaidInvestmentTransaction] = []
    accounts_by_id: dict[str, PlaidAccount] = {}
    securities_by_id: dict[str, PlaidSecurity] = {}
    total: int | None = None
    offset = 0
    page_count = 0
    client = get_client()
    while True:
        request = cast(
            Any,
            InvestmentsTransactionsGetRequest(
                access_token=access_token,
                start_date=start_date,
                end_date=end_date,
                options=InvestmentsTransactionsGetRequestOptions(count=page_size, offset=offset),
            ),
        )
        try:
            response = cast(Any, client.investments_transactions_get(request))
        except Exception:
            # Provider exceptions can retain request context. Do not propagate
            # an access-token-bearing body into logs or tracebacks.
            raise ProviderDeliveryError("Plaid investment transaction request failed") from None
        page_count += 1
        try:
            raw_total = response.total_investment_transactions
        except Exception:
            raise ProviderPayloadError("Plaid pagination total is missing") from None
        page_total = _provider_total(raw_total, provider="Plaid")
        if total is None:
            total = page_total
        elif page_total != total:
            raise ProviderDeliveryIncompleteError(
                "Plaid transaction delivery total changed during pagination"
            )
        try:
            for raw_account in response.accounts:
                account = _account_from_plaid(raw_account)
                accounts_by_id[account.plaid_account_id] = account
            for raw_security in response.securities:
                security = _security_from_plaid(raw_security)
                securities_by_id[security.plaid_security_id] = security
            page_rows = list(response.investment_transactions)
        except Exception:
            raise ProviderPayloadError("Plaid transaction page failed validation") from None
        if not page_rows and offset < total:
            raise ProviderDeliveryIncompleteError(
                f"Plaid transaction delivery stopped early: reported={total}, fetched={offset}"
            )
        for index, raw_transaction in enumerate(page_rows, start=offset):
            try:
                all_tx.append(_investment_tx_from_plaid(raw_transaction))
            except Exception:
                raise ProviderPayloadError(
                    f"Plaid transaction payload failed validation at offset {index}"
                ) from None
        offset += len(page_rows)
        if offset >= total:
            break
    delivery = build_provider_delivery_metadata(
        provider="plaid",
        source_format=_INVESTMENT_TRANSACTIONS_SOURCE_FORMAT,
        parser_version=_INVESTMENT_TRANSACTIONS_PARSER_VERSION,
        requested_start_date=start_date,
        requested_end_date=end_date,
        page_count=page_count,
        provider_reported_total=total,
        record_ids=[tx.plaid_investment_transaction_id for tx in all_tx],
        normalized_records=[tx.model_dump(mode="json") for tx in all_tx],
    )
    return InvestmentsTransactionsResponse(
        accounts=list(accounts_by_id.values()),
        securities=list(securities_by_id.values()),
        transactions=all_tx,
        total_transactions=total,
        delivery=delivery,
    )


def _provider_total(value: object, *, provider: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderPayloadError(f"{provider} pagination total is invalid")
    return value
