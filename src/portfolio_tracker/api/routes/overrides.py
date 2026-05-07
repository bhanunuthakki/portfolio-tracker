"""User-managed overrides for fields the aggregator didn't supply.

Two override types:
  * cost-basis — total dollars paid for a position in a specific account.
    Used when Plaid/SnapTrade returns NULL cost_basis.
  * ticker     — yfinance-compatible symbol for a security Plaid couldn't
    ticker. Re-run `jobs/prices.py` after setting one to populate history.

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
    Security,
    TickerOverride,
)
from portfolio_tracker.schemas import (
    CostBasisOverrideIn,
    CostBasisOverrideOut,
    TickerOverrideIn,
    TickerOverrideOut,
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
        )
        session.add(existing)
    else:
        existing.total_cost_basis = body.total_cost_basis
        existing.notes = body.notes
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
