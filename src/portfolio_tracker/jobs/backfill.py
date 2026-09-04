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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

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
from portfolio_tracker.provider_delivery import (
    ProviderDeliveryMetadata,
    ProviderPayloadError,
    canonical_normalized_record_set_sha256,
)
from portfolio_tracker.services.provider_transaction_corrections import (
    ProviderTransactionCorrectionApproval,
)
from portfolio_tracker.services.provider_transaction_provenance import (
    ProviderAccountTransactionCapture,
    persist_provider_account_attestation_with_correction_approvals,
)

PLAID_INVESTMENT_TX_RETENTION_DAYS = 730  # 24 months


@dataclass(frozen=True)
class BackfillAccountDeliveryReceipt:
    """An account-local slice of a count-verified provider delivery."""

    account_id: int
    source_row_count: int
    record_set_sha256: str


@dataclass(frozen=True)
class BackfillItemDeliveryReceipt:
    """Non-sensitive evidence retained by callers that need to attest a run."""

    item_id: int
    rows_written: int
    delivery: ProviderDeliveryMetadata
    accounts: tuple[BackfillAccountDeliveryReceipt, ...]


@dataclass(frozen=True)
class BackfillRunResult:
    rows_written: int
    item_receipts: tuple[BackfillItemDeliveryReceipt, ...]


def run(start_date: date | None = None, end_date: date | None = None) -> int:
    """Compatibility entry point returning only newly inserted row count."""
    return run_with_delivery_receipts(start_date=start_date, end_date=end_date).rows_written


def run_with_delivery_receipts(
    start_date: date | None = None,
    end_date: date | None = None,
    *,
    transaction_correction_approvals: Mapping[str, ProviderTransactionCorrectionApproval]
    | None = None,
) -> BackfillRunResult:
    """Backfill and return count/digest receipts without private transaction values."""
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=PLAID_INVESTMENT_TX_RETENTION_DAYS)

    rows_written = 0
    receipts: list[BackfillItemDeliveryReceipt] = []
    with SessionLocal() as session:
        items = (
            session.execute(select(Item).where(Item.source == ItemSource.PLAID.value))
            .scalars()
            .all()
        )
        for item in items:
            rows_written += _backfill_item(
                session,
                item,
                start_date,
                end_date,
                delivery_receipts=receipts,
                transaction_correction_approvals=transaction_correction_approvals,
            )
        session.commit()
    return BackfillRunResult(rows_written=rows_written, item_receipts=tuple(receipts))


def _backfill_item(
    session: Session,
    item: Item,
    start_date: date,
    end_date: date,
    *,
    delivery_receipts: list[BackfillItemDeliveryReceipt] | None = None,
    transaction_correction_approvals: Mapping[str, ProviderTransactionCorrectionApproval]
    | None = None,
) -> int:
    """Compatibility helper returning only newly inserted row count."""
    receipt = _backfill_item_with_delivery_receipt(
        session,
        item,
        start_date,
        end_date,
        transaction_correction_approvals=transaction_correction_approvals,
    )
    if receipt is None:
        return 0
    if delivery_receipts is not None:
        delivery_receipts.append(receipt)
    return receipt.rows_written


def _backfill_item_with_delivery_receipt(
    session: Session,
    item: Item,
    start_date: date,
    end_date: date,
    *,
    transaction_correction_approvals: Mapping[str, ProviderTransactionCorrectionApproval]
    | None = None,
) -> BackfillItemDeliveryReceipt | None:
    if item.plaid_access_token_encrypted is None:
        return None
    access_token = decrypt_token(item.plaid_access_token_encrypted)
    response = plaid_client.get_investment_transactions(access_token, start_date, end_date)
    captured_at = datetime.now(UTC)
    if response.delivery is None or not response.delivery.is_complete:
        raise ProviderPayloadError("Plaid backfill response is missing a complete delivery receipt")

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
            raise ProviderPayloadError(
                "Plaid transaction references an account outside the validated account set"
            )
        security_id = (
            plaid_security_to_id.get(tx.plaid_security_id)
            if tx.plaid_security_id is not None
            else None
        )
        if tx.plaid_security_id is not None and security_id is None:
            raise ProviderPayloadError(
                "Plaid transaction references a security outside the validated security set"
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

    # The provenance writer flushes the normalized transaction parents before
    # inserting FK-dependent Source events and decisions. This is required in
    # production because SessionLocal has autoflush disabled.
    for provider_account_id, account_id in sorted(plaid_account_to_id.items()):
        persist_provider_account_attestation_with_correction_approvals(
            session,
            ProviderAccountTransactionCapture(
                account_id=account_id,
                provider_account_id=provider_account_id,
                coverage_start=start_date,
                coverage_end=end_date,
                delivery=response.delivery,
                transactions=tuple(
                    transaction
                    for transaction in response.transactions
                    if transaction.plaid_account_id == provider_account_id
                ),
                security_ids_by_provider_id=plaid_security_to_id,
                captured_at=captured_at,
            ),
            transaction_correction_approvals,
        )

    account_receipts: list[BackfillAccountDeliveryReceipt] = []
    for plaid_account_id, account_id in sorted(plaid_account_to_id.items()):
        account_transactions = [
            tx for tx in response.transactions if tx.plaid_account_id == plaid_account_id
        ]
        account_receipts.append(
            BackfillAccountDeliveryReceipt(
                account_id=account_id,
                source_row_count=len(account_transactions),
                record_set_sha256=canonical_normalized_record_set_sha256(
                    [tx.model_dump(mode="json") for tx in account_transactions]
                ),
            )
        )
    return BackfillItemDeliveryReceipt(
        item_id=item.item_id,
        rows_written=rows_written,
        delivery=response.delivery,
        accounts=tuple(account_receipts),
    )


if __name__ == "__main__":
    written = run()
    print(f"backfill complete: {written} new transactions stored")
