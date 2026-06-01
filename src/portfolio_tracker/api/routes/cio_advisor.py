"""CIO advisor endpoints — chat sessions + monthly briefs.

All LLM calls are dispatched through `run_in_threadpool` because
`claude_cli.py` is a sync subprocess wrapper. Without the threadpool
hop the chat turn (~10–15s) or brief generation (~30–60s) would block
the FastAPI event loop and stall every other request.

The streaming chat endpoint (`/turns/stream`) bridges the sync subprocess
to an async generator via a thread + asyncio.Queue. The threadpool hop
isn't enough by itself because we need to interleave yielded tokens
back to FastAPI's StreamingResponse as they arrive — not after the
full subprocess exits.

Surfaces
========

* `POST /api/cio-advisor/sessions` — create a new chat thread.
* `GET  /api/cio-advisor/sessions` — list threads (newest first).
* `DELETE /api/cio-advisor/sessions/{id}` — drop a thread.
* `GET  /api/cio-advisor/sessions/{id}/turns` — load the transcript.
* `POST /api/cio-advisor/sessions/{id}/turns` — send a message + get reply (blocking).
* `POST /api/cio-advisor/sessions/{id}/turns/stream` — same, but SSE token stream.
* `POST /api/cio-advisor/briefs/generate` — generate or overwrite the
  current-month brief. Returns the full HTML body.
* `GET  /api/cio-advisor/briefs` — list briefs by period (newest first).
* `GET  /api/cio-advisor/briefs/{id}` — fetch one brief by id.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from portfolio_tracker.db import get_session
from portfolio_tracker.services.cio_advisor import (
    ChatSessionCreateIn,
    ChatSessionOut,
    ChatTurnIn,
    ChatTurnOut,
    MonthlyBriefOut,
    MonthlyBriefSummary,
    begin_streamed_turn,
    create_session,
    delete_session,
    finalize_streamed_turn,
    generate_brief,
    get_brief,
    list_briefs,
    list_sessions,
    list_turns,
    send_turn,
    stream_chat_response,
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


@router.post("/sessions/{session_id}/turns/stream")
async def post_turn_stream(
    session_id: int,
    payload: ChatTurnIn,
    db: Annotated[Session, Depends(get_session)],
) -> StreamingResponse:
    """Send a user message; stream the assistant reply as SSE.

    SSE event payloads (each preceded by `data: ` and terminated by
    `\\n\\n`):
      * `{"user_turn_id": N}`            — first event, lets the client
                                            tag the locally-rendered user
                                            bubble with its real id.
      * `{"text": "..."}`                — one per token chunk from Claude.
      * `{"error": "..."}`               — Claude call failed; will still
                                            be followed by a `done` event
                                            (the failure is persisted into
                                            the assistant turn so the
                                            transcript surfaces it).
      * `{"done": true, "turn_id": N}`   — final event; the client should
                                            invalidate its transcript
                                            query to pull the persisted
                                            user + assistant turns.

    The user turn is persisted synchronously before the stream opens so
    a Claude-side failure doesn't drop the user's message. The assistant
    turn is persisted after the stream finishes (accumulated chunks +
    any error sentinel).
    """
    if not payload.content.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Message content cannot be empty."
        )
    try:
        user_turn, prompt = await run_in_threadpool(
            begin_streamed_turn, db, session_id, payload.content
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    async def event_gen() -> Any:
        # Tell the client the persisted user-turn id so it can key the
        # in-progress user bubble correctly before the final cache invalidate.
        yield f"data: {json.dumps({'user_turn_id': user_turn.turn_id})}\n\n"

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def produce() -> None:
            """Run the sync subprocess generator on a worker thread, push
            each chunk back to the event loop via call_soon_threadsafe.

            The thread is daemon=True so a server shutdown doesn't hang
            on a long-running Claude call. We catch BaseException so a
            stuck/killed subprocess can still flush a final 'done' onto
            the queue and let the consumer drain cleanly.
            """
            try:
                for chunk in stream_chat_response(prompt):
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
            except BaseException as exc:  # noqa: BLE001 — last-chance handler
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        threading.Thread(target=produce, daemon=True).start()

        accumulated: list[str] = []
        error_msg: str | None = None
        while True:
            kind, val = await queue.get()
            if kind == "chunk":
                accumulated.append(val)
                yield f"data: {json.dumps({'text': val})}\n\n"
            elif kind == "error":
                error_msg = val
                yield f"data: {json.dumps({'error': val})}\n\n"
            elif kind == "done":
                break

        full_text = "".join(accumulated)
        if error_msg is not None:
            # Append the sentinel so the persisted transcript shows the
            # failure inline — mirrors what the blocking endpoint does.
            full_text = (
                (full_text + "\n\n" if full_text else "")
                + f"[advisor error — Claude call failed: {error_msg}]"
            )

        try:
            assistant_turn = await run_in_threadpool(
                finalize_streamed_turn, db, session_id, full_text
            )
            yield (
                "data: "
                + json.dumps({"done": True, "turn_id": assistant_turn.turn_id})
                + "\n\n"
            )
        except Exception as exc:  # noqa: BLE001 — generator can't raise to caller
            yield (
                "data: "
                + json.dumps({"error": f"failed to persist assistant turn: {exc!s}"})
                + "\n\n"
            )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            # Don't let any intermediary cache or buffer the stream.
            # `X-Accel-Buffering: no` disables nginx response buffering
            # in case the app is ever fronted by one.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
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
