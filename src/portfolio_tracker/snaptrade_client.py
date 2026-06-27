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
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from typing import Any, cast

from pydantic import BaseModel
from snaptrade_client import SnapTrade

from portfolio_tracker.config import get_settings
from portfolio_tracker.plaid_client import (
    PlaidAccount,
    PlaidHolding,
    PlaidInvestmentTransaction,
    PlaidSecurity,
)


class SnapTradeNotConfiguredError(RuntimeError):
    """Raised when SnapTrade endpoints are called without env vars set."""


class SnapTradeUserCredentials(BaseModel):
    user_id: str
    user_secret: str


class SnapTradeBrokerageAuthorization(BaseModel):
    authorization_id: str
    brokerage_name: str | None = None


class SnapTradeHoldingsResponse(BaseModel):
    accounts: list[PlaidAccount]
    securities: list[PlaidSecurity]
    holdings: list[PlaidHolding]


class SnapTradeTransactionsResponse(BaseModel):
    accounts: list[PlaidAccount]
    securities: list[PlaidSecurity]
    transactions: list[PlaidInvestmentTransaction]


_client: SnapTrade | None = None


def get_client() -> SnapTrade:
    global _client
    if _client is not None:
        return _client
    settings = get_settings()
    if settings.snaptrade_client_id is None or settings.snaptrade_consumer_key is None:
        raise SnapTradeNotConfiguredError(
            "Set SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY in .env to enable SnapTrade."
        )
    _client = SnapTrade(
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
    response = get_client().authentication.login_snap_trade_user(**kwargs)
    body = _body(response)
    redirect = cast(object, body.get("redirectURI") if isinstance(body, dict) else body)
    if not isinstance(redirect, str):
        raise RuntimeError(f"unexpected SnapTrade login response shape: {body!r}")
    return redirect


def list_brokerage_authorizations(
    creds: SnapTradeUserCredentials,
) -> list[SnapTradeBrokerageAuthorization]:
    response = get_client().connections.list_brokerage_authorizations(
        user_id=creds.user_id, user_secret=creds.user_secret
    )
    body = _body(response)
    items = cast("list[Any]", body if isinstance(body, list) else body.get("data", []))
    out: list[SnapTradeBrokerageAuthorization] = []
    for raw in items:
        out.append(
            SnapTradeBrokerageAuthorization(
                authorization_id=str(raw["id"]),
                brokerage_name=_opt_str(_dig(raw, ["brokerage", "name"])),
            )
        )
    return out


def list_user_accounts(
    creds: SnapTradeUserCredentials,
    authorization_id: str | None = None,
) -> list[tuple[str, PlaidAccount]]:
    """Return [(snaptrade_account_id, normalized_account), ...].

    If `authorization_id` is provided, only accounts under that connection
    are returned.
    """
    response = get_client().account_information.list_user_accounts(
        user_id=creds.user_id, user_secret=creds.user_secret
    )
    body = _body(response)
    rows = cast("list[Any]", body if isinstance(body, list) else body.get("data", []))
    out: list[tuple[str, PlaidAccount]] = []
    for raw in rows:
        if (
            authorization_id is not None
            and str(raw.get("brokerage_authorization", "")) != authorization_id
        ):
            continue
        account_id = str(raw["id"])
        out.append((account_id, _account_from_snaptrade(raw)))
    return out


def get_holdings(
    creds: SnapTradeUserCredentials, snaptrade_account_id: str
) -> SnapTradeHoldingsResponse:
    response = get_client().account_information.get_user_holdings(
        user_id=creds.user_id,
        user_secret=creds.user_secret,
        account_id=snaptrade_account_id,
    )
    body = _body(response)
    accounts = [_account_from_snaptrade(body["account"])] if "account" in body else []
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
    }
    response = get_client().account_information.get_account_activities(**activity_kwargs)
    body = _body(response)
    rows = cast("list[Any]", body if isinstance(body, list) else body.get("data", []))
    securities: dict[str, PlaidSecurity] = {}
    transactions: list[PlaidInvestmentTransaction] = []
    for raw in rows:
        symbol_payload = raw.get("symbol") or raw.get("option_symbol")
        plaid_security_id: str | None = None
        if isinstance(symbol_payload, dict):
            sec = _security_from_snaptrade(cast("dict[str, Any]", symbol_payload))
            securities[sec.plaid_security_id] = sec
            plaid_security_id = sec.plaid_security_id
        tx = _transaction_from_snaptrade(raw, snaptrade_account_id, plaid_security_id)
        if tx is not None:
            transactions.append(tx)
    return SnapTradeTransactionsResponse(
        accounts=[],
        securities=list(securities.values()),
        transactions=transactions,
    )


# ---- normalization helpers ----------------------------------------------


def _account_from_snaptrade(raw: dict[str, Any]) -> PlaidAccount:
    snaptrade_account_id = str(raw["id"])
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
    )


def _security_from_snaptrade(raw: dict[str, Any]) -> PlaidSecurity:
    symbol = raw.get("symbol")
    symbol_dict = cast("dict[str, Any]", symbol if isinstance(symbol, dict) else raw)
    symbol_id = str(symbol_dict.get("id") or symbol_dict.get("symbol") or "unknown")
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
) -> PlaidInvestmentTransaction | None:
    tx_id = raw.get("id") or raw.get("trade_date")
    if tx_id is None:
        return None
    tx_date = _opt_date(raw.get("trade_date") or raw.get("settlement_date"))
    if tx_date is None:
        return None
    raw_type = str(raw.get("type", "")).upper()
    canonical_type, subtype = _classify_activity(raw_type)
    return PlaidInvestmentTransaction(
        plaid_investment_transaction_id=f"snaptrade:{tx_id}",
        plaid_account_id=plaid_account_id,
        plaid_security_id=plaid_security_id,
        date=tx_date,
        name=_opt_str(raw.get("description")),
        quantity=_to_decimal(raw.get("units")) or Decimal(0),
        amount=_to_decimal(raw.get("amount")) or Decimal(0),
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
}


def _classify_activity(raw_type: str) -> tuple[str, str | None]:
    return _ACTIVITY_MAP.get(raw_type.upper(), ("cash", raw_type.lower() or None))


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
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        # SnapTrade returns ISO strings, sometimes with time component.
        return date.fromisoformat(value[:10])
    return None


def _dig(payload: object, path: list[str]) -> object | None:
    current: object = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = cast("dict[str, object]", current).get(key)
        if current is None:
            return None
    return current
