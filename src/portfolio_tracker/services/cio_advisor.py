"""LLM-powered CIO advisor: per-session chat + monthly briefs.

Architecture
============

* **Single-shot Claude calls via `claude_cli.py`.** Routes through the
  user's Pro/Max subscription rather than the metered API. See
  `C:\\Users\\user\\.gemini\\snippets\\claude_cli.py` and the project's
  CLAUDE.md for the rationale. The wrapper is sync; FastAPI endpoints
  must invoke this module via `run_in_threadpool` so the subprocess
  doesn't block the event loop.

* **Stuffed context.** Each call assembles a fresh facts block from the
  current portfolio state and prepends it to the user prompt. We don't
  use Claude tool-use because (a) `claude_cli.py` is one-shot subprocess
  and (b) the inputs the advisor needs are small enough to inline.

* **Chat sessions are threaded.** Per `ChatSession` row, the first turn
  attaches a fresh facts block. Subsequent turns inherit context from
  the transcript history. If the session's `last_context_refresh_at` is
  more than 24 hours old, OR if the user message starts with `/refresh`,
  the next turn re-attaches a fresh facts block.

* **Monthly briefs are HTML.** Six locked sections (Executive Summary,
  Holdings Review, Concentration & Human Capital, Decision Log Status,
  Market Regime, Action Items). We request the model emit JSON with one
  HTML-string per section, then wrap with a consistent stylesheet
  scaffold. Regeneration overwrites the row for that month.

Models used
===========

* **Chat:** `claude-sonnet-4-6` — fast (~5–15s), good analytical writing.
* **Brief:** `claude-opus-4-7` — slow (~30–60s), deepest reasoning for
  the monthly memo.

Files / surfaces
================

* `coaching.py` — deterministic engine; its output is fed into the LLM
  context here as the "rule-based facts" block. Source of truth for
  numeric thresholds.
* `CIO_CONTEXT.md` — the user's persona file. Read fresh on every call
  so edits take effect without restart.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import (
    BriefJobStatus,
    ChatSession,
    ChatTurn,
    HoldingSnapshot,
    MonthlyBrief,
    MonthlyBriefJob,
    Security,
    TradeDecision,
)
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.coaching import generate_coaching_tips

# Sonnet for chat (interactive), Opus for the monthly brief (depth matters).
CHAT_MODEL: str = "claude-sonnet-4-6"
BRIEF_MODEL: str = "claude-opus-4-7"

# Maximum chat turns we replay back to the model on follow-ups. Past
# this we drop the oldest user/assistant pairs from the transcript to
# stay under the token budget while preserving the most recent context.
_MAX_REPLAY_TURNS: int = 30

# How long a session's facts snapshot is "fresh" before the next user
# turn forces a re-attach. Matches the spec; tune if needed.
_FACTS_FRESHNESS_HOURS: int = 24

# The CIO persona prose lives in the repo root. Read fresh on every
# advisor call so edits don't require a restart.
_PERSONA_FILE: Path = Path(__file__).resolve().parents[3] / "CIO_CONTEXT.md"

# Path to the canonical Claude CLI subprocess wrapper, per CLAUDE.md.
# Routes calls to the user's subscription (not the metered API).
_CLAUDE_CLI_DIR: str = r"C:\Users\user\.gemini\snippets"


def _import_claude() -> callable:
    """Lazy import so module-import doesn't fail if the snippet dir is
    missing on a colleague's machine. The CLI wrapper raises clearly if
    its prerequisites aren't met (ANTHROPIC_API_KEY set, claude CLI
    missing, etc.)."""
    if _CLAUDE_CLI_DIR not in sys.path:
        sys.path.insert(0, _CLAUDE_CLI_DIR)
    from claude_cli import call_claude  # type: ignore

    return call_claude


def _import_claude_stream() -> callable:
    """Lazy import of the streaming variant (sync generator yielding
    text chunks). Same rationale as `_import_claude`."""
    if _CLAUDE_CLI_DIR not in sys.path:
        sys.path.insert(0, _CLAUDE_CLI_DIR)
    from claude_cli import call_claude_stream  # type: ignore

    return call_claude_stream


# ---------------------------------------------------------------------------
# Pydantic schemas for the API
# ---------------------------------------------------------------------------


class ChatSessionOut(BaseModel):
    session_id: int
    title: str
    created_at: datetime
    updated_at: datetime
    last_context_refresh_at: datetime | None
    turn_count: int


class ChatTurnOut(BaseModel):
    turn_id: int
    session_id: int
    role: Literal["user", "assistant", "system"]
    content: str
    model_used: str | None
    created_at: datetime


class ChatTurnIn(BaseModel):
    content: str


class ChatSessionCreateIn(BaseModel):
    title: str | None = None  # optional; default = "Untitled · <date>"


class MonthlyBriefOut(BaseModel):
    brief_id: int
    period_yyyymm: str
    html: str
    model_used: str | None
    generated_at: datetime


class MonthlyBriefSummary(BaseModel):
    """Light row for listing briefs — excludes the HTML payload."""
    brief_id: int
    period_yyyymm: str
    model_used: str | None
    generated_at: datetime


class MonthlyBriefJobOut(BaseModel):
    """Status of an async brief-generation job.

    Frontend polls `GET /briefs/jobs/{job_id}` and pivots on `status`.
    `brief_id` is populated when status flips to `succeeded`; `error` is
    populated when status flips to `failed`.
    """
    job_id: int
    period_yyyymm: str
    status: Literal["pending", "running", "succeeded", "failed"]
    started_at: datetime
    completed_at: datetime | None
    brief_id: int | None
    error: str | None


# ---------------------------------------------------------------------------
# Facts block — what the LLM sees as the snapshot of current state
# ---------------------------------------------------------------------------


@dataclass
class FactsSnapshot:
    """Structured snapshot of portfolio state the LLM gets prepended to
    its first turn (or on `/refresh`). Serialized to JSON for the prompt.

    Dollar values are int-USD (rounded to the nearest dollar). The user's
    project-wide convention is "no decimals on dollar amounts" — emitting
    floats in the facts feed leads the model to invent fractional cents
    or, worse, hallucinate entirely different magnitudes. Integers are
    cleaner for the model AND match the display convention.
    """

    as_of: str
    portfolio_total_value: int  # USD, rounded
    positions: list[dict]  # each position's `value_usd` is int USD
    recent_decisions: list[dict]
    coaching_findings: dict
    persona_excerpt: str = field(default="")


def build_facts_snapshot(session: Session) -> FactsSnapshot:
    """Assemble the facts block from current DB state.

    Inputs to the LLM (per design "minimal bundle"):
      * persona file contents (read fresh)
      * current holdings (ticker, name, value, weight)
      * recent decisions (last 12)
      * deterministic coaching findings (the existing tip categories)

    NOT included (out of scope for "minimal"):
      * historical performance series / position-alpha rows
      * earnings calendar
      * market regime indicators (SPY level, VIX, etc.)
    """
    accts = active_account_ids(session)
    today_iso = date.today().isoformat()

    positions: list[dict] = []
    portfolio_total = Decimal(0)
    if accts:
        # Pin to the latest snapshot date — without this filter we'd sum
        # holdings across every historical snapshot day, inflating
        # position values N× (where N = snapshot count). Same pattern
        # data_quality.py uses.
        latest_date = session.execute(
            select(func.max(HoldingSnapshot.snapshot_date))
            .where(HoldingSnapshot.account_id.in_(accts))
        ).scalar_one_or_none()
        latest = session.execute(
            select(HoldingSnapshot, Security)
            .join(Security, Security.security_id == HoldingSnapshot.security_id)
            .where(HoldingSnapshot.account_id.in_(accts))
            .where(HoldingSnapshot.snapshot_date == latest_date)
            .where(Security.is_cash_equivalent.is_(False))
            .where(Security.ticker.is_not(None))
        ).all() if latest_date is not None else []
        # Roll up across accounts for a clean per-ticker view.
        agg: dict[str, dict] = {}
        for h, s in latest:
            t = (s.ticker or "").upper()
            if not t:
                continue
            bucket = agg.setdefault(
                t,
                {"ticker": t, "name": s.name, "qty": Decimal(0), "value": Decimal(0)},
            )
            bucket["qty"] += Decimal(h.quantity or 0)
            if h.institution_value is not None:
                bucket["value"] += Decimal(h.institution_value)
        for row in agg.values():
            portfolio_total += row["value"]
        for row in sorted(agg.values(), key=lambda r: -r["value"]):
            v = row["value"]
            positions.append({
                "ticker": row["ticker"],
                "name": row["name"],
                # Integer USD per project convention (no decimal dollars).
                "value_usd": int(round(float(v))),
                # Percentages keep 2 decimals — they're not dollars.
                "weight_pct": round(
                    float(v / portfolio_total * 100), 2
                ) if portfolio_total > 0 else 0.0,
            })

    decisions = session.execute(
        select(TradeDecision)
        .order_by(TradeDecision.decision_date.desc(), TradeDecision.decision_id.desc())
        .limit(12)
    ).scalars().all()
    recent_decisions = [
        {
            "decision_date": d.decision_date.isoformat(),
            "ticker": d.ticker,
            "action": d.action,
            "thesis": d.thesis,
            "expected_outcome": d.expected_outcome,
            "invalidation_triggers": d.invalidation_triggers,
            "confidence": d.confidence,
            "outcome_status": d.outcome_status,
        }
        for d in decisions
    ]

    coaching = generate_coaching_tips(session)
    coaching_findings = {
        "rubric_summary": coaching.rubric_summary,
        "rubric": coaching.rubric.model_dump(),
        "positions_evaluated": coaching.positions_evaluated,
        "tips": [t.model_dump() for t in coaching.tips],
    }

    persona = _read_persona()

    return FactsSnapshot(
        as_of=today_iso,
        portfolio_total_value=int(round(float(portfolio_total))),
        positions=positions,
        recent_decisions=recent_decisions,
        coaching_findings=coaching_findings,
        persona_excerpt=persona,
    )


def _read_persona() -> str:
    """Read CIO_CONTEXT.md fresh from disk. Empty string if missing."""
    try:
        return _PERSONA_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _facts_to_prompt_block(snap: FactsSnapshot) -> str:
    """Render the facts snapshot as a structured Markdown block.

    Markdown beats raw JSON for LLM ingestion — the model reads section
    headings naturally and doesn't waste tokens on JSON syntax. We
    preserve the JSON for the structured-data sections (positions,
    coaching) where structure carries meaning.
    """
    positions_json = json.dumps(snap.positions, indent=2, default=str)
    decisions_json = json.dumps(snap.recent_decisions, indent=2, default=str)
    coaching_json = json.dumps(snap.coaching_findings, indent=2, default=str)
    return f"""# Portfolio facts (as of {snap.as_of})

## CIO persona & strategic directives

{snap.persona_excerpt}

---

## Portfolio total value

${snap.portfolio_total_value:,.0f}

## Current positions (rolled up across accounts)

```json
{positions_json}
```

## Recent decisions (most recent 12)

```json
{decisions_json}
```

## Deterministic coaching findings

The portfolio-tracker's rule-based coaching engine has already computed
these flags against the rubric in `CIO_CONTEXT.md`. Use them as ground
truth — don't re-derive concentration percentages or thesis-staleness.
Contextualize them: are they actionable right now given market regime,
livelihood correlation, dry-powder posture? Which warrant immediate
action vs which are noise?

```json
{coaching_json}
```
"""


# ---------------------------------------------------------------------------
# Chat — session + turn helpers
# ---------------------------------------------------------------------------


def list_sessions(session: Session) -> list[ChatSessionOut]:
    rows = session.execute(
        select(
            ChatSession,
            func.count(ChatTurn.turn_id).label("turn_count"),
        )
        .outerjoin(ChatTurn, ChatTurn.session_id == ChatSession.session_id)
        .group_by(ChatSession.session_id)
        .order_by(ChatSession.updated_at.desc())
    ).all()
    return [
        ChatSessionOut(
            session_id=s.session_id,
            title=s.title,
            created_at=s.created_at,
            updated_at=s.updated_at,
            last_context_refresh_at=s.last_context_refresh_at,
            turn_count=tc,
        )
        for s, tc in rows
    ]


def create_session(session: Session, title: str | None) -> ChatSessionOut:
    actual_title = (title or "").strip() or f"Untitled · {date.today().isoformat()}"
    s = ChatSession(title=actual_title[:200])
    session.add(s)
    session.commit()
    session.refresh(s)
    return ChatSessionOut(
        session_id=s.session_id,
        title=s.title,
        created_at=s.created_at,
        updated_at=s.updated_at,
        last_context_refresh_at=s.last_context_refresh_at,
        turn_count=0,
    )


def delete_session(session: Session, session_id: int) -> None:
    s = session.get(ChatSession, session_id)
    if s is None:
        return
    session.delete(s)
    session.commit()


def list_turns(session: Session, session_id: int) -> list[ChatTurnOut]:
    rows = session.execute(
        select(ChatTurn)
        .where(ChatTurn.session_id == session_id)
        .order_by(ChatTurn.turn_id.asc())
    ).scalars().all()
    return [
        ChatTurnOut(
            turn_id=t.turn_id,
            session_id=t.session_id,
            role=t.role,  # type: ignore[arg-type]
            content=t.content,
            model_used=t.model_used,
            created_at=t.created_at,
        )
        for t in rows
    ]


def send_turn(
    session: Session,
    session_id: int,
    user_content: str,
) -> tuple[ChatTurnOut, ChatTurnOut]:
    """Append a user turn, call Claude, append the assistant turn.

    Returns (user_turn, assistant_turn) for the UI to render immediately.
    Caller is responsible for running this in a threadpool — the Claude
    subprocess can take 10–30s and would block the event loop otherwise.
    """
    s = session.get(ChatSession, session_id)
    if s is None:
        raise ValueError(f"Chat session {session_id} not found")

    user_content = user_content.strip()
    refresh_requested = user_content.lower().startswith("/refresh")
    if refresh_requested:
        # Strip the slash command so the model just sees the rest of the
        # message (or treats it as a noop continuation).
        user_content = user_content[len("/refresh"):].strip() or (
            "Refresh context and continue from the last topic."
        )

    facts_block = _maybe_attach_facts_block(session, s, force=refresh_requested)
    prompt = _assemble_prompt(session, session_id, facts_block, user_content)

    # Persist the user turn before calling the model so an LLM failure
    # leaves the user's message visible in the transcript.
    user_turn = ChatTurn(
        session_id=session_id,
        role="user",
        content=user_content,
    )
    session.add(user_turn)
    session.commit()
    session.refresh(user_turn)

    call_claude = _import_claude()
    try:
        response_text = call_claude(prompt, model=CHAT_MODEL)
    except Exception as exc:
        # Save a clear failure record as the assistant turn so the
        # transcript shows the error inline rather than silently dropping.
        response_text = f"[advisor error — Claude call failed: {exc!s}]"

    assistant_turn = ChatTurn(
        session_id=session_id,
        role="assistant",
        content=response_text,
        model_used=CHAT_MODEL,
    )
    session.add(assistant_turn)
    s.updated_at = datetime.now(UTC)
    session.commit()
    session.refresh(assistant_turn)
    session.refresh(user_turn)

    return (
        ChatTurnOut(
            turn_id=user_turn.turn_id,
            session_id=user_turn.session_id,
            role="user",
            content=user_turn.content,
            model_used=None,
            created_at=user_turn.created_at,
        ),
        ChatTurnOut(
            turn_id=assistant_turn.turn_id,
            session_id=assistant_turn.session_id,
            role="assistant",
            content=assistant_turn.content,
            model_used=assistant_turn.model_used,
            created_at=assistant_turn.created_at,
        ),
    )


def begin_streamed_turn(
    session: Session,
    session_id: int,
    user_content: str,
) -> tuple[ChatTurnOut, str]:
    """Prep step for a streaming chat turn.

    Mirrors the front half of `send_turn`: validates the session,
    handles `/refresh`, attaches a facts block if stale, persists the
    user turn, and builds the Claude prompt. Returns
    `(user_turn, prompt)` so the SSE endpoint can emit the user-turn id
    immediately and then start streaming Claude's output.

    The assistant turn is NOT persisted here — that happens in
    `finalize_streamed_turn` after the stream completes.
    """
    s = session.get(ChatSession, session_id)
    if s is None:
        raise ValueError(f"Chat session {session_id} not found")

    user_content = user_content.strip()
    refresh_requested = user_content.lower().startswith("/refresh")
    if refresh_requested:
        user_content = user_content[len("/refresh"):].strip() or (
            "Refresh context and continue from the last topic."
        )

    facts_block = _maybe_attach_facts_block(session, s, force=refresh_requested)
    prompt = _assemble_prompt(session, session_id, facts_block, user_content)

    user_turn = ChatTurn(
        session_id=session_id,
        role="user",
        content=user_content,
    )
    session.add(user_turn)
    session.commit()
    session.refresh(user_turn)

    return (
        ChatTurnOut(
            turn_id=user_turn.turn_id,
            session_id=user_turn.session_id,
            role="user",
            content=user_turn.content,
            model_used=None,
            created_at=user_turn.created_at,
        ),
        prompt,
    )


def stream_chat_response(prompt: str) -> Iterator[str]:
    """Stream Claude's response text chunk-by-chunk.

    Thin wrapper around `call_claude_stream` that pins the model to
    `CHAT_MODEL` (Sonnet — Opus would be too slow for an interactive
    chat). Caller is responsible for assembling the prompt via
    `begin_streamed_turn` and persisting the assistant turn via
    `finalize_streamed_turn` once the iterator is consumed.
    """
    call_claude_stream = _import_claude_stream()
    yield from call_claude_stream(prompt, model=CHAT_MODEL)


def finalize_streamed_turn(
    session: Session,
    session_id: int,
    full_text: str,
) -> ChatTurnOut:
    """Persist the assistant turn after streaming completes.

    `full_text` is the concatenation of every chunk the SSE handler
    yielded to the client. If the stream errored mid-flight the caller
    should append the error sentinel to `full_text` before calling so
    the transcript reflects the partial reply + the failure inline.
    """
    s = session.get(ChatSession, session_id)
    if s is None:
        raise ValueError(f"Chat session {session_id} not found")

    text = full_text or "[advisor returned no text]"
    assistant_turn = ChatTurn(
        session_id=session_id,
        role="assistant",
        content=text,
        model_used=CHAT_MODEL,
    )
    session.add(assistant_turn)
    s.updated_at = datetime.now(UTC)
    session.commit()
    session.refresh(assistant_turn)

    return ChatTurnOut(
        turn_id=assistant_turn.turn_id,
        session_id=assistant_turn.session_id,
        role="assistant",
        content=assistant_turn.content,
        model_used=assistant_turn.model_used,
        created_at=assistant_turn.created_at,
    )


def _maybe_attach_facts_block(
    db: Session, s: ChatSession, *, force: bool = False
) -> str | None:
    """Return a facts block to prepend, OR None if the existing snapshot
    is fresh enough to skip. Either way, records the refresh timestamp
    on the session so subsequent turns know when to next attach.

    Persists the facts block as a 'system' role ChatTurn so we have a
    record of what the model was shown at refresh time.
    """
    # SQLite drops timezone info on round-trip even when the column is
    # declared `DateTime(timezone=True)`. Re-attach UTC before comparing
    # with the aware `datetime.now(UTC)` — otherwise the second turn in
    # any session raises "can't subtract offset-naive and offset-aware".
    last_refresh = s.last_context_refresh_at
    if last_refresh is not None and last_refresh.tzinfo is None:
        last_refresh = last_refresh.replace(tzinfo=UTC)
    needs_refresh = force or (
        last_refresh is None
        or (
            datetime.now(UTC) - last_refresh
            > timedelta(hours=_FACTS_FRESHNESS_HOURS)
        )
    )
    if not needs_refresh:
        return None
    snap = build_facts_snapshot(db)
    block = _facts_to_prompt_block(snap)
    db.add(ChatTurn(
        session_id=s.session_id,
        role="system",
        content=block,
    ))
    s.last_context_refresh_at = datetime.now(UTC)
    db.commit()
    return block


def _assemble_prompt(
    db: Session, session_id: int, facts_block: str | None, user_content: str
) -> str:
    """Build the Claude prompt: optional facts block + recent transcript
    + current user message.

    Transcript replay is capped at `_MAX_REPLAY_TURNS` to keep the prompt
    bounded as conversations grow. 'system' turns (the facts blocks) are
    excluded from replay — they're stored for audit but only the most
    recent one is included via the `facts_block` arg.
    """
    parts: list[str] = []

    # Always include the most recent facts block as anchoring context.
    # If `facts_block` is None we pull the most recent stored 'system'
    # row so the model still has the snapshot, just slightly stale.
    if facts_block is not None:
        parts.append(facts_block)
    else:
        recent_facts = db.execute(
            select(ChatTurn)
            .where(ChatTurn.session_id == session_id)
            .where(ChatTurn.role == "system")
            .order_by(ChatTurn.turn_id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if recent_facts is not None:
            parts.append(recent_facts.content)

    parts.append(
        "---\n\n# Conversation so far\n\n"
        "You are the CIO advisor described in the persona above. Respond "
        "in a single message in plain text or markdown — terse, "
        "probabilistic, data-driven. When making a recommendation, name "
        "the trade-off explicitly. If the user asks about a specific "
        "ticker, ground the answer in their actual position size and "
        "the relevant deterministic flag, then add the contextual judgment "
        "the rule-based engine cannot make.\n\n"
        "### Numeric discipline (hard rules)\n\n"
        "1. **Cite, don't invent.** When quoting an absolute dollar "
        "amount or percentage, copy it verbatim from the facts block "
        "above. Specifically: use `positions[].value_usd` for position "
        "values, `positions[].weight_pct` for weights, "
        "`coaching_findings.tips[].context` for tip-specific numbers, "
        "and the `portfolio_total_value` for the portfolio total. If "
        "the number you want isn't in the facts block, say so "
        "explicitly (\"facts feed doesn't include …\") rather than "
        "estimating.\n"
        "2. **No decimal points on dollar amounts.** Render dollars as "
        "whole integers with thousands separators (e.g., $89,436 — not "
        "$89,436.00 or $89,436.07). Percentages keep one decimal "
        "(13.5%). Per-share prices and EPS may be rounded to the "
        "nearest dollar; do not output cents.\n"
        "3. **Sanity-check magnitude.** Before quoting a dollar figure, "
        "verify it against the facts block. The portfolio total is the "
        "ceiling for any single position. If you're about to write a "
        "number larger than the portfolio total, stop and re-read.\n"
    )

    prior_turns = db.execute(
        select(ChatTurn)
        .where(ChatTurn.session_id == session_id)
        .where(ChatTurn.role.in_(["user", "assistant"]))
        .order_by(ChatTurn.turn_id.asc())
    ).scalars().all()
    if len(prior_turns) > _MAX_REPLAY_TURNS:
        prior_turns = prior_turns[-_MAX_REPLAY_TURNS:]

    for t in prior_turns:
        speaker = "User" if t.role == "user" else "Advisor"
        parts.append(f"**{speaker}:** {t.content}")

    parts.append(f"**User:** {user_content}")
    parts.append("**Advisor:**")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Monthly brief
# ---------------------------------------------------------------------------


_BRIEF_SECTIONS: list[tuple[str, str]] = [
    ("executive_summary", "Executive Summary"),
    ("holdings_review", "Holdings Review"),
    ("concentration_human_capital", "Concentration & Human Capital"),
    ("decision_log_status", "Decision Log Status"),
    ("market_regime", "Market Regime"),
    ("action_items", "Action Items"),
]


def list_briefs(session: Session) -> list[MonthlyBriefSummary]:
    rows = session.execute(
        select(MonthlyBrief).order_by(MonthlyBrief.period_yyyymm.desc())
    ).scalars().all()
    return [
        MonthlyBriefSummary(
            brief_id=b.brief_id,
            period_yyyymm=b.period_yyyymm,
            model_used=b.model_used,
            generated_at=b.generated_at,
        )
        for b in rows
    ]


def get_brief(session: Session, brief_id: int) -> MonthlyBriefOut | None:
    b = session.get(MonthlyBrief, brief_id)
    if b is None:
        return None
    return MonthlyBriefOut(
        brief_id=b.brief_id,
        period_yyyymm=b.period_yyyymm,
        html=b.html,
        model_used=b.model_used,
        generated_at=b.generated_at,
    )


def generate_brief(session: Session, period_yyyymm: str | None = None) -> MonthlyBriefOut:
    """Generate (or regenerate) the brief for `period_yyyymm`.

    `period_yyyymm` defaults to the current month. Existing briefs for
    the same period are overwritten (per design spec — `(a) Overwrite`).

    Returns the persisted brief. Caller must run in a threadpool — the
    Opus call typically takes 30-60s.
    """
    if period_yyyymm is None:
        today = date.today()
        period_yyyymm = f"{today.year:04d}-{today.month:02d}"

    snap = build_facts_snapshot(session)
    facts_block = _facts_to_prompt_block(snap)
    prompt = _build_brief_prompt(facts_block, period_yyyymm)

    call_claude = _import_claude()
    raw = call_claude(prompt, model=BRIEF_MODEL, timeout_seconds=300)
    sections = _parse_brief_sections(raw)
    html = _render_brief_html(period_yyyymm, snap, sections)

    existing = session.execute(
        select(MonthlyBrief).where(MonthlyBrief.period_yyyymm == period_yyyymm)
    ).scalar_one_or_none()
    if existing is not None:
        existing.html = html
        existing.model_used = BRIEF_MODEL
        existing.generated_at = datetime.now(UTC)
        b = existing
    else:
        b = MonthlyBrief(
            period_yyyymm=period_yyyymm,
            html=html,
            model_used=BRIEF_MODEL,
        )
        session.add(b)
    session.commit()
    session.refresh(b)
    return MonthlyBriefOut(
        brief_id=b.brief_id,
        period_yyyymm=b.period_yyyymm,
        html=b.html,
        model_used=b.model_used,
        generated_at=b.generated_at,
    )


def _build_brief_prompt(facts_block: str, period_yyyymm: str) -> str:
    section_keys = ", ".join(f"`{k}`" for k, _ in _BRIEF_SECTIONS)
    section_descriptions = "\n".join(
        f"  * `{k}` — {label}: {_brief_section_guidance(k)}"
        for k, label in _BRIEF_SECTIONS
    )
    return f"""{facts_block}

---

# Task: Produce the Monthly CIO Brief for {period_yyyymm}

Produce a structured monthly review of the portfolio as the CIO described
in the persona above. The output must be a single JSON object with
exactly these keys: {section_keys}. Each value is an HTML string
(no surrounding `<html>` / `<body>` — just inline-renderable HTML using
`<p>`, `<ul>`, `<li>`, `<table>`, `<strong>`, `<em>`, `<h3>`, etc.).

Section guidance:

{section_descriptions}

Constraints
-----------
- **Cite, don't invent.** Every absolute dollar amount or percentage you
  quote must come verbatim from the facts block above. Use
  `positions[].value_usd` for position values, `positions[].weight_pct`
  for weights, `coaching_findings.tips[].context` for tip-specific
  numbers, and `portfolio_total_value` for the portfolio total. If the
  number you want isn't in the facts feed, say so explicitly rather
  than estimating.
- **No decimal points on dollar amounts.** Render dollars as whole
  integers with thousands separators (e.g., $89,436 — not $89,436.00).
  Percentages keep one decimal (13.5%). Per-share prices and EPS round
  to the nearest dollar; do not output cents.
- **Sanity-check magnitude.** Before writing any dollar number, confirm
  it's no larger than `portfolio_total_value`. If it would be, you've
  hallucinated a magnitude — re-read the facts feed.
- Use the deterministic coaching findings as ground truth — your job is
  to *contextualize* them (livelihood, market regime, conviction) and
  recommend, not to recompute concentration percentages.
- Action Items should be concrete and prioritized: top-3, each with
  rationale, threshold for execution, and what would change the call.
- Tone: tier-2 thinking, terse, probabilistic. No filler.
- Output **only the JSON object**, no markdown fences, no commentary.
"""


def _brief_section_guidance(key: str) -> str:
    return {
        "executive_summary": (
            "5-7 sentence overview. Total value, vs prior period if "
            "apparent, top 2-3 strategic themes for the month, any "
            "critical action items at a glance."
        ),
        "holdings_review": (
            "Position-by-position commentary on each material holding "
            "(>5% weight). For each: thesis status, conviction, what "
            "moved this month if apparent from the data, hold/trim/add "
            "leaning. Avoid tickers <2% unless they're newly opened."
        ),
        "concentration_human_capital": (
            "Where is portfolio risk correlated with the user's "
            "livelihood (Meta employment + spouse's startup-ecosystem "
            "employment)? Specific tickers and exposures. Recommend "
            "rebalancing IF correlation is meaningful, otherwise "
            "confirm the diversification is intact."
        ),
        "decision_log_status": (
            "Which open positions lack a thesis? Which have stale "
            "theses (>180d)? What were the most recent decisions and "
            "did they execute? Don't list every gap; surface the 3-5 "
            "most material ones."
        ),
        "market_regime": (
            "Brief read on market posture: are we in a regime where "
            "the Rational Bull thesis still applies, or a dislocation "
            "calling for active dry-powder deployment? Use the data "
            "available; explicitly state where you're inferring."
        ),
        "action_items": (
            "Top 3 prioritized actions for the month. Each: what, why, "
            "trigger threshold, what would change the call. "
            "Format as an HTML <ol> with one <li> per action."
        ),
    }.get(key, "Free-form HTML.")


def _parse_brief_sections(raw: str) -> dict[str, str]:
    """Parse the JSON the model emitted into a dict of section_key → HTML.

    Falls back gracefully if the model wrapped its output in a markdown
    code fence or added stray text. Missing sections render as a
    placeholder so the brief is still viewable.
    """
    text = raw.strip()
    # Strip a leading code fence if present
    if text.startswith("```"):
        first_nl = text.find("\n")
        text = text[first_nl + 1:] if first_nl != -1 else text
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("brief JSON is not an object")
    except (json.JSONDecodeError, ValueError):
        # Surface the raw text in the executive summary so the user can
        # see what the model produced even if it didn't follow the schema.
        return {
            "executive_summary": (
                "<p><em>Note: the model didn't return parseable JSON. "
                "Raw output below.</em></p>"
                f"<pre style='white-space:pre-wrap'>{_escape(raw)}</pre>"
            ),
        }
    return {k: str(v) for k, v in parsed.items() if k in dict(_BRIEF_SECTIONS)}


def _render_brief_html(
    period_yyyymm: str, snap: FactsSnapshot, sections: dict[str, str]
) -> str:
    """Wrap the LLM section HTML in a consistent stylesheet + scaffold."""
    rendered_sections: list[str] = []
    for key, label in _BRIEF_SECTIONS:
        content = sections.get(key, f"<p><em>{label} not produced by the model.</em></p>")
        rendered_sections.append(
            f'<section id="{key}">\n  <h2>{label}</h2>\n  {content}\n</section>'
        )
    body = "\n\n".join(rendered_sections)
    portfolio_total_fmt = f"${snap.portfolio_total_value:,.0f}"
    today_iso = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>CIO Brief · {period_yyyymm}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
      max-width: 760px;
      margin: 2rem auto;
      padding: 0 1.5rem 4rem;
      color: #0f172a;
      line-height: 1.55;
    }}
    header.brief-header {{
      border-bottom: 1px solid #e2e8f0;
      padding-bottom: 1rem;
      margin-bottom: 1.5rem;
    }}
    header.brief-header h1 {{
      margin: 0;
      font-size: 1.5rem;
      font-weight: 600;
    }}
    header.brief-header .meta {{
      margin-top: 0.25rem;
      color: #64748b;
      font-size: 0.85rem;
    }}
    section {{
      margin-top: 2rem;
    }}
    section h2 {{
      font-size: 1.05rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #475569;
      border-bottom: 1px solid #e2e8f0;
      padding-bottom: 0.35rem;
    }}
    section h3 {{
      font-size: 0.95rem;
      margin-top: 1.25rem;
      color: #0f172a;
    }}
    p, ul, ol, table {{
      font-size: 0.92rem;
    }}
    ul, ol {{ padding-left: 1.4rem; }}
    li {{ margin: 0.35rem 0; }}
    table {{
      border-collapse: collapse;
      margin: 0.5rem 0;
    }}
    th, td {{
      border: 1px solid #e2e8f0;
      padding: 0.35rem 0.6rem;
      text-align: left;
    }}
    th {{ background: #f8fafc; font-weight: 600; }}
    code, pre {{
      background: #f1f5f9;
      padding: 0.1rem 0.3rem;
      border-radius: 3px;
      font-size: 0.85em;
    }}
    pre {{ padding: 0.75rem; overflow-x: auto; }}
    footer.brief-footer {{
      margin-top: 3rem;
      padding-top: 1rem;
      border-top: 1px solid #e2e8f0;
      color: #94a3b8;
      font-size: 0.8rem;
    }}
  </style>
</head>
<body>
  <header class="brief-header">
    <h1>CIO Brief · {period_yyyymm}</h1>
    <div class="meta">
      Portfolio value {portfolio_total_fmt} ·
      Generated {today_iso}
    </div>
  </header>

{body}

  <footer class="brief-footer">
    Generated by the portfolio-tracker CIO advisor.
    Numbers derived from holdings snapshots + the rule-based coaching engine.
    The advisor's recommendations are LLM-generated and reflect the rubric
    in CIO_CONTEXT.md.
  </footer>
</body>
</html>"""


def _escape(s: str) -> str:
    """Minimal HTML escape for the JSON-parse-failure fallback."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Async job lifecycle — see also api/routes/cio_advisor.py:post_brief
# ---------------------------------------------------------------------------


def _job_to_out(job: MonthlyBriefJob) -> MonthlyBriefJobOut:
    return MonthlyBriefJobOut(
        job_id=job.job_id,
        period_yyyymm=job.period_yyyymm,
        status=job.status,  # type: ignore[arg-type]
        started_at=job.started_at,
        completed_at=job.completed_at,
        brief_id=job.brief_id,
        error=job.error,
    )


def find_in_flight_brief_job(
    session: Session, period_yyyymm: str
) -> MonthlyBriefJobOut | None:
    """Return the pending/running job for `period_yyyymm`, if any.

    Used by `POST /briefs/generate` to dedupe rapid clicks: rather than
    spawning a second Opus call, we return the existing job so the
    frontend polls it.
    """
    row = session.execute(
        select(MonthlyBriefJob)
        .where(MonthlyBriefJob.period_yyyymm == period_yyyymm)
        .where(
            MonthlyBriefJob.status.in_(
                [BriefJobStatus.PENDING.value, BriefJobStatus.RUNNING.value]
            )
        )
        .order_by(MonthlyBriefJob.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _job_to_out(row) if row is not None else None


def create_brief_job(session: Session, period_yyyymm: str) -> MonthlyBriefJobOut:
    """Insert a new pending job row and return it.

    Caller is responsible for the in-flight dedup check (see
    `find_in_flight_brief_job`) and for scheduling the background
    worker (`run_brief_job`).
    """
    job = MonthlyBriefJob(
        period_yyyymm=period_yyyymm,
        status=BriefJobStatus.PENDING.value,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return _job_to_out(job)


def get_brief_job(session: Session, job_id: int) -> MonthlyBriefJobOut | None:
    job = session.get(MonthlyBriefJob, job_id)
    return _job_to_out(job) if job is not None else None


def list_brief_jobs(
    session: Session, statuses: list[str] | None = None
) -> list[MonthlyBriefJobOut]:
    """List jobs, newest first. Optionally filter to a subset of statuses
    (used by the frontend to resume polling on mount via
    `?status=pending,running`)."""
    q = select(MonthlyBriefJob).order_by(MonthlyBriefJob.started_at.desc())
    if statuses:
        q = q.where(MonthlyBriefJob.status.in_(statuses))
    rows = session.execute(q).scalars().all()
    return [_job_to_out(r) for r in rows]


def run_brief_job(job_id: int, period_yyyymm: str) -> None:
    """Sync worker invoked by FastAPI BackgroundTasks.

    Opens its OWN DB session — the request-scoped session that inserted
    the job row was already closed by the time this runs. Marks the job
    `running`, calls `generate_brief`, and finalizes to `succeeded` (with
    `brief_id` set) or `failed` (with `error` set).

    Defined in the service module — not the route — so the lifecycle is
    a unit of business logic, not glue. FastAPI runs this in a threadpool
    because it's a sync def.
    """
    db = SessionLocal()
    try:
        job = db.get(MonthlyBriefJob, job_id)
        if job is None:
            # Row vanished between scheduling and running. Nothing to do.
            return

        job.status = BriefJobStatus.RUNNING.value
        db.commit()

        try:
            brief = generate_brief(db, period_yyyymm)
        except Exception as exc:
            # Roll back any partial state from `generate_brief` and
            # re-fetch the job — the rollback can detach our reference.
            db.rollback()
            job = db.get(MonthlyBriefJob, job_id)
            if job is None:
                return
            job.status = BriefJobStatus.FAILED.value
            job.error = str(exc)[:2000]
            job.completed_at = datetime.now(UTC)
            db.commit()
            return

        job.status = BriefJobStatus.SUCCEEDED.value
        job.brief_id = brief.brief_id
        job.completed_at = datetime.now(UTC)
        db.commit()
    finally:
        db.close()
