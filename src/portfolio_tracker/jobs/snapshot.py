"""Daily holdings snapshotter for Plaid-sourced Items.

Pulls /investments/holdings/get for every Plaid Item, writes one row per
(account, security) into `holdings_snapshots`. Idempotent per snapshot_date:
re-running the job for the same day overwrites rows rather than duplicating.

SnapTrade-sourced Items are skipped here — their snapshot is written
inline by the SnapTrade sync flow (`/api/snaptrade/sync`).

Run manually:
    python -m portfolio_tracker.jobs.snapshot
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from portfolio_tracker import plaid_client
from portfolio_tracker.crypto import decrypt_token
from portfolio_tracker.db import SessionLocal
from portfolio_tracker.jobs._helpers import (
    is_investment_account,
    upsert_account,
    upsert_security,
)
from portfolio_tracker.models import HoldingSnapshot, Item, ItemSource


def run() -> int:
    """Snapshot every linked Item. Returns the number of holdings rows written."""
    snapshot_date = date.today()
    rows_written = 0
    with SessionLocal() as session:
        items = session.execute(
            select(Item).where(Item.source == ItemSource.PLAID.value)
        ).scalars().all()
        for item in items:
            rows_written += _snapshot_item(session, item, snapshot_date)
        session.commit()
    return rows_written


def _snapshot_item(session: Session, item: Item, snapshot_date: date) -> int:
    if item.plaid_access_token_encrypted is None:
        return 0
    access_token = decrypt_token(item.plaid_access_token_encrypted)
    response = plaid_client.get_holdings(access_token)

    plaid_account_to_id: dict[str, int] = {}
    for plaid_account in response.accounts:
        if not is_investment_account(plaid_account):
            continue
        account = upsert_account(session, item.item_id, plaid_account)
        plaid_account_to_id[plaid_account.plaid_account_id] = account.account_id

    plaid_security_to_id: dict[str, int] = {}
    for plaid_security in response.securities:
        security = upsert_security(session, plaid_security)
        plaid_security_to_id[plaid_security.plaid_security_id] = security.security_id

    account_ids = list(plaid_account_to_id.values())
    if account_ids:
        session.execute(
            delete(HoldingSnapshot)
            .where(HoldingSnapshot.snapshot_date == snapshot_date)
            .where(HoldingSnapshot.account_id.in_(account_ids))
        )

    rows_written = 0
    for holding in response.holdings:
        account_id = plaid_account_to_id.get(holding.plaid_account_id)
        security_id = plaid_security_to_id.get(holding.plaid_security_id)
        if account_id is None or security_id is None:
            continue
        institution_value = holding.institution_value
        if institution_value is None and holding.institution_price is not None:
            institution_value = holding.quantity * holding.institution_price
        session.add(
            HoldingSnapshot(
                snapshot_date=snapshot_date,
                account_id=account_id,
                security_id=security_id,
                quantity=holding.quantity,
                institution_price=holding.institution_price,
                institution_value=institution_value,
                cost_basis=holding.cost_basis,
                currency=holding.currency,
            )
        )
        rows_written += 1

    item.last_refreshed_at = datetime.now(timezone.utc)
    return rows_written


if __name__ == "__main__":
    written = run()
    print(f"snapshot complete: {written} holdings rows written for {date.today()}")
