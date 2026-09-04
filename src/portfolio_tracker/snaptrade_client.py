"""Thin wrapper around the official `snaptrade-python-sdk`.

Mirrors the surface of `plaid_client.py` so that ingest jobs can branch on
`item.source` and otherwise treat both data feeds identically. Every
SnapTrade response is converted to a Pydantic model before leaving this
module — nothing else in the app touches `snaptrade_client.*` types.

SnapTrade's auth model differs from Plaid:
  * You register a USER once (returns a `user_secret`)
  * The same user can have many BROKERAGE CONNECTIONS (Fidelity, Schwab, ...)
  * Every API call requires `user_id` + `user_secret`
  * Each connection is identified by an `authorization_id`
  * Each connection exposes one or more ACCOUNTS

We map this onto our schema as: one Item per brokerage connection (keyed by
`snaptrade_authorization_id`), with `snaptrade_user_id` / `_user_secret`
copied per Item for simplicity.

The vendor SDK is imported LAZILY (see `_ensure_snaptrade`) — everything above
this line is Pydantic models and pure normalization that must stay cheap to
import.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

from portfolio_tracker.config import get_settings
from portfolio_tracker.plaid_client import (
    PlaidAccount,
    PlaidHolding,
    PlaidInvestmentTransaction,
    PlaidSecurity,
)
from portfolio_tracker.provider_delivery import (
    ProviderDeliveryError,
    ProviderDeliveryIncompleteError,
    ProviderDeliveryMetadata,
    ProviderPayloadError,
    build_provider_delivery_metadata,
)

if TYPE_CHECKING:
    # Type-only: `from __future__ import annotations` keeps every annotation
    # below a string at runtime, so naming `SnapTrade` costs nothing at import.
    from snaptrade_client import SnapTrade


class SnapTradeNotConfiguredError(RuntimeError):
    """Raised when SnapTrade endpoints are called without env vars set."""


class SnapTradeUserCredentials(BaseModel):
    user_id: str
    user_secret: str


class SnapTradeBrokerageAuthorization(BaseModel):
    authorization_id: str
    brokerage_name: str | None = None
    disabled: bool | None = None
    disabled_at: datetime | None = None


class SnapTradeHoldingsResponse(BaseModel):
    accounts: list[PlaidAccount]
    securities: list[PlaidSecurity]
    holdings: list[PlaidHolding]


class SnapTradeTransactionsResponse(BaseModel):
    accounts: list[PlaidAccount]
    securities: list[PlaidSecurity]
    transactions: list[PlaidInvestmentTransaction]
    total_transactions: int = 0
    delivery: ProviderDeliveryMetadata | None = None


_ACCOUNT_ACTIVITIES_SOURCE_FORMAT = "snaptrade_account_activities_api"
_ACCOUNT_ACTIVITIES_PARSER_VERSION = "snaptrade_account_activity.v1"
_ACCOUNT_ACTIVITIES_PAGE_SIZE = 1000


_client: SnapTrade | None = None


def _ensure_snaptrade() -> type[SnapTrade]:
    """Import the vendor SDK on first SnapTrade use and return its client class.

    `snaptrade_client` is by far the most expensive import in the tree (~5s
    warm, ~30s cold) and it used to be paid on every `uvicorn` boot, delaying
    the port bind past the point where a dev-server / preview launcher gives up
    probing. SnapTrade is optional (see `config.Settings.snaptrade_client_id`),
    so the cost belongs on the first SnapTrade call, not on import.

    A missing SDK is reported as `SnapTradeNotConfiguredError` so an install
    without the optional dependency still 503s through the routes rather than
    surfacing an ImportError as a 500.
    """
    try:
        from snaptrade_client import SnapTrade
    except ImportError as exc:  # pragma: no cover - SDK is a declared dependency
        raise SnapTradeNotConfiguredError(
            "The `snaptrade-python-sdk` package is not installed; SnapTrade is unavailable."
        ) from exc
    return SnapTrade


def get_client() -> SnapTrade:
    global _client
    if _client is not None:
        return _client
    settings = get_settings()
    # Check credentials BEFORE importing: an unconfigured install must reach the
    # 503 path without ever paying the SDK import.
    if settings.snaptrade_client_id is None or settings.snaptrade_consumer_key is None:
        raise SnapTradeNotConfiguredError(
            "Set SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY in .env to enable SnapTrade."
        )
    _client = _ensure_snaptrade()(
        client_id=settings.snaptrade_client_id,
        consumer_key=settings.snaptrade_consumer_key,
    )
    return _client


def is_configured() -> bool:
    settings = get_settings()
    return settings.snaptrade_client_id is not None and settings.snaptrade_consumer_key is not None


def register_user(user_id: str) -> SnapTradeUserCredentials:
    """Register `user_id` with SnapTrade. Returns the secret to store securely.

    NOT idempotent: SnapTrade rejects re-registration of an existing user_id.
    Use `recover_user` if you've lost the secret for a user that already
    exists on SnapTrade's side.
    """
    response = get_client().authentication.register_snap_trade_user(user_id=user_id)
    body = _body(response)
    return SnapTradeUserCredentials(user_id=user_id, user_secret=str(body["userSecret"]))


def delete_user(user_id: str) -> None:
    """Hard-delete a SnapTrade user — wipes all their brokerage connections.

    Use only when we've lost the user_secret and need to re-register. The
    user will have to re-link every brokerage via the Connection Portal.
    """
    get_client().authentication.delete_snap_trade_user(user_id=user_id)


def recover_user(user_id: str) -> SnapTradeUserCredentials:
    """Delete-and-re-register a user when we've lost the secret.

    Destructive: every brokerage connection under this user is wiped on
    SnapTrade's side. Returns a fresh user_secret that the caller MUST
    persist before doing anything else.

    Includes retry-with-backoff because SnapTrade's user-count enforcement
    runs slightly after the delete completes — re-registering immediately
    after delete sometimes returns code 1012 ("Personal keys can only
    register one user") even though the previous user is already gone.
    """
    delete_user(user_id)
    last_exc: BaseException | None = None
    for attempt_delay in (0, 2, 5, 10):
        if attempt_delay > 0:
            time.sleep(attempt_delay)
        try:
            return register_user(user_id)
        except Exception as exc:  # SDK raises bare ApiException
            text = str(exc).lower()
            if "1012" in text or "can only register one user" in text:
                last_exc = exc
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("recover_user: register_user returned no result and no error")


def is_user_already_exists_error(exc: BaseException) -> bool:
    """Heuristic: did SnapTrade reject register_user because the id is taken?

    SDK surfaces these as `ApiException` with a JSON body. We match on the
    status code (400) and a stable substring of the error text rather than
    parsing the body — the SDK's exception types vary by version.
    """
    text = str(exc).lower()
    if "400" not in text and "409" not in text:
        return False
    return "already exist" in text or "user_id" in text or "userid" in text


def login_url(creds: SnapTradeUserCredentials, custom_redirect: str | None = None) -> str:
    """Mint a SnapTrade Connection Portal URL for the user.

    The URL is short-lived (~5 minutes) and single-use. The user opens it,
    picks a brokerage, logs in, and SnapTrade redirects back to
    `custom_redirect` (or its default) when done.
    """
    kwargs: dict[str, Any] = {"user_id": creds.user_id, "user_secret": creds.user_secret}
    if custom_redirect is not None:
        kwargs["custom_redirect"] = custom_redirect
    try:
        response = get_client().authentication.login_snap_trade_user(**kwargs)
    except SnapTradeNotConfiguredError:
        raise
    except Exception:
        raise ProviderDeliveryError("SnapTrade login URL request failed") from None
    body = _body(response)
    redirect = cast(object, body.get("redirectURI") if isinstance(body, dict) else body)
    if not isinstance(redirect, str):
        raise ProviderPayloadError("SnapTrade login response is missing a redirect URL")
    return redirect


def list_brokerage_authorizations(
    creds: SnapTradeUserCredentials,
) -> list[SnapTradeBrokerageAuthorization]:
    try:
        response = get_client().connections.list_brokerage_authorizations(
            user_id=creds.user_id, user_secret=creds.user_secret
        )
    except SnapTradeNotConfiguredError:
        raise
    except Exception:
        # The generated SDK sends user_secret in the query string. Never
        # propagate its credential-bearing request URL through an exception.
        raise ProviderDeliveryError("SnapTrade authorization request failed") from None
    body = _body(response)
    items = _snaptrade_list_payload(body, label="authorization")
    out: list[SnapTradeBrokerageAuthorization] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            raise ProviderPayloadError(
                f"SnapTrade authorization payload failed validation at offset {index}"
            )
        payload = dict(cast("Mapping[str, Any]", raw))
        authorization_id = payload.get("id")
        if not isinstance(authorization_id, str) or not authorization_id:
            raise ProviderPayloadError(
                f"SnapTrade authorization payload failed validation at offset {index}"
            )
        try:
            out.append(
                SnapTradeBrokerageAuthorization(
                    authorization_id=authorization_id,
                    brokerage_name=_opt_str(_dig(payload, ["brokerage", "name"])),
                    disabled=_opt_bool(payload.get("disabled")),
                    disabled_at=_opt_datetime(payload.get("disabled_date")),
                )
            )
        except Exception:
            raise ProviderPayloadError(
                f"SnapTrade authorization payload failed validation at offset {index}"
            ) from None
    return out


def list_user_accounts(
    creds: SnapTradeUserCredentials,
    authorization_id: str | None = None,
) -> list[tuple[str, PlaidAccount]]:
    """Return [(snaptrade_account_id, normalized_account), ...].

    If `authorization_id` is provided, only accounts under that connection
    are returned.
    """
    try:
        response = get_client().account_information.list_user_accounts(
            user_id=creds.user_id, user_secret=creds.user_secret
        )
    except SnapTradeNotConfiguredError:
        raise
    except Exception:
        raise ProviderDeliveryError("SnapTrade account list request failed") from None
    body = _body(response)
    rows = _snaptrade_list_payload(body, label="account")
    out: list[tuple[str, PlaidAccount]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ProviderPayloadError(
                f"SnapTrade account payload failed validation at offset {index}"
            )
        raw = dict(cast("Mapping[str, Any]", raw))
        if (
            authorization_id is not None
            and str(raw.get("brokerage_authorization", "")) != authorization_id
        ):
            continue
        try:
            account_id = str(raw["id"])
            out.append((account_id, _account_from_snaptrade(raw)))
        except Exception:
            raise ProviderPayloadError(
                f"SnapTrade account payload failed validation at offset {index}"
            ) from None
    return out


def get_holdings(
    creds: SnapTradeUserCredentials, snaptrade_account_id: str
) -> SnapTradeHoldingsResponse:
    try:
        response = get_client().account_information.get_user_holdings(
            user_id=creds.user_id,
            user_secret=creds.user_secret,
            account_id=snaptrade_account_id,
        )
    except SnapTradeNotConfiguredError:
        raise
    except Exception:
        raise ProviderDeliveryError("SnapTrade holdings request failed") from None
    raw_body = _body(response)
    if not isinstance(raw_body, Mapping):
        raise ProviderPayloadError("SnapTrade holdings response is not an object")
    body = dict(cast("Mapping[str, Any]", raw_body))
    accounts: list[PlaidAccount] = []
    if "account" in body:
        account = _account_from_snaptrade(body["account"])
        account = account.model_copy(
            update={
                "provider_available_cash": _snaptrade_available_cash(
                    body.get("balances"), account.currency
                )
            }
        )
        accounts.append(account)
    raw_positions = body.get("positions", [])
    securities: dict[str, PlaidSecurity] = {}
    holdings: list[PlaidHolding] = []
    plaid_account_id = accounts[0].plaid_account_id if accounts else snaptrade_account_id
    for pos in raw_positions:
        sec = _security_from_snaptrade(pos.get("symbol", {}))
        securities[sec.plaid_security_id] = sec
        holdings.append(_holding_from_snaptrade(pos, plaid_account_id, sec.plaid_security_id))
    return SnapTradeHoldingsResponse(
        accounts=accounts,
        securities=list(securities.values()),
        holdings=holdings,
    )


def get_account_activities(
    creds: SnapTradeUserCredentials,
    snaptrade_account_id: str,
    start_date: date,
    end_date: date,
) -> SnapTradeTransactionsResponse:
    activity_kwargs: dict[str, Any] = {
        "user_id": creds.user_id,
        "user_secret": creds.user_secret,
        "account_id": snaptrade_account_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "limit": _ACCOUNT_ACTIVITIES_PAGE_SIZE,
    }
    securities: dict[str, PlaidSecurity] = {}
    transactions: list[PlaidInvestmentTransaction] = []
    total: int | None = None
    offset = 0
    page_count = 0
    while True:
        try:
            response = get_client().account_information.get_account_activities(
                **activity_kwargs,
                offset=offset,
            )
        except SnapTradeNotConfiguredError:
            raise
        except Exception:
            # The generated SDK sends user_secret as a query parameter, so its
            # raw exception may contain the credentialed URL.
            raise ProviderDeliveryError("SnapTrade account activity request failed") from None
        body = _body(response)
        rows, page_total = _snaptrade_activity_page(body)
        page_count += 1
        if total is None:
            total = page_total
        elif page_total != total:
            raise ProviderDeliveryIncompleteError(
                "SnapTrade transaction delivery total changed during pagination"
            )
        if not rows and offset < total:
            raise ProviderDeliveryIncompleteError(
                f"SnapTrade transaction delivery stopped early: reported={total}, fetched={offset}"
            )
        for index, raw_row in enumerate(rows, start=offset):
            raw = dict(raw_row)
            try:
                symbol_payload = raw.get("symbol") or raw.get("option_symbol")
                plaid_security_id: str | None = None
                if isinstance(symbol_payload, Mapping):
                    symbol_mapping = cast("Mapping[str, Any]", symbol_payload)
                    sec = _security_from_snaptrade(dict(symbol_mapping))
                    securities[sec.plaid_security_id] = sec
                    plaid_security_id = sec.plaid_security_id
                transactions.append(
                    _transaction_from_snaptrade(raw, snaptrade_account_id, plaid_security_id)
                )
            except Exception:
                raise ProviderPayloadError(
                    f"SnapTrade activity payload failed validation at offset {index}"
                ) from None
        offset += len(rows)
        if offset >= total:
            break
    delivery = build_provider_delivery_metadata(
        provider="snaptrade",
        source_format=_ACCOUNT_ACTIVITIES_SOURCE_FORMAT,
        parser_version=_ACCOUNT_ACTIVITIES_PARSER_VERSION,
        requested_start_date=start_date,
        requested_end_date=end_date,
        page_count=page_count,
        provider_reported_total=total,
        record_ids=[tx.plaid_investment_transaction_id for tx in transactions],
        normalized_records=[tx.model_dump(mode="json") for tx in transactions],
    )
    return SnapTradeTransactionsResponse(
        accounts=[],
        securities=list(securities.values()),
        transactions=transactions,
        total_transactions=total,
        delivery=delivery,
    )


# ---- normalization helpers ----------------------------------------------


def _account_from_snaptrade(raw: dict[str, Any]) -> PlaidAccount:
    raw_account_id = raw.get("id")
    if not isinstance(raw_account_id, str) or not raw_account_id:
        raise ValueError("account is missing a provider account id")
    snaptrade_account_id = raw_account_id
    raw_account_status = _opt_str(raw.get("status"))
    account_status = raw_account_status.lower() if raw_account_status is not None else None
    holdings_initial_sync = _opt_bool(
        _dig(raw, ["sync_status", "holdings", "initial_sync_completed"])
    )
    holdings_last_sync = _opt_datetime(
        _dig(raw, ["sync_status", "holdings", "last_successful_sync"])
    )
    transactions_initial_sync = _opt_bool(
        _dig(raw, ["sync_status", "transactions", "initial_sync_completed"])
    )
    transactions_last_sync = _opt_date(
        _dig(raw, ["sync_status", "transactions", "last_successful_sync"])
    )
    first_transaction_date = _opt_date(
        _dig(raw, ["sync_status", "transactions", "first_transaction_date"])
    )
    return PlaidAccount(
        plaid_account_id=snaptrade_account_id,
        name=str(raw.get("name") or raw.get("number") or snaptrade_account_id),
        official_name=_opt_str(raw.get("name")),
        type="investment",
        subtype=_opt_str(
            raw.get("meta", {}).get("type") if isinstance(raw.get("meta"), dict) else None
        ),
        mask=_opt_str(raw.get("number")),
        currency=str(_dig(raw, ["balance", "total", "currency"]) or "USD"),
        provider_total_value=_to_decimal(_dig(raw, ["balance", "total", "amount"])),
        provider_available_cash=None,
        provider_account_status=account_status,
        provider_holdings_initial_sync_completed=holdings_initial_sync,
        provider_holdings_last_successful_sync=holdings_last_sync,
        provider_transactions_initial_sync_completed=transactions_initial_sync,
        provider_transactions_last_successful_sync=transactions_last_sync,
        provider_first_transaction_date=first_transaction_date,
    )


def _security_from_snaptrade(raw: dict[str, Any]) -> PlaidSecurity:
    symbol = raw.get("symbol")
    symbol_dict = cast("dict[str, Any]", symbol if isinstance(symbol, dict) else raw)
    raw_symbol_id = (
        symbol_dict.get("id") or symbol_dict.get("symbol") or symbol_dict.get("raw_symbol")
    )
    if raw_symbol_id is None or not str(raw_symbol_id).strip():
        raise ValueError("security is missing a provider symbol id")
    symbol_id = str(raw_symbol_id).strip()
    return PlaidSecurity(
        plaid_security_id=f"snaptrade:{symbol_id}",
        ticker=_opt_str(symbol_dict.get("symbol") or symbol_dict.get("raw_symbol")),
        cusip=_opt_str(symbol_dict.get("cusip")),
        isin=None,
        name=_opt_str(symbol_dict.get("description")),
        type=_opt_str(_dig(symbol_dict, ["type", "code"])),
        currency=str(_dig(symbol_dict, ["currency", "code"]) or "USD"),
        is_cash_equivalent=False,
        close_price=None,
        close_price_as_of=None,
    )


def _holding_from_snaptrade(
    raw: dict[str, Any], plaid_account_id: str, plaid_security_id: str
) -> PlaidHolding:
    quantity = _to_decimal(raw.get("units")) or Decimal(0)
    price = _to_decimal(raw.get("price"))
    value = quantity * price if price is not None else None
    # SnapTrade reports `average_purchase_price` PER SHARE, but `cost_basis`
    # follows the Plaid convention of TOTAL dollars for the lot (every
    # downstream consumer — Holdings unrealized P&L, Trade Analysis — treats
    # it as total). Multiply by units so the column is aggregator-independent;
    # without this, SnapTrade-sourced cost basis is understated by a factor of
    # quantity unless a CostBasisOverride happens to mask it.
    avg_price = _to_decimal(raw.get("average_purchase_price"))
    cost_basis = avg_price * quantity if avg_price is not None else None
    return PlaidHolding(
        plaid_account_id=plaid_account_id,
        plaid_security_id=plaid_security_id,
        quantity=quantity,
        institution_price=price,
        institution_value=value,
        cost_basis=cost_basis,
        currency=str(_dig(raw, ["currency", "code"]) or "USD"),
    )


def _transaction_from_snaptrade(
    raw: dict[str, Any], plaid_account_id: str, plaid_security_id: str | None
) -> PlaidInvestmentTransaction:
    tx_id = raw.get("id")
    if not isinstance(tx_id, str) or not tx_id:
        raise ValueError("activity is missing a provider record id")
    tx_date = _opt_date(raw.get("trade_date") or raw.get("settlement_date"))
    if tx_date is None:
        raise ValueError("activity is missing an effective date")
    raw_type_value = raw.get("type")
    if not isinstance(raw_type_value, str) or not raw_type_value.strip():
        raise ValueError("activity is missing a type")
    raw_type = raw_type_value.upper()
    canonical_type, subtype = _classify_activity(raw_type)
    # SnapTrade declares amount nullable for valid in-kind transfers and
    # corporate actions. Preserve those rows as zero-cash events; their units
    # still drive the position walk-back and in-kind flow valuation.
    amount = _to_decimal(raw.get("amount")) or Decimal(0)
    return PlaidInvestmentTransaction(
        plaid_investment_transaction_id=f"snaptrade:{tx_id}",
        plaid_account_id=plaid_account_id,
        plaid_security_id=plaid_security_id,
        date=tx_date,
        name=_opt_str(raw.get("description")),
        quantity=_to_decimal(raw.get("units")) or Decimal(0),
        amount=amount,
        price=_to_decimal(raw.get("price")),
        fees=_to_decimal(raw.get("fee")),
        type=canonical_type,
        subtype=subtype,
        currency=str(_dig(raw, ["currency", "code"]) or "USD"),
    )


# Map SnapTrade activity types onto Plaid's canonical type/subtype so the rest
# of the pipeline (TWR cashflow detection, Plaid-shaped Pydantic models)
# behaves identically.
_ACTIVITY_MAP: dict[str, tuple[str, str | None]] = {
    "BUY": ("buy", "buy"),
    "SELL": ("sell", "sell"),
    "DIVIDEND": ("cash", "dividend"),
    "INTEREST": ("cash", "interest"),
    "DEPOSIT": ("cash", "deposit"),
    "WITHDRAWAL": ("cash", "withdrawal"),
    "CONTRIBUTION": ("cash", "contribution"),
    "TRANSFER": ("transfer", "transfer"),
    "FEE": ("fee", "fee"),
    "TAX": ("fee", "tax"),
    "SUBSTITUTE_DIVIDEND": ("cash", "substitute_dividend"),
    "REI": ("cash", "rei"),
    "STOCK_DIVIDEND": ("transfer", "stock distribution"),
    "OPTIONEXPIRATION": ("cash", "optionexpiration"),
    "OPTIONASSIGNMENT": ("cash", "optionassignment"),
    "OPTIONEXERCISE": ("transfer", "exercise"),
    "EXTERNAL_ASSET_TRANSFER_IN": ("cash", "external_asset_transfer_in"),
    "EXTERNAL_ASSET_TRANSFER_OUT": ("cash", "external_asset_transfer_out"),
    "SPLIT": ("cash", "split"),
}


def _classify_activity(raw_type: str) -> tuple[str, str | None]:
    normalized = raw_type.upper()
    classification = _ACTIVITY_MAP.get(normalized)
    if classification is not None:
        return classification
    # SnapTrade documents this enum as open-ended and may return the
    # brokerage's raw label. Preserve that row without promoting it to
    # Plaid's cash/transfer types; downstream cash-flow logic will leave it
    # unclassified until an explicit rule or owner decision exists.
    preserved = normalized.lower()
    return preserved, preserved


# ---- low-level helpers --------------------------------------------------


def _body(response: object) -> Any:
    """Pull the parsed body off whatever shape the SDK returns.

    The SDK exposes responses with a `.body` attribute that's a `dict` for
    JSON object responses, a `list` for JSON array responses, or a primitive.
    """
    body = getattr(response, "body", None)
    if body is None:
        return cast(Any, response)
    return body


def _snaptrade_activity_page(body: object) -> tuple[list[Mapping[str, Any]], int]:
    if not isinstance(body, Mapping):
        raise ProviderPayloadError("SnapTrade activity response is not a paginated object")
    payload = cast("Mapping[str, object]", body)
    rows_value = payload.get("data")
    pagination_value = payload.get("pagination")
    if not isinstance(rows_value, list) or not isinstance(pagination_value, Mapping):
        raise ProviderDeliveryIncompleteError(
            "SnapTrade activity response is missing pagination metadata"
        )
    rows = cast("list[object]", rows_value)
    pagination = cast("Mapping[str, object]", pagination_value)
    total = pagination.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ProviderPayloadError("SnapTrade pagination total is invalid")
    if any(not isinstance(row, Mapping) for row in rows):
        raise ProviderPayloadError("SnapTrade activity page contains an invalid row")
    return cast("list[Mapping[str, Any]]", rows), total


def _snaptrade_list_payload(body: object, *, label: str) -> list[Any]:
    if isinstance(body, list):
        return cast("list[Any]", body)
    if isinstance(body, Mapping):
        rows = cast("Mapping[str, object]", body).get("data")
        if isinstance(rows, list):
            return cast("list[Any]", rows)
    raise ProviderPayloadError(f"SnapTrade {label} response is not a list")


def _snaptrade_available_cash(raw_balances: object, account_currency: str) -> Decimal | None:
    """Sum direct cash balances in the account's reporting currency only."""
    if not isinstance(raw_balances, list):
        return None
    values: list[Decimal] = []
    for raw_balance in cast("list[object]", raw_balances):
        if not isinstance(raw_balance, Mapping):
            continue
        balance = cast("Mapping[str, object]", raw_balance)
        raw_currency = balance.get("currency")
        currency: object | None
        if isinstance(raw_currency, Mapping):
            currency = cast("Mapping[str, object]", raw_currency).get("code")
        else:
            currency = raw_currency
        if str(currency or "") != account_currency:
            continue
        value = _to_decimal(balance.get("cash"))
        if value is not None:
            values.append(value)
    return sum(values, Decimal(0)) if values else None


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _opt_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        # SnapTrade returns ISO strings, sometimes with time component.
        return date.fromisoformat(value[:10])
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


def _opt_bool(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("provider boolean field has invalid type")
    return value


def _dig(payload: object, path: list[str]) -> object | None:
    current: object = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = cast("dict[str, object]", current).get(key)
        if current is None:
            return None
    return current
