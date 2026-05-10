"""Shared helpers for ingest jobs (upserts for accounts, securities)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import INVESTMENT_ACCOUNT_TYPES, Account, Security
from portfolio_tracker.plaid_client import PlaidAccount, PlaidSecurity

# Money-market funds whose NAV is essentially fixed at $1/share. We mark
# them as cash equivalents so the historical valuation in the backfill
# uses face value × quantity instead of asking yfinance for prices that
# don't meaningfully exist.
_KNOWN_MONEY_MARKET_TICKERS: frozenset[str] = frozenset(
    {
        "FDRXX",  # Fidelity Government Cash Reserves
        "SPAXX",  # Fidelity Government Money Market
        "FZFXX",  # Fidelity Treasury Money Market
        "VMFXX",  # Vanguard Federal Money Market
        "SWVXX",  # Schwab Value Advantage Money
        "CASH",
    }
)


def _is_cash_equivalent(plaid_security: PlaidSecurity) -> bool:
    if plaid_security.is_cash_equivalent:
        return True
    ticker = (plaid_security.ticker or "").upper()
    return ticker in _KNOWN_MONEY_MARKET_TICKERS


def is_investment_account(plaid_account: PlaidAccount) -> bool:
    """Whether to persist this account. Non-investment accounts are dropped."""
    return plaid_account.type in INVESTMENT_ACCOUNT_TYPES


def upsert_account(session: Session, item_id: int, plaid_account: PlaidAccount) -> Account:
    existing = session.execute(
        select(Account).where(Account.plaid_account_id == plaid_account.plaid_account_id)
    ).scalar_one_or_none()
    if existing is not None:
        existing.name = plaid_account.name
        existing.official_name = plaid_account.official_name
        existing.type = plaid_account.type
        existing.subtype = plaid_account.subtype
        existing.mask = plaid_account.mask
        existing.currency = plaid_account.currency
        return existing
    account = Account(
        item_id=item_id,
        plaid_account_id=plaid_account.plaid_account_id,
        name=plaid_account.name,
        official_name=plaid_account.official_name,
        type=plaid_account.type,
        subtype=plaid_account.subtype,
        mask=plaid_account.mask,
        currency=plaid_account.currency,
    )
    session.add(account)
    session.flush()
    return account


def upsert_security(session: Session, plaid_security: PlaidSecurity) -> Security:
    """Find-or-create a Security row for an aggregator-supplied identity.

    Lookup precedence:
      1. **`plaid_security_id`** (the aggregator's internal ID) — exact
         match. The fast path for "I've seen this exact security row
         before from this aggregator."
      2. **`ticker`** — fall back when the aggregator's ID isn't in the
         table yet but a row with the same ticker exists from a *different*
         aggregator. Without this, both Plaid and SnapTrade ingest paths
         create separate rows for the same instrument (NU, VTI, etc.):
         transactions and prices land under different IDs and the walk-back
         drops positions whose `security_id` happens to be the one without
         pulled prices. We adopt the existing row, repoint
         `plaid_security_id` to whichever aggregator we're currently
         seeing, and refresh metadata. Tickers without a value (None or
         empty) skip step 2.
    """
    existing = session.execute(
        select(Security).where(Security.plaid_security_id == plaid_security.plaid_security_id)
    ).scalar_one_or_none()
    if existing is None and plaid_security.ticker:
        existing = session.execute(
            select(Security).where(Security.ticker == plaid_security.ticker)
        ).scalar_one_or_none()
    if existing is not None:
        # Last-writer-wins on metadata is fine — both aggregators report
        # essentially the same info. Repoint plaid_security_id only if
        # the row was found by ticker (different aggregator's view); the
        # column is unique, so we keep one canonical ID per row and
        # silently swap to whichever ID we're currently ingesting under.
        if existing.plaid_security_id != plaid_security.plaid_security_id:
            existing.plaid_security_id = plaid_security.plaid_security_id
        existing.ticker = plaid_security.ticker
        existing.cusip = plaid_security.cusip
        existing.isin = plaid_security.isin
        existing.name = plaid_security.name
        existing.type = plaid_security.type
        existing.currency = plaid_security.currency
        existing.is_cash_equivalent = _is_cash_equivalent(plaid_security)
        return existing
    security = Security(
        plaid_security_id=plaid_security.plaid_security_id,
        ticker=plaid_security.ticker,
        cusip=plaid_security.cusip,
        isin=plaid_security.isin,
        name=plaid_security.name,
        type=plaid_security.type,
        currency=plaid_security.currency,
        is_cash_equivalent=_is_cash_equivalent(plaid_security),
    )
    session.add(security)
    session.flush()
    return security
