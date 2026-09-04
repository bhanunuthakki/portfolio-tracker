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

from datetime import UTC, date, datetime

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
from portfolio_tracker.models import AccountValuationSourceKind, HoldingSnapshot, Item, ItemSource
from portfolio_tracker.services.account_valuations import (
    NewAccountValuationObservation,
    canonical_account_balance_source_sha256,
    record_account_valuation_observation,
)


def run() -> int:
    """Snapshot every linked Item. Returns the number of holdings rows written."""
    snapshot_date = date.today()
    rows_written = 0
    with SessionLocal() as session:
        items = (
            session.execute(select(Item).where(Item.source == ItemSource.PLAID.value))
            .scalars()
            .all()
        )
        for item in items:
            rows_written += _snapshot_item(session, item, snapshot_date)
        session.commit()

    # Cache today's total in `portfolio_values_daily` so the chart reads
    # don't have to recompute it on every request. Imported lazily to keep
    # snapshot.py free of the performance-service dependency at module load.
    from portfolio_tracker.jobs import daily_values

    daily_values.run(start_date=snapshot_date, end_date=snapshot_date)

    return rows_written


def _snapshot_item(session: Session, item: Item, snapshot_date: date) -> int:
    if item.plaid_access_token_encrypted is None:
        return 0
    access_token = decrypt_token(item.plaid_access_token_encrypted)
    response = plaid_client.get_holdings(access_token)
    fetched_at = datetime.now(UTC)

    plaid_account_to_id: dict[str, int] = {}
    for plaid_account in response.accounts:
        if not is_investment_account(plaid_account):
            continue
        account = upsert_account(session, item.item_id, plaid_account)
        plaid_account_to_id[plaid_account.plaid_account_id] = account.account_id

    holding_account_ids = {holding.plaid_account_id for holding in response.holdings}
    for plaid_account in response.accounts:
        account_id = plaid_account_to_id.get(plaid_account.plaid_account_id)
        total_value = plaid_account.provider_total_value
        if account_id is None or total_value is None:
            continue
        cash_value = plaid_account.provider_available_cash
        balance_as_of = plaid_account.provider_balance_as_of
        valuation_date = balance_as_of.date() if balance_as_of is not None else snapshot_date
        has_exact_provider_as_of = balance_as_of is not None
        source_reference = "investments/holdings/get.accounts[].balances"
        if balance_as_of is not None:
            source_reference += ".last_updated_datetime"
        else:
            # Plaid documents holdings balances as potentially cached. The
            # fetch timestamp is evidence of receipt, not a fabricated balance
            # as-of timestamp when the provider omits last_updated_datetime.
            source_reference += ";cached_as_fetched_no_provider_as_of"
        is_empty = (
            has_exact_provider_as_of
            and total_value == 0
            and (cash_value is None or cash_value == 0)
            and plaid_account.plaid_account_id not in holding_account_ids
        )
        record_account_valuation_observation(
            session,
            NewAccountValuationObservation(
                account_id=account_id,
                as_of_date=valuation_date,
                as_of_at=balance_as_of,
                total_value=total_value,
                cash_value=cash_value,
                currency=plaid_account.currency.upper(),
                source_kind=AccountValuationSourceKind.PROVIDER_API,
                source_provider="plaid",
                source_reference=source_reference,
                source_record_id=plaid_account.plaid_account_id,
                source_payload_sha256=canonical_account_balance_source_sha256(
                    source_provider="plaid",
                    provider_account_id=plaid_account.plaid_account_id,
                    source_reference=source_reference,
                    as_of_date=valuation_date,
                    total_value=total_value,
                    cash_value=cash_value,
                    currency=plaid_account.currency.upper(),
                ),
                fetched_at=fetched_at,
                is_complete=has_exact_provider_as_of,
                is_empty=is_empty,
            ),
        )

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

    item.last_refreshed_at = fetched_at
    return rows_written


if __name__ == "__main__":
    written = run()
    print(f"snapshot complete: {written} holdings rows written for {date.today()}")
