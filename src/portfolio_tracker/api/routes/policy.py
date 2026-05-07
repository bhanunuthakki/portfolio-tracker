"""Policy-portfolio target allocation endpoints.

The user's policy weights define the synthetic "what if I'd just stuck to
my IPS" benchmark. Returns are computed by building a synthetic portfolio
that started with the same V_start and received the same cashflows as the
actual portfolio — but invested in the policy mix.

Editing weights triggers a benchmarks-job re-run on the next chart load
(the prices for new tickers get pulled lazily). Setting a weight to 0 is
the way to remove a ticker.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from portfolio_tracker.db import get_session
from portfolio_tracker.models import PolicyWeight
from portfolio_tracker.schemas import (
    PolicyOut,
    PolicyWeightIn,
    PolicyWeightOut,
)

router = APIRouter(prefix="/api/policy", tags=["policy"])

# Total weights are considered "balanced" when within this tolerance of 100%.
_BALANCE_TOLERANCE_PCT = Decimal("0.01")


@router.get("", response_model=PolicyOut)
def get_policy(session: Annotated[Session, Depends(get_session)]) -> PolicyOut:
    rows = session.execute(
        select(PolicyWeight).order_by(PolicyWeight.ticker)
    ).scalars().all()
    weights = [_to_out(r) for r in rows]
    total = sum((w.weight_pct for w in weights), Decimal(0))
    return PolicyOut(
        weights=weights,
        total_pct=total,
        is_balanced=abs(total - Decimal(100)) <= _BALANCE_TOLERANCE_PCT,
    )


@router.put("", response_model=PolicyOut)
def replace_policy(
    body: list[PolicyWeightIn],
    session: Annotated[Session, Depends(get_session)],
) -> PolicyOut:
    """Replace the entire policy in one shot. PUT semantics — the request
    body is the complete new state. Tickers not in the body are removed.
    """
    seen: set[str] = set()
    cleaned: list[tuple[str, int, str | None]] = []
    for row in body:
        ticker = row.ticker.strip().upper()
        if not ticker:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "ticker must be non-empty"
            )
        if ticker in seen:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"duplicate ticker {ticker} in policy",
            )
        if row.weight_pct < 0:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"weight for {ticker} must be non-negative",
            )
        seen.add(ticker)
        cleaned.append((ticker, _pct_to_bps(row.weight_pct), row.notes))

    session.execute(delete(PolicyWeight))
    for ticker, bps, notes in cleaned:
        session.add(PolicyWeight(ticker=ticker, weight_bps=bps, notes=notes))
    session.commit()
    return get_policy(session)


def _to_out(row: PolicyWeight) -> PolicyWeightOut:
    return PolicyWeightOut(
        ticker=row.ticker,
        weight_pct=_bps_to_pct(row.weight_bps),
        notes=row.notes,
        updated_at=row.updated_at,
    )


def _bps_to_pct(bps: int) -> Decimal:
    return (Decimal(bps) / Decimal(100)).quantize(Decimal("0.01"))


def _pct_to_bps(pct: Decimal) -> int:
    return int((pct * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
