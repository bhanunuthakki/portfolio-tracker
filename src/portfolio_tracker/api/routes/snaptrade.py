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

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from portfolio_tracker import snaptrade_client
from portfolio_tracker.crypto import decrypt_token, encrypt_token
from portfolio_tracker.db import get_session
from portfolio_tracker.jobs._helpers import upsert_account, upsert_security
from portfolio_tracker.models import (
    INVESTMENT_ACCOUNT_TYPES,
    AccountValuationSourceKind,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    ItemSource,
    SnapTradeUser,
)
from portfolio_tracker.plaid_client import PlaidAccount
from portfolio_tracker.provider_delivery import ProviderPayloadError
from portfolio_tracker.services.account_valuations import (
    NewAccountValuationObservation,
    canonical_account_balance_source_sha256,
    record_account_valuation_observation,
)
from portfolio_tracker.services.provider_transaction_corrections import (
    ProviderTransactionCorrectionApproval,
)
from portfolio_tracker.services.provider_transaction_provenance import (
    ProviderAccountTransactionCapture,
    ProviderHistoryGap,
    persist_provider_account_attestation_with_correction_approvals,
)
from portfolio_tracker.snaptrade_client import (
    SnapTradeNotConfiguredError,
    SnapTradeUserCredentials,
)

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
    return sync_with_transaction_correction_approvals(
        session,
        profile,
        lookback_days,
    )


def sync_with_transaction_correction_approvals(
    session: Session,
    profile: SnapTradeProfile = SnapTradeProfile.PRIMARY,
    lookback_days: int = 730,
    *,
    transaction_correction_approvals: Mapping[str, ProviderTransactionCorrectionApproval]
    | None = None,
) -> SyncResultOut:
    """Run the sync with optional exact plan-digest correction approvals.

    The public HTTP route never supplies this mapping. It is reserved for the
    local preview/backup/owner-approval workflow.
    """
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
            fetched_at = datetime.now(UTC)
            holdings_available = _holdings_are_available(auth, plaid_shaped_account)
            holdings_account = next(
                (
                    provider_account
                    for provider_account in holdings_resp.accounts
                    if provider_account.plaid_account_id == snaptrade_account_id
                ),
                None,
            )
            # In the pinned SDK, list_user_accounts owns account.balance.total;
            # get_user_holdings owns same-currency balances[].cash. Its nested
            # account object has no balance total, so combine the two direct
            # provider fields rather than choosing one response wholesale.
            total_value = plaid_shaped_account.provider_total_value
            if total_value is not None:
                cash_value = (
                    holdings_account.provider_available_cash
                    if holdings_account is not None
                    and holdings_account.provider_available_cash is not None
                    else plaid_shaped_account.provider_available_cash
                )
                source_reference = "account_information.list_user_accounts[].balance.total"
                if cash_value is not None:
                    source_reference += "+get_user_holdings.balances[].cash"
                balance_as_of = plaid_shaped_account.provider_holdings_last_successful_sync
                valuation_date = balance_as_of.date() if balance_as_of is not None else today
                has_exact_provider_as_of = balance_as_of is not None
                if balance_as_of is not None:
                    source_reference += ";as_of=sync_status.holdings.last_successful_sync"
                else:
                    # SnapTrade can return cached state without a reliable
                    # holdings timestamp. Fetch time is retained separately and
                    # must not be described as the broker-data as-of time.
                    source_reference += ";cached_as_fetched_no_provider_as_of"
                if not holdings_available:
                    source_reference += ";provider_state_unavailable"
                is_empty = (
                    holdings_available
                    and has_exact_provider_as_of
                    and total_value == 0
                    and (cash_value is None or cash_value == 0)
                    and not holdings_resp.holdings
                )
                currency = plaid_shaped_account.currency.upper()
                record_account_valuation_observation(
                    session,
                    NewAccountValuationObservation(
                        account_id=account.account_id,
                        as_of_date=valuation_date,
                        as_of_at=balance_as_of,
                        total_value=total_value,
                        cash_value=cash_value,
                        currency=currency,
                        source_kind=AccountValuationSourceKind.PROVIDER_API,
                        source_provider="snaptrade",
                        source_reference=source_reference,
                        source_record_id=snaptrade_account_id,
                        source_payload_sha256=canonical_account_balance_source_sha256(
                            source_provider="snaptrade",
                            provider_account_id=snaptrade_account_id,
                            source_reference=source_reference,
                            as_of_date=valuation_date,
                            total_value=total_value,
                            cash_value=cash_value,
                            currency=currency,
                        ),
                        fetched_at=fetched_at,
                        is_complete=holdings_available and has_exact_provider_as_of,
                        is_empty=is_empty,
                    ),
                )
            for sec in holdings_resp.securities:
                upsert_security(session, sec)
            session.flush()

            # Clear today's snapshot rows for this account before writing the
            # fresh set (mirrors jobs/snapshot.py). A merge-only upsert would
            # leave a phantom row for any position the user fully exited
            # between two same-day syncs — the new response never names it, so
            # the stale row would survive and inflate today's value.
            if holdings_available:
                session.execute(
                    delete(HoldingSnapshot)
                    .where(HoldingSnapshot.snapshot_date == today)
                    .where(HoldingSnapshot.account_id == account.account_id)
                )

            for h in holdings_resp.holdings if holdings_available else ():
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

            activities = snaptrade_client.get_account_activities(
                creds, snaptrade_account_id, backfill_start, today
            )
            activities_captured_at = datetime.now(UTC)
            if activities.delivery is None:
                raise ProviderPayloadError("SnapTrade activity delivery receipt is missing")
            snaptrade_security_to_id: dict[str, int] = {}
            for sec in activities.securities:
                stored_security = upsert_security(session, sec)
                snaptrade_security_to_id[sec.plaid_security_id] = stored_security.security_id
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
            persist_provider_account_attestation_with_correction_approvals(
                session,
                ProviderAccountTransactionCapture(
                    account_id=account.account_id,
                    provider_account_id=snaptrade_account_id,
                    coverage_start=backfill_start,
                    coverage_end=today,
                    delivery=activities.delivery,
                    transactions=tuple(activities.transactions),
                    security_ids_by_provider_id=snaptrade_security_to_id,
                    captured_at=activities_captured_at,
                    provider_history_gaps=_transaction_history_gaps(
                        auth,
                        plaid_shaped_account,
                        coverage_start=backfill_start,
                        coverage_end=today,
                    ),
                ),
                transaction_correction_approvals,
            )

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


def _holdings_are_available(
    auth: snaptrade_client.SnapTradeBrokerageAuthorization,
    account: PlaidAccount,
) -> bool:
    """Use only explicit provider failure states; do not invent staleness cutoffs."""

    return (
        auth.disabled is not True
        and account.provider_account_status != "unavailable"
        and account.provider_holdings_initial_sync_completed is not False
    )


def _transaction_history_gaps(
    auth: snaptrade_client.SnapTradeBrokerageAuthorization,
    account: PlaidAccount,
    *,
    coverage_start: date,
    coverage_end: date,
) -> tuple[ProviderHistoryGap, ...]:
    """Translate only documented SnapTrade sync facts into coverage gaps."""

    if (
        auth.disabled is True
        or account.provider_account_status == "unavailable"
        or account.provider_transactions_initial_sync_completed is False
    ):
        return (ProviderHistoryGap(coverage_start, coverage_end),)

    gaps: list[ProviderHistoryGap] = []
    first_known = account.provider_first_transaction_date
    if first_known is not None and first_known > coverage_start:
        gaps.append(
            ProviderHistoryGap(
                coverage_start,
                min(coverage_end, first_known - timedelta(days=1)),
            )
        )
    last_sync = account.provider_transactions_last_successful_sync
    if last_sync is not None and last_sync < coverage_end:
        gaps.append(
            ProviderHistoryGap(
                max(coverage_start, last_sync + timedelta(days=1)),
                coverage_end,
            )
        )
    return _coalesce_history_gaps(tuple(gap for gap in gaps if gap.start <= gap.end))


def _coalesce_history_gaps(
    gaps: tuple[ProviderHistoryGap, ...],
) -> tuple[ProviderHistoryGap, ...]:
    merged: list[ProviderHistoryGap] = []
    for gap in sorted(gaps, key=lambda value: (value.start, value.end)):
        if not merged or gap.start > merged[-1].end + timedelta(days=1):
            merged.append(gap)
            continue
        previous = merged[-1]
        merged[-1] = ProviderHistoryGap(previous.start, max(previous.end, gap.end))
    return tuple(merged)
