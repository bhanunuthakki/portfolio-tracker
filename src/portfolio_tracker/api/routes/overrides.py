"""User-managed overrides for fields the aggregator didn't supply.

Three override types:
  * cost-basis  — total dollars paid for a position in a specific account.
    Used when Plaid/SnapTrade returns NULL cost_basis.
  * ticker      — yfinance-compatible symbol for a security Plaid couldn't
    ticker. Re-run `jobs/prices.py` after setting one to populate history.
  * transaction — per-tx cashflow classification override. Lets the user
    force a row to count as contribution / withdrawal / internal so the
    contribution number on the chart matches their mental model.

All endpoints are idempotent (PUT semantics — set or replace).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.db import get_session
from portfolio_tracker.models import (
    Account,
    CostBasisOverride,
    InvestmentTransaction,
    Security,
    TickerOverride,
    TransactionOverride,
)
from portfolio_tracker.schemas import (
    CostBasisOverrideIn,
    CostBasisOverrideOut,
    TickerOverrideIn,
    TickerOverrideOut,
    TransactionOverrideIn,
    TransactionOverrideOut,
)

_VALID_TX_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"external_in", "external_out", "internal"}
)

router = APIRouter(prefix="/api/overrides", tags=["overrides"])


# ---- cost basis -----------------------------------------------------------


@router.get("/cost-basis", response_model=list[CostBasisOverrideOut])
def list_cost_basis_overrides(
    session: Annotated[Session, Depends(get_session)],
) -> list[CostBasisOverrideOut]:
    rows = session.execute(
        select(CostBasisOverride, Account, Security)
        .join(Account, Account.account_id == CostBasisOverride.account_id)
        .join(Security, Security.security_id == CostBasisOverride.security_id)
        .order_by(Account.name, Security.ticker)
    ).all()
    return [
        CostBasisOverrideOut(
            account_id=ov.account_id,
            security_id=ov.security_id,
            account_name=a.name,
            ticker=s.ticker,
            security_name=s.name,
            total_cost_basis=ov.total_cost_basis,
            notes=ov.notes,
            source=ov.source,
            updated_at=ov.updated_at,
        )
        for ov, a, s in rows
    ]


@router.put("/cost-basis", response_model=CostBasisOverrideOut)
def upsert_cost_basis_override(
    body: CostBasisOverrideIn,
    session: Annotated[Session, Depends(get_session)],
) -> CostBasisOverrideOut:
    if body.total_cost_basis < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "total_cost_basis must be non-negative"
        )
    account = session.get(Account, body.account_id)
    security = session.get(Security, body.security_id)
    if account is None or security is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account or security not found")

    existing = session.get(CostBasisOverride, (body.account_id, body.security_id))
    if existing is None:
        existing = CostBasisOverride(
            account_id=body.account_id,
            security_id=body.security_id,
            total_cost_basis=body.total_cost_basis,
            notes=body.notes,
            source="manual",
        )
        session.add(existing)
    else:
        # PUT through the API is always a manual edit; promote source to
        # "manual" so a later inference script doesn't quietly overwrite
        # the user's chosen value.
        existing.total_cost_basis = body.total_cost_basis
        existing.notes = body.notes
        existing.source = "manual"
    session.commit()
    session.refresh(existing)
    return CostBasisOverrideOut(
        account_id=existing.account_id,
        security_id=existing.security_id,
        account_name=account.name,
        ticker=security.ticker,
        security_name=security.name,
        total_cost_basis=existing.total_cost_basis,
        notes=existing.notes,
        source=existing.source,
        updated_at=existing.updated_at,
    )


@router.delete(
    "/cost-basis/{account_id}/{security_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_cost_basis_override(
    account_id: int,
    security_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> None:
    record = session.get(CostBasisOverride, (account_id, security_id))
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    session.delete(record)
    session.commit()


# ---- ticker --------------------------------------------------------------


@router.get("/ticker", response_model=list[TickerOverrideOut])
def list_ticker_overrides(
    session: Annotated[Session, Depends(get_session)],
) -> list[TickerOverrideOut]:
    rows = session.execute(
        select(TickerOverride, Security)
        .join(Security, Security.security_id == TickerOverride.security_id)
        .order_by(Security.name)
    ).all()
    return [
        TickerOverrideOut(
            security_id=ov.security_id,
            plaid_security_id=s.plaid_security_id,
            security_name=s.name,
            ticker=ov.ticker,
            notes=ov.notes,
            updated_at=ov.updated_at,
        )
        for ov, s in rows
    ]


@router.put("/ticker", response_model=TickerOverrideOut)
def upsert_ticker_override(
    body: TickerOverrideIn,
    session: Annotated[Session, Depends(get_session)],
) -> TickerOverrideOut:
    ticker = body.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ticker is required")
    security = session.get(Security, body.security_id)
    if security is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "security not found")

    existing = session.get(TickerOverride, body.security_id)
    if existing is None:
        existing = TickerOverride(
            security_id=body.security_id, ticker=ticker, notes=body.notes
        )
        session.add(existing)
    else:
        existing.ticker = ticker
        existing.notes = body.notes
    session.commit()
    session.refresh(existing)
    return TickerOverrideOut(
        security_id=existing.security_id,
        plaid_security_id=security.plaid_security_id,
        security_name=security.name,
        ticker=existing.ticker,
        notes=existing.notes,
        updated_at=existing.updated_at,
    )


@router.delete("/ticker/{security_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ticker_override(
    security_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> None:
    record = session.get(TickerOverride, security_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    session.delete(record)
    session.commit()


# ---- transaction classification ------------------------------------------


def _serialize_tx_override(
    ov: TransactionOverride,
    tx: InvestmentTransaction | None,
    account: Account | None,
    security: Security | None,
) -> TransactionOverrideOut:
    return TransactionOverrideOut(
        plaid_investment_transaction_id=ov.plaid_investment_transaction_id,
        classification=ov.classification,
        notes=ov.notes,
        updated_at=ov.updated_at,
        tx_date=tx.date if tx is not None else None,
        tx_type=tx.type if tx is not None else None,
        tx_subtype=tx.subtype if tx is not None else None,
        tx_amount=tx.amount if tx is not None else None,
        account_name=account.name if account is not None else None,
        ticker=security.ticker if security is not None else None,
    )


@router.get("/transactions", response_model=list[TransactionOverrideOut])
def list_transaction_overrides(
    session: Annotated[Session, Depends(get_session)],
) -> list[TransactionOverrideOut]:
    rows = session.execute(
        select(TransactionOverride, InvestmentTransaction, Account, Security)
        .join(
            InvestmentTransaction,
            InvestmentTransaction.plaid_investment_transaction_id
            == TransactionOverride.plaid_investment_transaction_id,
            isouter=True,
        )
        .join(
            Account,
            Account.account_id == InvestmentTransaction.account_id,
            isouter=True,
        )
        .join(
            Security,
            Security.security_id == InvestmentTransaction.security_id,
            isouter=True,
        )
        .order_by(InvestmentTransaction.date.desc())
    ).all()
    return [_serialize_tx_override(ov, tx, a, s) for ov, tx, a, s in rows]


@router.put("/transactions", response_model=TransactionOverrideOut)
def upsert_transaction_override(
    input: TransactionOverrideIn,
    session: Annotated[Session, Depends(get_session)],
) -> TransactionOverrideOut:
    if input.classification not in _VALID_TX_CLASSIFICATIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                f"classification must be one of "
                f"{sorted(_VALID_TX_CLASSIFICATIONS)}; got '{input.classification}'"
            ),
        )
    tx = session.get(InvestmentTransaction, input.plaid_investment_transaction_id)
    if tx is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"No transaction with id={input.plaid_investment_transaction_id}",
        )
    existing = session.get(TransactionOverride, input.plaid_investment_transaction_id)
    if existing is None:
        existing = TransactionOverride(
            plaid_investment_transaction_id=input.plaid_investment_transaction_id,
            classification=input.classification,
            notes=input.notes,
        )
        session.add(existing)
    else:
        existing.classification = input.classification
        existing.notes = input.notes
    session.commit()
    session.refresh(existing)
    account = session.get(Account, tx.account_id) if tx else None
    security = session.get(Security, tx.security_id) if tx and tx.security_id else None
    return _serialize_tx_override(existing, tx, account, security)


@router.delete(
    "/transactions/{plaid_investment_transaction_id:path}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_transaction_override(
    plaid_investment_transaction_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> None:
    record = session.get(TransactionOverride, plaid_investment_transaction_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    session.delete(record)
    session.commit()
