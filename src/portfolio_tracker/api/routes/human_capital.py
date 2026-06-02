"""Human-capital overlap bucket assignments — CRUD over `human_capital_overlap`.

The CIO coaching engine sums portfolio-wide weighted exposure per bucket
and fires `concentration_human_capital_aggregate` when total exposure
exceeds the bucket's cap (caps live in `services/coaching.py`). This
endpoint lets the user edit the (ticker, bucket, weight) assignments
from the Accounts page; the engine reads them on every /tips request.

PUT semantics on the per-ticker endpoint: the request body is the
complete new state for that ticker. Bucket rows not in the body are
removed for that ticker; rows present are upserted.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session

from portfolio_tracker.db import get_session
from portfolio_tracker.models import HumanCapitalOverlap
from portfolio_tracker.schemas import (
    HumanCapitalBucketWeightOut,
    HumanCapitalOverlapIn,
    HumanCapitalOverlapOut,
    HumanCapitalOverlapsOut,
)
from portfolio_tracker.services.coaching import _BUCKET_CAPS  # pyright: ignore[reportPrivateUsage]

router = APIRouter(prefix="/api/human-capital", tags=["human-capital"])


@router.get("/overlaps", response_model=HumanCapitalOverlapsOut)
def list_overlaps(
    session: Annotated[Session, Depends(get_session)],
) -> HumanCapitalOverlapsOut:
    rows = (
        session.execute(
            select(HumanCapitalOverlap).order_by(
                HumanCapitalOverlap.ticker, HumanCapitalOverlap.bucket
            )
        )
        .scalars()
        .all()
    )

    by_ticker: dict[str, list[HumanCapitalBucketWeightOut]] = {}
    for r in rows:
        by_ticker.setdefault(r.ticker, []).append(
            HumanCapitalBucketWeightOut(
                bucket=r.bucket,
                weight_pct=r.weight_pct,
                notes=r.notes,
                updated_at=r.updated_at,
            )
        )
    overlaps = [HumanCapitalOverlapOut(ticker=t, buckets=bs) for t, bs in by_ticker.items()]
    return HumanCapitalOverlapsOut(
        overlaps=overlaps,
        bucket_caps_pct={b: Decimal(c) for b, c in _BUCKET_CAPS.items()},
    )


@router.put("/overlaps", response_model=HumanCapitalOverlapOut)
def upsert_overlap(
    body: HumanCapitalOverlapIn,
    session: Annotated[Session, Depends(get_session)],
) -> HumanCapitalOverlapOut:
    """Replace all bucket rows for the given ticker.

    PUT semantics — supplying an empty `buckets` list removes all rows
    for the ticker. Duplicate bucket names in the body are rejected.
    Negative weights are rejected; >100 is allowed (e.g., a user could
    intentionally over-weight a name to model 1.2× leverage exposure).
    """
    ticker = body.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ticker must be non-empty")

    seen: set[str] = set()
    cleaned: list[tuple[str, Decimal, str | None]] = []
    for row in body.buckets:
        bucket = row.bucket.strip()
        if not bucket:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "bucket must be non-empty")
        if bucket in seen:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"duplicate bucket {bucket} for ticker {ticker}",
            )
        if row.weight_pct < 0:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"weight for {ticker}/{bucket} must be non-negative",
            )
        seen.add(bucket)
        cleaned.append((bucket, row.weight_pct, row.notes))

    session.execute(delete(HumanCapitalOverlap).where(HumanCapitalOverlap.ticker == ticker))
    for bucket, weight_pct, notes in cleaned:
        session.add(
            HumanCapitalOverlap(
                ticker=ticker,
                bucket=bucket,
                weight_pct=weight_pct,
                notes=notes,
            )
        )
    session.commit()

    rows = (
        session.execute(
            select(HumanCapitalOverlap)
            .where(HumanCapitalOverlap.ticker == ticker)
            .order_by(HumanCapitalOverlap.bucket)
        )
        .scalars()
        .all()
    )
    return HumanCapitalOverlapOut(
        ticker=ticker,
        buckets=[
            HumanCapitalBucketWeightOut(
                bucket=r.bucket,
                weight_pct=r.weight_pct,
                notes=r.notes,
                updated_at=r.updated_at,
            )
            for r in rows
        ],
    )


@router.delete(
    "/overlaps/{ticker}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_overlap(
    ticker: str,
    session: Annotated[Session, Depends(get_session)],
) -> None:
    """Remove all bucket assignments for one ticker."""
    upper = ticker.strip().upper()
    if not upper:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ticker required")
    result = session.execute(delete(HumanCapitalOverlap).where(HumanCapitalOverlap.ticker == upper))
    if cast("CursorResult[Any]", result).rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    session.commit()
