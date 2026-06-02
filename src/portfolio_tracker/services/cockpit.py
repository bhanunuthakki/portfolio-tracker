"""Cockpit signal aggregation (P3, slice 1).

Produces the grounded, dollar-weighted signal set that feeds the decision
cockpit's action queue. This is the *grounding* layer: it merges the existing
deterministic coaching red-flags with the earnings-summary thesis / valuation /
alert readers (see services.earnings_summary), tagging each with the position's
dollar exposure. The Opus ranking layer (next slice) ranks and writes prose over
these signals but may not invent any that aren't grounded here.

Pure logic lives in `build_signals`; `gather_signals` does the DB / companion-DB
fetch and calls it. Concentration is deliberately NOT privileged in the default
ordering — the owner weights thesis/valuation health above concentration.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.config import get_settings
from portfolio_tracker.models import ActionQueueItem, HoldingSnapshot, Security
from portfolio_tracker.services import earnings_summary as es
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.coaching import CoachingTip, generate_coaching_tips

_SEVERITY_RANK = {"high": 0, "warning": 1, "info": 2}

# Map an earnings-summary alert trigger to a cockpit severity.
_ALERT_SEVERITY = {
    "thesis_drift": "high",
    "kpi_inflection": "high",
    "earnings_tone": "warning",
    "saydo_due": "warning",
    "material_news": "warning",
}


class Signal(BaseModel):
    """One grounded, dollar-weighted reason the cockpit might surface a holding."""

    ticker: str
    name: str | None
    kind: str  # thesis_verdict | valuation | es_alert:<kind> | coaching:<category> | coverage_gap
    severity: str  # high | warning | info
    weight_pct: float  # position's share of the portfolio (0 if unknown)
    headline: str
    detail: str
    source: str  # coaching | earnings_summary | earnings_summary_alert | portfolio
    evidence: dict[str, str] = Field(default_factory=dict)


def _severity_rank(sev: str) -> int:
    return _SEVERITY_RANK.get(sev, 9)


def build_signals(
    *,
    value_by_ticker: dict[str, Decimal],
    total_value: Decimal,
    names_by_ticker: dict[str, str | None],
    coaching_tips: list[CoachingTip],
    verdicts: dict[str, es.ThesisVerdict],
    valuations: dict[str, es.Valuation],
    alerts: dict[str, tuple[es.ThesisAlert, ...]],
    untracked: list[str],
) -> list[Signal]:
    """Merge deterministic signals into one dollar-weighted, grounded list.

    Pure: every input is supplied by the caller, so this is trivially testable.
    The default sort is a sane fallback (severity, then exposure); the Opus
    ranking layer reorders later.
    """

    def weight(ticker: str) -> float:
        if total_value <= 0:
            return 0.0
        v = value_by_ticker.get(ticker.upper())
        return float(v / total_value * 100) if v is not None else 0.0

    signals: list[Signal] = []

    # 1) Deterministic coaching red-flags (concentration included but not privileged).
    for tip in coaching_tips:
        signals.append(
            Signal(
                ticker=tip.ticker,
                name=tip.name,
                kind=f"coaching:{tip.category}",
                severity=tip.severity,
                weight_pct=weight(tip.ticker),
                headline=tip.headline,
                detail=f"{tip.detail} {tip.suggested_action}".strip(),
                source="coaching",
                evidence=dict(tip.context),
            )
        )

    # 2) Thesis verdicts (warn/breach only — 'ok' isn't worth a queue item).
    for ticker, verdict in verdicts.items():
        if verdict.status not in ("warn", "breach"):
            continue
        rule_names = "; ".join(r.kpi_name for r in verdict.flagged_rules[:4]) or "—"
        signals.append(
            Signal(
                ticker=ticker,
                name=names_by_ticker.get(ticker),
                kind="thesis_verdict",
                severity="high" if verdict.status == "breach" else "warning",
                weight_pct=weight(ticker),
                headline=f"Thesis {verdict.status.upper()} — {ticker}",
                detail=f"Flagged rule(s): {rule_names}.",
                source="earnings_summary",
                evidence={
                    "status": verdict.status,
                    "evaluated_at": verdict.evaluated_at or "",
                    "flagged_rules": rule_names,
                },
            )
        )

    # 3) Valuation triggers (rich/cheap beyond the margin-of-safety bar).
    for ticker, val in valuations.items():
        if val.signal not in ("rich", "cheap"):
            continue
        signals.append(
            Signal(
                ticker=ticker,
                name=names_by_ticker.get(ticker),
                kind="valuation",
                severity="warning" if val.signal == "rich" else "info",
                weight_pct=weight(ticker),
                headline=f"Valuation: {val.signal} — {ticker}",
                detail=_valuation_detail(val),
                source="earnings_summary",
                evidence=_valuation_evidence(val),
            )
        )

    # 4) Pending fundamental alerts.
    for ticker, items in alerts.items():
        for alert in items:
            label = alert.trigger_kind.replace("_", " ")
            signals.append(
                Signal(
                    ticker=ticker,
                    name=names_by_ticker.get(ticker),
                    kind=f"es_alert:{alert.trigger_kind}",
                    severity=_ALERT_SEVERITY.get(alert.trigger_kind, "warning"),
                    weight_pct=weight(ticker),
                    headline=f"Alert: {label} — {ticker}",
                    detail=f"Earnings Summary flagged a {label} signal on {ticker}.",
                    source="earnings_summary_alert",
                    evidence={
                        "trigger_kind": alert.trigger_kind,
                        "fired_at": alert.fired_at or "",
                        "signature_sha": alert.signature_sha or "",
                    },
                )
            )

    # 5) Coverage gaps — held, but no thesis coverage at all.
    for raw_ticker in untracked:
        ticker = raw_ticker.upper()
        signals.append(
            Signal(
                ticker=ticker,
                name=names_by_ticker.get(ticker),
                kind="coverage_gap",
                severity="info",
                weight_pct=weight(ticker),
                headline=f"No thesis coverage — {ticker}",
                detail="Not tracked in Earnings Summary — no fundamental monitoring on this holding.",
                source="portfolio",
                evidence={},
            )
        )

    signals.sort(key=lambda s: (_severity_rank(s.severity), -s.weight_pct, s.ticker))
    return signals


def gather_signals(session: Session) -> list[Signal]:
    """Fetch live inputs (holdings + coaching + earnings-summary) and build the
    grounded signal set."""
    value_by_ticker, names_by_ticker = _value_by_ticker(session)
    total = Decimal(0)
    for v in value_by_ticker.values():
        total += v
    tickers = list(value_by_ticker.keys())

    coaching = generate_coaching_tips(session)
    return build_signals(
        value_by_ticker=value_by_ticker,
        total_value=total,
        names_by_ticker=names_by_ticker,
        coaching_tips=coaching.tips,
        verdicts=es.latest_verdicts(tickers),
        valuations=es.latest_valuations(tickers),
        alerts=es.pending_alerts(tickers),
        untracked=es.untracked_holdings(tickers),
    )


# ---- internals ------------------------------------------------------------


def _valuation_detail(val: es.Valuation) -> str:
    if val.over_under_pct is None:
        return "Valuation gap unavailable."
    pct = val.over_under_pct * 100
    direction = "above" if pct >= 0 else "below"
    return f"Live price is {abs(pct):.0f}% {direction} DCF fair value."


def _valuation_evidence(val: es.Valuation) -> dict[str, str]:
    ev: dict[str, str] = {}
    if val.live_price is not None:
        ev["live_price"] = f"{val.live_price:.2f}"
    if val.fair_value is not None:
        ev["fair_value"] = f"{val.fair_value:.2f}"
    if val.over_under_pct is not None:
        ev["over_under_pct"] = f"{val.over_under_pct * 100:.0f}%"
    if val.mos_bar is not None:
        ev["mos_bar"] = f"{val.mos_bar * 100:.0f}%"
    if val.valuation_date:
        ev["valuation_date"] = val.valuation_date
    return ev


def _value_by_ticker(session: Session) -> tuple[dict[str, Decimal], dict[str, str | None]]:
    """Latest-snapshot market value summed per ticker for active accounts.

    Returns (value_by_ticker, name_by_ticker). Mirrors the holdings route's
    latest-snapshot selection; cash/untickered rows are skipped.
    """
    value: dict[str, Decimal] = {}
    names: dict[str, str | None] = {}
    accts = active_account_ids(session)
    if not accts:
        return value, names
    latest = session.execute(
        select(HoldingSnapshot.snapshot_date)
        .where(HoldingSnapshot.account_id.in_(accts))
        .order_by(HoldingSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return value, names
    rows = session.execute(
        select(HoldingSnapshot, Security)
        .join(Security, Security.security_id == HoldingSnapshot.security_id)
        .where(HoldingSnapshot.snapshot_date == latest)
        .where(HoldingSnapshot.account_id.in_(accts))
    ).all()
    for holding, security in rows:
        if not security.ticker:
            continue
        ticker = security.ticker.upper()
        market_value = holding.institution_value
        if market_value is None and holding.institution_price is not None:
            market_value = holding.quantity * holding.institution_price
        if market_value is None:
            continue
        value[ticker] = value.get(ticker, Decimal(0)) + market_value
        names.setdefault(ticker, security.name)
    return value, names


# ---------------------------------------------------------------------------
# Action queue ranking (P3, slice 2)
#
# Opus ranks the grounded signals into an advisory action queue. Hybrid
# grounding: a ranked item must reference a ticker present in the signals
# (hallucinated tickers are dropped), high-severity deterministic signals are
# re-injected if the model omits them, and if the LLM is unavailable or returns
# unparseable output we fall back to a deterministic queue built straight from
# the signals. Tone is advisory with an explicit suggested action.
# ---------------------------------------------------------------------------

RANKING_MODEL = "claude-opus-4-7"

_TIER_FROM_SEVERITY = {"high": "act", "warning": "watch", "info": "fine"}
_TIER_RANK = {"act": 0, "watch": 1, "fine": 2}

# Default advisory verb per signal-kind prefix (the LLM may override).
_ACTION_BY_KIND_PREFIX: list[tuple[str, str]] = [
    ("thesis_verdict", "audit"),
    ("valuation", "review"),
    ("es_alert:", "review"),
    ("coaching:concentration", "trim"),
    ("coaching:irr_below_bar", "review"),
    ("coaching:drawdown_audit", "audit"),
    ("coaching:thesis_stale", "audit"),
    ("coaching:multiples_detachment", "trim"),
    ("coverage_gap", "research"),
]


class ActionItem(BaseModel):
    """One ranked, advisory entry in the cockpit action queue."""

    rank: int
    ticker: str
    name: str | None
    tier: str  # act | watch | fine
    action: str  # advisory verb: trim | add | hold | exit | audit | review | research
    headline: str
    rationale: str
    suggested_action: str
    weight_pct: float
    evidence: dict[str, str] = Field(default_factory=dict)
    signal_kinds: list[str] = Field(default_factory=list)
    llm_ranked: bool = True  # False => deterministic fallback or re-injected


def rank_signals(
    signals: list[Signal],
    *,
    llm: Callable[[str], str] | None = None,
    use_llm: bool = True,
) -> list[ActionItem]:
    """Rank grounded signals into an advisory action queue.

    `llm` is injectable for tests; the production default routes an Opus call
    through the subscription-billed claude_cli wrapper. Falls back to a
    deterministic queue when `use_llm` is False, the LLM is unavailable, or it
    returns unparseable JSON.
    """
    if not signals:
        return []
    if not use_llm:
        return _deterministic_items(signals)
    caller = llm or _opus_caller()
    if caller is None:
        return _deterministic_items(signals)
    try:
        raw = caller(_build_ranking_prompt(signals))
    except Exception:  # any LLM/subprocess failure -> safe deterministic queue
        return _deterministic_items(signals)
    return _parse_ranking(raw, signals)


def _deterministic_items(signals: list[Signal]) -> list[ActionItem]:
    """1:1 signal -> ActionItem; the no-LLM fallback and re-injection source.
    Sorted act -> watch -> fine, then by exposure."""
    items = [_item_from_signal(s, llm_ranked=False) for s in signals]
    items.sort(key=lambda i: (_TIER_RANK.get(i.tier, 9), -i.weight_pct, i.ticker))
    for idx, item in enumerate(items, 1):
        item.rank = idx
    return items


def _item_from_signal(s: Signal, *, llm_ranked: bool) -> ActionItem:
    return ActionItem(
        rank=0,
        ticker=s.ticker,
        name=s.name,
        tier=_TIER_FROM_SEVERITY.get(s.severity, "fine"),
        action=_action_for_kind(s.kind),
        headline=s.headline,
        rationale=s.detail,
        suggested_action=_default_suggested_action(s),
        weight_pct=s.weight_pct,
        evidence=dict(s.evidence),
        signal_kinds=[s.kind],
        llm_ranked=llm_ranked,
    )


def _action_for_kind(kind: str) -> str:
    for prefix, action in _ACTION_BY_KIND_PREFIX:
        if kind.startswith(prefix):
            return action
    return "review"


def _default_suggested_action(s: Signal) -> str:
    return f"Review {s.ticker}: {s.headline}"


def _build_ranking_prompt(signals: list[Signal]) -> str:
    payload = json.dumps([s.model_dump() for s in signals], indent=2)
    return (
        "You are the analytical assistant to a long-horizon, concentrated equity "
        "investor (a 'Rational Bull' CIO). Below is a list of GROUNDED signals about "
        "the current portfolio; each already carries the real numbers and the "
        "position's dollar weight (weight_pct).\n\n"
        "Produce a RANKED action queue, most-urgent first. Rules:\n"
        "1. Only reference tickers that appear in the signals. Invent nothing.\n"
        "2. Ground every rationale in the provided numbers (cite them).\n"
        "3. Weight THESIS health and VALUATION far above concentration.\n"
        "4. Consolidate a ticker's multiple signals into ONE item.\n"
        "5. Tone: advisory, with an explicit suggested action (not a command).\n"
        "6. Assign tier: 'act' (needs attention now), 'watch', or 'fine'.\n\n"
        "Respond with ONLY a JSON object, no prose:\n"
        '{"items": [{"ticker": "NU", "tier": "act", '
        '"action": "trim|add|hold|exit|audit|review|research", "headline": "short", '
        '"rationale": "advisory, cites the numbers", "suggested_action": "explicit but advisory"}]}\n\n'
        f"SIGNALS:\n{payload}\n"
    )


def _parse_ranking(raw: str, signals: list[Signal]) -> list[ActionItem]:
    by_ticker: dict[str, list[Signal]] = defaultdict(list)
    for s in signals:
        by_ticker[s.ticker.upper()].append(s)

    parsed = _safe_json(raw)
    if not isinstance(parsed, dict):
        return _deterministic_items(signals)
    entries = cast(dict[str, object], parsed).get("items")
    if not isinstance(entries, list):
        return _deterministic_items(signals)

    items: list[ActionItem] = []
    seen: set[str] = set()
    for order, entry in enumerate(cast(list[object], entries)):
        if not isinstance(entry, dict):
            continue
        e = cast(dict[str, object], entry)
        ticker = str(e.get("ticker", "")).upper()
        sigs = by_ticker.get(ticker)
        if not sigs or ticker in seen:
            continue  # ungrounded ticker or duplicate -> drop
        items.append(_item_from_entry(e, sigs, order))
        seen.add(ticker)

    if not items:
        return _deterministic_items(signals)

    # Hybrid grounding: never silently drop a high-severity signal.
    for s in signals:
        if s.severity == "high" and s.ticker.upper() not in seen:
            items.append(_item_from_signal(s, llm_ranked=False))
            seen.add(s.ticker.upper())

    items.sort(key=lambda i: (_TIER_RANK.get(i.tier, 9), i.rank))
    for idx, item in enumerate(items, 1):
        item.rank = idx
    return items


def _item_from_entry(entry: dict[str, object], sigs: list[Signal], order: int) -> ActionItem:
    evidence: dict[str, str] = {}
    kinds: list[str] = []
    weight = 0.0
    name: str | None = None
    for s in sigs:
        evidence.update(s.evidence)
        kinds.append(s.kind)
        weight = max(weight, s.weight_pct)
        name = name or s.name
    return ActionItem(
        rank=order,
        ticker=sigs[0].ticker,
        name=name,
        tier=_norm_tier(entry.get("tier")) or _highest_tier(sigs),
        action=_opt_text(entry.get("action")) or _action_for_kind(kinds[0]),
        headline=_opt_text(entry.get("headline")) or sigs[0].headline,
        rationale=_opt_text(entry.get("rationale")) or sigs[0].detail,
        suggested_action=_opt_text(entry.get("suggested_action")) or _default_suggested_action(sigs[0]),
        weight_pct=weight,
        evidence=evidence,
        signal_kinds=kinds,
        llm_ranked=True,
    )


def _norm_tier(v: object) -> str | None:
    tier = str(v).lower().strip() if v is not None else ""
    return tier if tier in _TIER_RANK else None


def _highest_tier(sigs: list[Signal]) -> str:
    return min(
        (_TIER_FROM_SEVERITY.get(s.severity, "fine") for s in sigs),
        key=lambda t: _TIER_RANK[t],
    )


def _opt_text(v: object) -> str:
    return str(v).strip() if v is not None else ""


def _safe_json(raw: str) -> object:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return {}


def _opus_caller() -> Callable[[str], str] | None:
    """A callable that runs an Opus prompt via the subscription-billed
    claude_cli wrapper, or None if CLAUDE_CLI_DIR isn't configured."""
    cli_dir = get_settings().claude_cli_dir
    if not cli_dir:
        return None
    if cli_dir not in sys.path:
        sys.path.insert(0, cli_dir)
    import claude_cli  # pyright: ignore[reportMissingImports]

    fn = cast("Callable[..., object]", claude_cli.call_claude)

    def _call(prompt: str) -> str:
        return str(fn(prompt, model=RANKING_MODEL, timeout_seconds=300))

    return _call


# ---------------------------------------------------------------------------
# Persistence + lifecycle (P3, slice 3)
#
# The ranked queue is snapshotted into the `action_queue` table so accept /
# dismiss / snooze survive regeneration. Each item's stable `signature`
# (ticker + its signal kinds, hashed) keys the upsert: a refresh updates the
# same row and a dismissal/snooze sticks. Open/snoozed items whose signal no
# longer appears are marked 'resolved'; accepted/dismissed rows are kept as a
# decision record.
# ---------------------------------------------------------------------------

_ACTIVE_STATUSES = ("open", "accepted")


class QueueItemOut(BaseModel):
    """A persisted action-queue row, as served to the cockpit."""

    id: int
    signature: str
    ticker: str
    name: str | None
    tier: str
    action: str
    rank: int
    headline: str
    rationale: str
    suggested_action: str
    weight_pct: float
    evidence: dict[str, str]
    signal_kinds: list[str]
    llm_ranked: bool
    status: str
    snooze_until: date | None
    accepted_at: datetime | None
    dismissed_at: datetime | None


def _signature(item: ActionItem) -> str:
    raw = item.ticker.upper() + "|" + "+".join(sorted(item.signal_kinds))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def refresh_queue(session: Session, *, use_llm: bool = True) -> list[QueueItemOut]:
    """Regenerate the ranked queue and upsert it into `action_queue`,
    preserving the accept/dismiss/snooze state of items that persist."""
    ranked = rank_signals(gather_signals(session), use_llm=use_llm)
    now = datetime.now(UTC)
    today = now.date()

    existing = {row.signature: row for row in session.execute(select(ActionQueueItem)).scalars()}
    current: set[str] = set()

    for idx, item in enumerate(ranked, 1):
        sig = _signature(item)
        current.add(sig)
        row = existing.get(sig)
        if row is None:
            row = ActionQueueItem(signature=sig, status="open")
            session.add(row)
        _apply_display(row, item, rank=idx, now=now)
        if row.status == "resolved":
            row.status = "open"  # the concern came back
        elif row.status == "snoozed" and row.snooze_until is not None and row.snooze_until <= today:
            row.status = "open"
            row.snooze_until = None

    # Open/snoozed items whose signal cleared -> resolved. Decisions are kept.
    for sig, row in existing.items():
        if sig not in current and row.status in ("open", "snoozed"):
            row.status = "resolved"
            row.updated_at = now

    session.commit()
    return list_queue(session)


def _apply_display(row: ActionQueueItem, item: ActionItem, *, rank: int, now: datetime) -> None:
    row.ticker = item.ticker
    row.name = item.name
    row.tier = item.tier
    row.action = item.action
    row.rank = rank
    row.headline = item.headline
    row.rationale = item.rationale
    row.suggested_action = item.suggested_action
    row.weight_pct = Decimal(str(round(item.weight_pct, 4)))
    row.evidence_json = json.dumps(item.evidence)
    row.signal_kinds_json = json.dumps(item.signal_kinds)
    row.llm_ranked = item.llm_ranked
    row.last_seen_at = now
    row.updated_at = now


def list_queue(session: Session, *, include_resolved: bool = False) -> list[QueueItemOut]:
    """Active queue (open + accepted), open first, then by tier and rank.
    Snoozed-until-future, dismissed, and resolved items are hidden by default."""
    today = date.today()
    visible = [
        row
        for row in session.execute(select(ActionQueueItem)).scalars()
        if _is_visible(row, today, include_resolved=include_resolved)
    ]
    visible.sort(key=lambda r: (0 if r.status == "open" else 1, _TIER_RANK.get(r.tier, 9), r.rank))
    return [_to_out(r) for r in visible]


def _is_visible(row: ActionQueueItem, today: date, *, include_resolved: bool) -> bool:
    if row.status in _ACTIVE_STATUSES:
        return True
    if row.status == "snoozed" and row.snooze_until is not None and row.snooze_until <= today:
        return True
    return include_resolved and row.status in ("dismissed", "resolved")


def accept_item(session: Session, item_id: int) -> QueueItemOut:
    row = _require_item(session, item_id)
    now = datetime.now(UTC)
    row.status = "accepted"
    row.accepted_at = now
    row.updated_at = now
    row.commitment_json = json.dumps(
        {"action": row.action, "ticker": row.ticker, "weight_pct_at_accept": float(row.weight_pct)}
    )
    session.commit()
    return _to_out(row)


def dismiss_item(session: Session, item_id: int) -> QueueItemOut:
    row = _require_item(session, item_id)
    now = datetime.now(UTC)
    row.status = "dismissed"
    row.dismissed_at = now
    row.updated_at = now
    session.commit()
    return _to_out(row)


def snooze_item(session: Session, item_id: int, *, days: int = 7) -> QueueItemOut:
    row = _require_item(session, item_id)
    row.status = "snoozed"
    row.snooze_until = date.today() + timedelta(days=max(1, days))
    row.updated_at = datetime.now(UTC)
    session.commit()
    return _to_out(row)


def _require_item(session: Session, item_id: int) -> ActionQueueItem:
    row = session.get(ActionQueueItem, item_id)
    if row is None:
        raise ValueError(f"action_queue item {item_id} not found")
    return row


def _to_out(row: ActionQueueItem) -> QueueItemOut:
    return QueueItemOut(
        id=row.id,
        signature=row.signature,
        ticker=row.ticker,
        name=row.name,
        tier=row.tier,
        action=row.action,
        rank=row.rank,
        headline=row.headline,
        rationale=row.rationale,
        suggested_action=row.suggested_action,
        weight_pct=float(row.weight_pct),
        evidence=_loads_dict(row.evidence_json),
        signal_kinds=_loads_list(row.signal_kinds_json),
        llm_ranked=row.llm_ranked,
        status=row.status,
        snooze_until=row.snooze_until,
        accepted_at=row.accepted_at,
        dismissed_at=row.dismissed_at,
    )


def _loads_dict(raw: str) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in cast(dict[object, object], data).items()}


def _loads_list(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in cast(list[object], data)]
