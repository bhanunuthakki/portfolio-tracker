"""SnapTrade auth + sync endpoints.

Three-step flow:
  1. POST /api/snaptrade/connection-portal-url?profile=primary|spouse
       Mints (or reuses) a SnapTrade user for the profile, returns a
       single-use portal URL the frontend opens in a new tab.
  2. The user completes brokerage login on SnapTrade's portal, which
       redirects them back. Our app does not need a callback handler —
       SnapTrade has already attached the connection to the user_id.
  3. POST /api/snaptrade/sync?profile=primary|spouse
       Lists the SnapTrade user's brokerage authorizations and creates
       (or updates) one Item per connection in our DB. Holdings + 24mo of
       activity are pulled in the same request to populate the rest of
       the pipeline.

The user_id we register with SnapTrade is derived deterministically from
the profile so re-runs are idempotent: `local-{profile}-snaptrade`.
"""

from __future__ import annotations

from datetime import date, timedelta
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from portfolio_tracker import snaptrade_client
from portfolio_tracker.crypto import decrypt_token, encrypt_token
from portfolio_tracker.db import get_session
from portfolio_tracker.jobs._helpers import upsert_account, upsert_security
from portfolio_tracker.models import (
    INVESTMENT_ACCOUNT_TYPES,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    ItemSource,
    SnapTradeUser,
    TransactionExclusion,
)
from portfolio_tracker.snaptrade_client import (
    SnapTradeNotConfiguredError,
    SnapTradeUserCredentials,
)

# How far back to reach the FIRST time we pull an account's transactions.
# 10 years is SnapTrade's documented ceiling; brokers return whatever subset
# they hold (observed: Robinhood back to 2021, Fidelity BrokerageLink only to
# account opening). Used once per account — see the call site for why the
# steady-state 730-day window is the wrong default for a first pull.
_FIRST_PULL_LOOKBACK_DAYS = 3650

router = APIRouter(prefix="/api/snaptrade", tags=["snaptrade"])


class SnapTradeProfile(StrEnum):
    PRIMARY = "primary"
    SPOUSE = "spouse"


_PROFILE_USER_IDS: dict[SnapTradeProfile, str] = {
    SnapTradeProfile.PRIMARY: "local-primary-snaptrade",
    SnapTradeProfile.SPOUSE: "local-spouse-snaptrade",
}


class ConnectionPortalUrlOut(BaseModel):
    url: str


class SyncResultOut(BaseModel):
    profile: SnapTradeProfile
    items_synced: int
    accounts_synced: int
    holdings_written: int
    transactions_written: int


@router.get("/status")
def status_endpoint() -> dict[str, bool]:
    return {"configured": snaptrade_client.is_configured()}


@router.post("/connection-portal-url", response_model=ConnectionPortalUrlOut)
def connection_portal_url(
    session: Annotated[Session, Depends(get_session)],
    profile: Annotated[SnapTradeProfile, Query()] = SnapTradeProfile.PRIMARY,
) -> ConnectionPortalUrlOut:
    creds = _ensure_user_registered(session, profile)
    try:
        url = snaptrade_client.login_url(creds)
    except SnapTradeNotConfiguredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return ConnectionPortalUrlOut(url=url)


@router.post("/sync", response_model=SyncResultOut)
def sync(
    session: Annotated[Session, Depends(get_session)],
    profile: Annotated[SnapTradeProfile, Query()] = SnapTradeProfile.PRIMARY,
    lookback_days: Annotated[
        int,
        Query(
            ge=1,
            le=3650,
            description=(
                "How far back to pull transaction activity. Default 730 (24 months) "
                "matches Plaid's retention so daily syncs don't redo work. The migration "
                "script bumps this to 3650 (10 years) for a one-shot deep pull."
            ),
        ),
    ] = 730,
) -> SyncResultOut:
    creds = _load_credentials(session, profile)
    if creds is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no SnapTrade user registered for profile={profile.value}; open the portal first",
        )

    authorizations = snaptrade_client.list_brokerage_authorizations(creds)
    items_synced = 0
    accounts_synced = 0
    holdings_written = 0
    transactions_written = 0
    today = date.today()
    backfill_start = today - timedelta(days=lookback_days)

    for auth in authorizations:
        item = _upsert_item(session, profile, creds, auth)
        items_synced += 1

        accounts = snaptrade_client.list_user_accounts(creds, auth.authorization_id)
        for snaptrade_account_id, plaid_shaped_account in accounts:
            if plaid_shaped_account.type not in INVESTMENT_ACCOUNT_TYPES:
                continue
            account = upsert_account(session, item.item_id, plaid_shaped_account)
            accounts_synced += 1

            holdings_resp = snaptrade_client.get_holdings(creds, snaptrade_account_id)
            for sec in holdings_resp.securities:
                upsert_security(session, sec)
            session.flush()

            # Clear today's snapshot rows for this account before writing the
            # fresh set (mirrors jobs/snapshot.py). A merge-only upsert would
            # leave a phantom row for any position the user fully exited
            # between two same-day syncs — the new response never names it, so
            # the stale row would survive and inflate today's value.
            session.execute(
                delete(HoldingSnapshot)
                .where(HoldingSnapshot.snapshot_date == today)
                .where(HoldingSnapshot.account_id == account.account_id)
            )

            for h in holdings_resp.holdings:
                security = next(
                    (
                        s
                        for s in holdings_resp.securities
                        if s.plaid_security_id == h.plaid_security_id
                    ),
                    None,
                )
                if security is None:
                    continue
                from portfolio_tracker.models import Security

                stored_security = session.execute(
                    select(Security).where(Security.plaid_security_id == security.plaid_security_id)
                ).scalar_one()
                snap = HoldingSnapshot(
                    snapshot_date=today,
                    account_id=account.account_id,
                    security_id=stored_security.security_id,
                    quantity=h.quantity,
                    institution_price=h.institution_price,
                    institution_value=h.institution_value,
                    cost_basis=h.cost_basis,
                    currency=h.currency,
                )
                session.merge(snap)
                holdings_written += 1

            # An account we've never pulled transactions for gets the DEEP
            # window, not the steady-state one.
            #
            # `lookback_days` defaults to 730 to match Plaid's retention so a
            # daily sync doesn't redo work. That's right for an account whose
            # history is already loaded, and wrong for one that just appeared:
            # the deep pull existed only inside the one-shot migration script,
            # so every account linked after that migration silently kept
            # whatever the last 730 days happened to contain, forever. Nothing
            # ever went back for the rest, because each subsequent sync asked
            # for the same 730 days and found it already stored.
            #
            # The cost of getting this wrong is invisible and permanent: the
            # walk-back can only reconstruct history as far as the transactions
            # reach, and contributions before that point are absent from the
            # cashflow series — where an unrecorded deposit is arithmetically
            # indistinguishable from an investment gain. Running the deep pull
            # on 2026-07-30 recovered 525 transactions across the SnapTrade
            # accounts, taking Robinhood Individual from 8 rows to 451 (back to
            # 2021-06-24) and raising 2024's recorded net contributions from
            # $4,867 to $27,117.
            #
            # Idempotent ingest makes this self-limiting: the deep window is
            # used once per account, on the sync that first sees it.
            first_pull = (
                session.execute(
                    select(func.count())
                    .select_from(InvestmentTransaction)
                    .where(InvestmentTransaction.account_id == account.account_id)
                ).scalar_one()
                == 0
            )
            account_start = (
                today - timedelta(days=_FIRST_PULL_LOOKBACK_DAYS) if first_pull else backfill_start
            )
            activities = snaptrade_client.get_account_activities(
                creds, snaptrade_account_id, account_start, today
            )
            for sec in activities.securities:
                upsert_security(session, sec)
            session.flush()
            for tx in activities.transactions:
                from portfolio_tracker.models import Security as Sec

                security_id: int | None = None
                if tx.plaid_security_id is not None:
                    stored = session.execute(
                        select(Sec).where(Sec.plaid_security_id == tx.plaid_security_id)
                    ).scalar_one_or_none()
                    if stored is not None:
                        security_id = stored.security_id
                existing = session.get(InvestmentTransaction, tx.plaid_investment_transaction_id)
                if existing is not None:
                    continue
                # Deliberately-removed rows stay removed across re-syncs.
                if (
                    session.get(TransactionExclusion, tx.plaid_investment_transaction_id)
                    is not None
                ):
                    continue
                session.add(
                    InvestmentTransaction(
                        plaid_investment_transaction_id=tx.plaid_investment_transaction_id,
                        account_id=account.account_id,
                        security_id=security_id,
                        date=tx.date,
                        name=tx.name,
                        quantity=tx.quantity,
                        amount=tx.amount,
                        price=tx.price,
                        fees=tx.fees,
                        type=tx.type,
                        subtype=tx.subtype,
                        currency=tx.currency,
                    )
                )
                transactions_written += 1

    session.commit()

    # Refresh today's row in the daily-value cache. SnapTrade is the source
    # of truth for Fidelity (and any other broker only on the SnapTrade
    # side), so without this the chart would lag a day until the next
    # daily-values cron tick.
    from portfolio_tracker.jobs import daily_values

    today = date.today()
    daily_values.run(start_date=today, end_date=today)

    return SyncResultOut(
        profile=profile,
        items_synced=items_synced,
        accounts_synced=accounts_synced,
        holdings_written=holdings_written,
        transactions_written=transactions_written,
    )


def _ensure_user_registered(
    session: Session, profile: SnapTradeProfile
) -> SnapTradeUserCredentials:
    """Register the SnapTrade user for `profile` if needed and persist the
    secret IMMEDIATELY. Without persistence, opening the portal generates
    a secret we never see again and `sync` later 404s.

    If a previous run registered the user but lost the secret (early bug
    pre-`snaptrade_users` table), `register_user` will fail with "user
    already exists". We auto-recover by deleting and re-registering — but
    this WIPES brokerage connections, so the user must re-link via portal.
    """
    existing = _load_credentials(session, profile)
    if existing is not None:
        return existing
    target_user_id = _PROFILE_USER_IDS[profile]

    try:
        creds = snaptrade_client.register_user(target_user_id)
    except SnapTradeNotConfiguredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except Exception as exc:
        if not snaptrade_client.is_user_already_exists_error(exc):
            raise
        # Lost-secret recovery path. Destructive on SnapTrade's side.
        creds = snaptrade_client.recover_user(target_user_id)

    record = SnapTradeUser(
        user_id=creds.user_id,
        user_secret_encrypted=encrypt_token(creds.user_secret),
    )
    session.add(record)
    session.commit()
    return creds


def _load_credentials(
    session: Session, profile: SnapTradeProfile
) -> SnapTradeUserCredentials | None:
    target_user_id = _PROFILE_USER_IDS[profile]
    record = session.get(SnapTradeUser, target_user_id)
    if record is None:
        return None
    return SnapTradeUserCredentials(
        user_id=target_user_id,
        user_secret=decrypt_token(record.user_secret_encrypted),
    )


def _upsert_item(
    session: Session,
    profile: SnapTradeProfile,
    creds: SnapTradeUserCredentials,
    auth: snaptrade_client.SnapTradeBrokerageAuthorization,
) -> Item:
    existing = session.execute(
        select(Item).where(Item.snaptrade_authorization_id == auth.authorization_id)
    ).scalar_one_or_none()
    if existing is not None:
        existing.institution_name = auth.brokerage_name
        existing.snaptrade_user_secret_encrypted = encrypt_token(creds.user_secret)
        return existing
    item = Item(
        source=ItemSource.SNAPTRADE.value,
        institution_name=auth.brokerage_name,
        snaptrade_user_id=creds.user_id,
        snaptrade_user_secret_encrypted=encrypt_token(creds.user_secret),
        snaptrade_authorization_id=auth.authorization_id,
    )
    session.add(item)
    session.flush()
    return item
