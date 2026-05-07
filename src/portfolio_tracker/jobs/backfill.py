"""24-month investment-transaction backfill for Plaid-sourced Items.

Pulls every transaction Plaid will give us per Item and stores them. The
performance service later uses these (combined with `prices`) to reconstruct
historical portfolio value. Fully idempotent — `INSERT OR IGNORE` semantics
via SQLAlchemy upsert on the natural primary key.

SnapTrade-sourced Items are skipped here — their transactions are written
inline by the SnapTrade sync flow (`/api/snaptrade/sync`), which uses
SnapTrade's own `get_account_activities` (often longer history than Plaid).

Run manually:
    python -m portfolio_tracker.jobs.backfill
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker import plaid_client
from portfolio_tracker.crypto import decrypt_token
from portfolio_tracker.db import SessionLocal
from portfolio_tracker.jobs._helpers import (
    is_investment_account,
    upsert_account,
    upsert_security,
)
from portfolio_tracker.models import InvestmentTransaction, Item, ItemSource

PLAID_INVESTMENT_TX_RETENTION_DAYS = 730  # 24 months


def run(start_date: date | None = None, end_date: date | None = None) -> int:
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=PLAID_INVESTMENT_TX_RETENTION_DAYS)

    rows_written = 0
    with SessionLocal() as session:
        items = session.execute(
            select(Item).where(Item.source == ItemSource.PLAID.value)
        ).scalars().all()
        for item in items:
            rows_written += _backfill_item(session, item, start_date, end_date)
        session.commit()
    return rows_written


def _backfill_item(session: Session, item: Item, start_date: date, end_date: date) -> int:
    if item.plaid_access_token_encrypted is None:
        return 0
    access_token = decrypt_token(item.plaid_access_token_encrypted)
    response = plaid_client.get_investment_transactions(access_token, start_date, end_date)

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

    rows_written = 0
    for tx in response.transactions:
        account_id = plaid_account_to_id.get(tx.plaid_account_id)
        if account_id is None:
            continue
        security_id = (
            plaid_security_to_id.get(tx.plaid_security_id)
            if tx.plaid_security_id is not None
            else None
        )
        existing = session.get(InvestmentTransaction, tx.plaid_investment_transaction_id)
        if existing is not None:
            continue
        session.add(
            InvestmentTransaction(
                plaid_investment_transaction_id=tx.plaid_investment_transaction_id,
                account_id=account_id,
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
        rows_written += 1
    return rows_written


if __name__ == "__main__":
    written = run()
    print(f"backfill complete: {written} new transactions stored")
