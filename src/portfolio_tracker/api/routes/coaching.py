"""CIO coaching endpoint.

Exposes `/api/coaching/tips` — a read-only view over the user's portfolio
state that emits structured red-flag tips against the rubric in
`CIO_CONTEXT.md`. The endpoint takes no parameters; the rubric is
deterministic on the current `holdings_snapshots` row and the lifetime
transaction log.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from portfolio_tracker.db import get_session
from portfolio_tracker.services.coaching import (
    CoachingResult,
    generate_coaching_tips,
)

router = APIRouter(prefix="/api/coaching", tags=["coaching"])


@router.get("/tips", response_model=CoachingResult)
def get_coaching_tips(
    session: Annotated[Session, Depends(get_session)],
) -> CoachingResult:
    """Return structured CIO coaching tips for the current portfolio.

    See `services/coaching.py` for the rubric. The rubric numbers are
    echoed back on every response so the frontend can render them
    alongside the tip list without re-reading the markdown file.
    """
    return generate_coaching_tips(session)
