"""CIO advisor endpoints — chat sessions + monthly briefs.

All LLM calls are dispatched through `run_in_threadpool` because
`claude_cli.py` is a sync subprocess wrapper. Without the threadpool
hop the chat turn (~10–15s) or brief generation (~30–60s) would block
the FastAPI event loop and stall every other request.

Surfaces
========

* `POST /api/cio-advisor/sessions` — create a new chat thread.
* `GET  /api/cio-advisor/sessions` — list threads (newest first).
* `DELETE /api/cio-advisor/sessions/{id}` — drop a thread.
* `GET  /api/cio-advisor/sessions/{id}/turns` — load the transcript.
* `POST /api/cio-advisor/sessions/{id}/turns` — send a message + get reply.
* `POST /api/cio-advisor/briefs/generate` — generate or overwrite the
  current-month brief. Returns the full HTML body.
* `GET  /api/cio-advisor/briefs` — list briefs by period (newest first).
* `GET  /api/cio-advisor/briefs/{id}` — fetch one brief by id.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from portfolio_tracker.db import get_session
from portfolio_tracker.services.cio_advisor import (
    ChatSessionCreateIn,
    ChatSessionOut,
    ChatTurnIn,
    ChatTurnOut,
    MonthlyBriefOut,
    MonthlyBriefSummary,
    create_session,
    delete_session,
    generate_brief,
    get_brief,
    list_briefs,
    list_sessions,
    list_turns,
    send_turn,
)

router = APIRouter(prefix="/api/cio-advisor", tags=["cio-advisor"])


# ---------------------------------------------------------------------------
# Chat sessions
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=list[ChatSessionOut])
def get_sessions(
    db: Annotated[Session, Depends(get_session)],
) -> list[ChatSessionOut]:
    return list_sessions(db)


@router.post(
    "/sessions",
    response_model=ChatSessionOut,
    status_code=status.HTTP_201_CREATED,
)
def post_session(
    payload: ChatSessionCreateIn,
    db: Annotated[Session, Depends(get_session)],
) -> ChatSessionOut:
    return create_session(db, payload.title)


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def del_session(
    session_id: int,
    db: Annotated[Session, Depends(get_session)],
) -> None:
    delete_session(db, session_id)


@router.get(
    "/sessions/{session_id}/turns",
    response_model=list[ChatTurnOut],
)
def get_turns(
    session_id: int,
    db: Annotated[Session, Depends(get_session)],
) -> list[ChatTurnOut]:
    return list_turns(db, session_id)


class _TurnPostResponse(ChatTurnOut):
    """Returned shape: the assistant turn. The frontend already has the
    user's text — we don't need to round-trip it. But we DO need to send
    back the user turn's id so it can be uniquely keyed in the React
    transcript list. See `paired_turn_id`."""

    paired_turn_id: int


@router.post(
    "/sessions/{session_id}/turns",
    response_model=_TurnPostResponse,
)
async def post_turn(
    session_id: int,
    payload: ChatTurnIn,
    db: Annotated[Session, Depends(get_session)],
) -> _TurnPostResponse:
    """Send a user message; get the assistant reply.

    Runs `send_turn` (which calls Claude via subprocess) in a threadpool
    so it doesn't block the event loop while the LLM is thinking.
    """
    if not payload.content.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Message content cannot be empty."
        )
    try:
        user_turn, assistant_turn = await run_in_threadpool(
            send_turn, db, session_id, payload.content
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _TurnPostResponse(
        **assistant_turn.model_dump(),
        paired_turn_id=user_turn.turn_id,
    )


# ---------------------------------------------------------------------------
# Monthly briefs
# ---------------------------------------------------------------------------


class _GenerateBriefIn:
    """Optional override for which YYYY-MM period to generate. Defaults
    to the current month."""
    period_yyyymm: str | None = None


@router.post(
    "/briefs/generate",
    response_model=MonthlyBriefOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_brief(
    db: Annotated[Session, Depends(get_session)],
    period_yyyymm: str | None = None,
) -> MonthlyBriefOut:
    """Generate (or regenerate, overwriting) the brief for `period_yyyymm`.

    The Opus call is dispatched via run_in_threadpool with a 5-min ceiling
    inherited from the cio_advisor service. Frontend polls / shows a
    progress indicator while this is in flight.
    """
    return await run_in_threadpool(generate_brief, db, period_yyyymm)


@router.get("/briefs", response_model=list[MonthlyBriefSummary])
def get_briefs(
    db: Annotated[Session, Depends(get_session)],
) -> list[MonthlyBriefSummary]:
    return list_briefs(db)


@router.get("/briefs/{brief_id}", response_model=MonthlyBriefOut)
def get_brief_by_id(
    brief_id: int,
    db: Annotated[Session, Depends(get_session)],
) -> MonthlyBriefOut:
    b = get_brief(db, brief_id)
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"brief {brief_id} not found")
    return b
