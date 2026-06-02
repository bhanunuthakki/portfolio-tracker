"""Cockpit endpoints — the decision-support action surface.

Slice 1 exposes the grounded, dollar-weighted signal set (deterministic
coaching red-flags + earnings-summary thesis / valuation / alert readers).
The ranked action queue (Opus) and the accept / dismiss / snooze lifecycle
land in later slices.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from portfolio_tracker.db import get_session
from portfolio_tracker.services.cockpit import (
    ActionItem,
    Signal,
    gather_signals,
    rank_signals,
)

router = APIRouter(prefix="/api/cockpit", tags=["cockpit"])


@router.get("/signals", response_model=list[Signal])
def get_signals(db: Annotated[Session, Depends(get_session)]) -> list[Signal]:
    """Grounded, dollar-weighted signals for the current portfolio."""
    return gather_signals(db)


@router.get("/queue", response_model=list[ActionItem])
def get_queue(
    db: Annotated[Session, Depends(get_session)],
    use_llm: bool = True,
) -> list[ActionItem]:
    """Ranked, advisory action queue over the grounded signals.

    `use_llm=true` (default) routes an Opus ranking pass; `use_llm=false`
    returns the fast deterministic queue with no LLM call. (Persistence and
    event-driven regeneration arrive in slice 3 — this ranks on demand.)
    """
    return rank_signals(gather_signals(db), use_llm=use_llm)
