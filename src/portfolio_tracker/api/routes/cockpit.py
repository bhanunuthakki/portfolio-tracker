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
from portfolio_tracker.services.cockpit import Signal, gather_signals

router = APIRouter(prefix="/api/cockpit", tags=["cockpit"])


@router.get("/signals", response_model=list[Signal])
def get_signals(db: Annotated[Session, Depends(get_session)]) -> list[Signal]:
    """Grounded, dollar-weighted signals for the current portfolio."""
    return gather_signals(db)
