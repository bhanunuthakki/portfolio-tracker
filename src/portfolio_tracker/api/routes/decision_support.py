"""Decision-support endpoints: pre-trade thesis log, trade tags, earnings.

Three independent surfaces, one file because they're all CRUD on small
tables and share the same patterns. The Dashboard imports each via its
own React Query hook.

  * `/api/decisions` — pre-trade thesis log. POST creates a decision
    BEFORE the user goes to Robinhood; PUT lets them attach an outcome
    later. Listing is reverse-chronological.
  * `/api/trade-tags` — behavioral tags on holding windows. Designed to
    let the user mine their own patterns ("how often did I panic-sell?").
  * `/api/earnings` — upcoming earnings dates for held tickers. Reads
    from the `earnings_calendar` table the daily refresh populates.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from portfolio_tracker.db import get_session
from portfolio_tracker.models import (
    EarningsCalendar,
    HoldingSnapshot,
    Security,
    TradeDecision,
    TradeTag,
)

router = APIRouter(prefix="/api", tags=["decision-support"])


# ---------------------------------------------------------------------------
# Trade decisions (pre-trade thesis log)
# ---------------------------------------------------------------------------


class DecisionIn(BaseModel):
    decision_date: date
    ticker: str = Field(..., min_length=1, max_length=16)
    action: str = Field(..., pattern=r"^(buy|sell|add|trim|hold)$")
    thesis: str = Field(..., min_length=10)
    expected_outcome: str | None = None
    invalidation_triggers: str | None = None
    position_size_pct: float | None = Field(default=None, ge=0, le=100)
    time_horizon_months: int | None = Field(default=None, ge=0, le=120)
    confidence: str | None = Field(default=None, pattern=r"^(low|medium|high)$")
    notes: str | None = None
    # Optional link to a research brief in earnings-summary. Stored as a
    # relative path like "research/NU/2026-05-13_report.html" so it
    # resolves through portfolio-tracker's brief-passthrough route.
    linked_brief_path: str | None = Field(default=None, max_length=512)


class DecisionOutcomeIn(BaseModel):
    outcome_date: date
    outcome_status: str = Field(..., pattern=r"^(validated|invalidated|partial|open)$")
    outcome_notes: str | None = None


class DecisionOut(BaseModel):
    decision_id: int
    decision_date: date
    ticker: str
    action: str
    thesis: str
    expected_outcome: str | None
    invalidation_triggers: str | None
    position_size_pct: Decimal | None
    time_horizon_months: int | None
    confidence: str | None
    notes: str | None
    outcome_date: date | None
    outcome_status: str | None
    outcome_notes: str | None
    linked_brief_path: str | None
    created_at: datetime
    updated_at: datetime


@router.post(
    "/decisions",
    response_model=DecisionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_decision(
    payload: DecisionIn,
    session: Annotated[Session, Depends(get_session)],
) -> DecisionOut:
    decision = TradeDecision(
        decision_date=payload.decision_date,
        ticker=payload.ticker.upper().strip(),
        action=payload.action,
        thesis=payload.thesis.strip(),
        expected_outcome=payload.expected_outcome.strip() if payload.expected_outcome else None,
        invalidation_triggers=payload.invalidation_triggers.strip()
        if payload.invalidation_triggers
        else None,
        position_size_pct=Decimal(str(payload.position_size_pct))
        if payload.position_size_pct is not None
        else None,
        time_horizon_months=payload.time_horizon_months,
        confidence=payload.confidence,
        notes=payload.notes.strip() if payload.notes else None,
        linked_brief_path=payload.linked_brief_path.strip() if payload.linked_brief_path else None,
    )
    session.add(decision)
    session.commit()
    session.refresh(decision)
    return _decision_to_out(decision)


@router.get("/decisions", response_model=list[DecisionOut])
def list_decisions(
    session: Annotated[Session, Depends(get_session)],
    ticker: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[DecisionOut]:
    query = select(TradeDecision).order_by(
        desc(TradeDecision.decision_date), desc(TradeDecision.decision_id)
    )
    if ticker:
        query = query.where(TradeDecision.ticker == ticker.upper().strip())
    query = query.limit(limit)
    rows = session.execute(query).scalars().all()
    return [_decision_to_out(r) for r in rows]


@router.put("/decisions/{decision_id}/outcome", response_model=DecisionOut)
def attach_outcome(
    decision_id: int,
    payload: DecisionOutcomeIn,
    session: Annotated[Session, Depends(get_session)],
) -> DecisionOut:
    decision = session.get(TradeDecision, decision_id)
    if decision is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"decision {decision_id} not found")
    decision.outcome_date = payload.outcome_date
    decision.outcome_status = payload.outcome_status
    decision.outcome_notes = (
        payload.outcome_notes.strip() if payload.outcome_notes else None
    )
    decision.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(decision)
    return _decision_to_out(decision)


@router.delete("/decisions/{decision_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_decision(
    decision_id: int,
    session: Annotated[Session, Depends(get_session)],
) -> None:
    decision = session.get(TradeDecision, decision_id)
    if decision is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"decision {decision_id} not found")
    session.delete(decision)
    session.commit()


def _decision_to_out(d: TradeDecision) -> DecisionOut:
    return DecisionOut(
        decision_id=d.decision_id,
        decision_date=d.decision_date,
        ticker=d.ticker,
        action=d.action,
        thesis=d.thesis,
        expected_outcome=d.expected_outcome,
        invalidation_triggers=d.invalidation_triggers,
        position_size_pct=d.position_size_pct,
        time_horizon_months=d.time_horizon_months,
        confidence=d.confidence,
        notes=d.notes,
        outcome_date=d.outcome_date,
        outcome_status=d.outcome_status,
        outcome_notes=d.outcome_notes,
        linked_brief_path=d.linked_brief_path,
        created_at=d.created_at,
        updated_at=d.updated_at,
    )


# ---------------------------------------------------------------------------
# Trade tags (behavioral pattern labels)
# ---------------------------------------------------------------------------

# Curated tag vocabulary the UI presents as a dropdown. Free-text via
# `notes` is allowed for nuance, but limiting the tag itself to a known
# list makes pattern-mining tractable later.
ALLOWED_TAGS: list[str] = [
    "panic_sold",
    "held_too_long",
    "bought_too_high",
    "bought_with_no_thesis",
    "thesis_validated",
    "thesis_broken",
    "rebalancing_trim",
    "tax_loss_harvest",
    "fomo_buy",
    "good_entry",
    "good_exit",
]


class TradeTagIn(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=16)
    period_start: date
    period_end: date | None = None
    tag: str = Field(..., min_length=1, max_length=32)
    notes: str | None = None


class TradeTagOut(BaseModel):
    tag_id: int
    ticker: str
    period_start: date
    period_end: date | None
    tag: str
    notes: str | None
    created_at: datetime


@router.get("/trade-tags/vocabulary", response_model=list[str])
def tag_vocabulary() -> list[str]:
    """Surfaced to the UI so the dropdown stays in sync with the backend."""
    return ALLOWED_TAGS


@router.post(
    "/trade-tags", response_model=TradeTagOut, status_code=status.HTTP_201_CREATED
)
def create_tag(
    payload: TradeTagIn,
    session: Annotated[Session, Depends(get_session)],
) -> TradeTagOut:
    if payload.tag not in ALLOWED_TAGS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"tag must be one of {ALLOWED_TAGS}",
        )
    tag = TradeTag(
        ticker=payload.ticker.upper().strip(),
        period_start=payload.period_start,
        period_end=payload.period_end,
        tag=payload.tag,
        notes=payload.notes.strip() if payload.notes else None,
    )
    session.add(tag)
    session.commit()
    session.refresh(tag)
    return _tag_to_out(tag)


@router.get("/trade-tags", response_model=list[TradeTagOut])
def list_tags(
    session: Annotated[Session, Depends(get_session)],
    ticker: str | None = Query(default=None),
) -> list[TradeTagOut]:
    query = select(TradeTag).order_by(desc(TradeTag.created_at))
    if ticker:
        query = query.where(TradeTag.ticker == ticker.upper().strip())
    return [_tag_to_out(r) for r in session.execute(query).scalars().all()]


@router.delete("/trade-tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tag(
    tag_id: int, session: Annotated[Session, Depends(get_session)]
) -> None:
    tag = session.get(TradeTag, tag_id)
    if tag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"tag {tag_id} not found")
    session.delete(tag)
    session.commit()


def _tag_to_out(t: TradeTag) -> TradeTagOut:
    return TradeTagOut(
        tag_id=t.tag_id,
        ticker=t.ticker,
        period_start=t.period_start,
        period_end=t.period_end,
        tag=t.tag,
        notes=t.notes,
        created_at=t.created_at,
    )


# ---------------------------------------------------------------------------
# Earnings calendar (read-only — populated by the daily-refresh job)
# ---------------------------------------------------------------------------


class EarningsRow(BaseModel):
    ticker: str
    name: str | None
    earnings_date: date
    earnings_time: str | None
    eps_estimate: Decimal | None
    eps_actual: Decimal | None
    revenue_estimate: Decimal | None
    revenue_actual: Decimal | None
    days_until: int  # negative = past
    held_qty: Decimal | None
    held_value: Decimal | None


@router.get("/earnings/upcoming", response_model=list[EarningsRow])
def upcoming_earnings(
    session: Annotated[Session, Depends(get_session)],
    days_ahead: int = Query(default=30, ge=1, le=365),
    only_held: bool = Query(default=True, description="Filter to currently-held tickers."),
) -> list[EarningsRow]:
    """Return earnings dates within the lookforward window.

    `only_held=True` (default) restricts to tickers that appear in the
    most recent `holdings_snapshots` row. The Dashboard surface uses
    that mode to keep the calendar focused on positions you actually
    care about; setting it false exposes the whole upcoming calendar
    if you want to scan candidate names.
    """
    today = date.today()
    horizon = today + timedelta(days=days_ahead)

    held: dict[str, tuple[Decimal, Decimal]] = {}
    if only_held:
        latest = session.execute(
            select(HoldingSnapshot.snapshot_date)
            .order_by(HoldingSnapshot.snapshot_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is None:
            return []
        rows = session.execute(
            select(
                Security.ticker,
                HoldingSnapshot.quantity,
                HoldingSnapshot.institution_value,
            )
            .join(Security, Security.security_id == HoldingSnapshot.security_id)
            .where(HoldingSnapshot.snapshot_date == latest)
            .where(Security.is_cash_equivalent.is_(False))
            .where(Security.ticker.is_not(None))
        ).all()
        for ticker, qty, val in rows:
            qty_decimal = Decimal(qty) if qty is not None else Decimal(0)
            val_decimal = Decimal(val) if val is not None else Decimal(0)
            prior_qty, prior_val = held.get(ticker, (Decimal(0), Decimal(0)))
            held[ticker] = (prior_qty + qty_decimal, prior_val + val_decimal)

    query = (
        select(EarningsCalendar, Security)
        .outerjoin(Security, Security.ticker == EarningsCalendar.ticker)
        .where(EarningsCalendar.earnings_date >= today)
        .where(EarningsCalendar.earnings_date <= horizon)
        .order_by(EarningsCalendar.earnings_date)
    )
    if only_held:
        if not held:
            return []
        query = query.where(EarningsCalendar.ticker.in_(list(held.keys())))

    out: list[EarningsRow] = []
    seen: set[tuple[str, date]] = set()
    for row in session.execute(query).all():
        ec = row[0]
        sec = row[1]
        key = (ec.ticker, ec.earnings_date)
        if key in seen:
            # Multi-row Security with same ticker (Plaid + SnapTrade dupe).
            continue
        seen.add(key)
        held_qty, held_val = held.get(ec.ticker, (None, None))
        out.append(
            EarningsRow(
                ticker=ec.ticker,
                name=sec.name if sec is not None else None,
                earnings_date=ec.earnings_date,
                earnings_time=ec.earnings_time,
                eps_estimate=ec.eps_estimate,
                eps_actual=ec.eps_actual,
                revenue_estimate=ec.revenue_estimate,
                revenue_actual=ec.revenue_actual,
                days_until=(ec.earnings_date - today).days,
                held_qty=held_qty,
                held_value=held_val,
            )
        )
    return out
