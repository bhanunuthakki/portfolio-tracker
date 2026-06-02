"""Cockpit endpoints — the decision-support action surface.

- GET  /signals                 grounded, dollar-weighted signals (slice 1)
- GET  /queue                   the persisted, ranked action queue (active items)
- POST /queue/refresh           regenerate + upsert the queue (Opus unless use_llm=false)
- POST /items/{id}/accept       log a commitment
- POST /items/{id}/dismiss      drop the item (sticks across refreshes)
- POST /items/{id}/snooze       hide for N days
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from portfolio_tracker.db import get_session
from portfolio_tracker.services.cockpit import (
    QueueItemOut,
    Signal,
    accept_item,
    dismiss_item,
    gather_signals,
    list_queue,
    refresh_queue,
    snooze_item,
)

router = APIRouter(prefix="/api/cockpit", tags=["cockpit"])


@router.get("/signals", response_model=list[Signal])
def get_signals(db: Annotated[Session, Depends(get_session)]) -> list[Signal]:
    """Grounded, dollar-weighted signals for the current portfolio."""
    return gather_signals(db)


@router.get("/queue", response_model=list[QueueItemOut])
def get_queue(db: Annotated[Session, Depends(get_session)]) -> list[QueueItemOut]:
    """The persisted, active action queue (open + accepted), ranked."""
    return list_queue(db)


@router.post("/queue/refresh", response_model=list[QueueItemOut])
def post_refresh(
    db: Annotated[Session, Depends(get_session)],
    use_llm: bool = True,
) -> list[QueueItemOut]:
    """Regenerate the queue and upsert it, preserving accept/dismiss/snooze.

    `use_llm=false` skips the Opus pass for a fast deterministic refresh.
    """
    return refresh_queue(db, use_llm=use_llm)


@router.post("/items/{item_id}/accept", response_model=QueueItemOut)
def post_accept(item_id: int, db: Annotated[Session, Depends(get_session)]) -> QueueItemOut:
    try:
        return accept_item(db, item_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/items/{item_id}/dismiss", response_model=QueueItemOut)
def post_dismiss(item_id: int, db: Annotated[Session, Depends(get_session)]) -> QueueItemOut:
    try:
        return dismiss_item(db, item_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/items/{item_id}/snooze", response_model=QueueItemOut)
def post_snooze(
    item_id: int,
    db: Annotated[Session, Depends(get_session)],
    days: int = 7,
) -> QueueItemOut:
    try:
        return snooze_item(db, item_id, days=days)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
