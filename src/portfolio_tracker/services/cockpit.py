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

from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import HoldingSnapshot, Security
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
