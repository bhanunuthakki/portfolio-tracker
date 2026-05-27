"""CIO coaching service — surfaces red-flag tips against the user's rubric.

The rubric lives in `CIO_CONTEXT.md` at the repo root and the *numeric*
defaults are mirrored as constants here. The persona prose isn't loaded
into Python at runtime — the file is for human / LLM coaching context.
Mechanical thresholds live here so the API is deterministic.

Categories of tips
==================

* ``irr_below_bar`` — held ≥ ``MIN_HOLD_YEARS_FOR_IRR`` years, annualized
  return below ``IRR_FLOOR_PCT``. Failing the 10–12% non-index bar.

* ``concentration_human_capital`` — single position > ``CONCENTRATION_MAX_PCT``
  of portfolio AND the ticker is in the Big-Tech/Ads (Meta) or Startup/VC
  (Plaid) overlap bucket. These positions magnify household income risk.

* ``concentration_limit`` — single position > ``CONCENTRATION_MAX_PCT``
  even outside human-capital overlap. Surfaced separately so it sorts
  below the human-capital tips.

* ``thesis_stale`` — open position, no decision/outcome update in
  ``THESIS_STALE_DAYS`` days.

* ``multiples_detachment`` — unrealized return ≥ ``DETACHMENT_FACTOR`` × cost
  basis with no ``trim`` or ``sell`` decision logged in the last
  ``THESIS_STALE_DAYS`` days. Strategic Directive 3: trim when multiples
  detach.

* ``drawdown_audit`` — unrealized return ≤ ``-DRAWDOWN_AUDIT_PCT`` and
  no decision logged in the last ``DRAWDOWN_AUDIT_WINDOW_DAYS`` days.
  Strategic Directive 3: macro drawdown is a trigger for micro-economic
  audit, not an automatic hold.

Index/ETF positions (VTI/VOO/SPY/IVV/RSP/QQQ) are excluded — they're
benchmark proxies, not active picks, and the rubric only governs active
non-index holdings. SGOV and other cash equivalents are also excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    CostBasisOverride,
    HoldingSnapshot,
    InvestmentTransaction,
    InvestmentTransactionType,
    Security,
    TradeDecision,
)
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services import earnings_summary as earnings_summary_svc

# Numeric rubric — keep aligned with CIO_CONTEXT.md "Numeric bar" table.
# These are the deterministic thresholds the engine evaluates; the
# qualitative persona prose lives in the markdown file.
IRR_FLOOR_PCT: Decimal = Decimal("10")
MIN_HOLD_YEARS_FOR_IRR: Decimal = Decimal("3")
CONCENTRATION_MAX_PCT: Decimal = Decimal("8")
THESIS_STALE_DAYS: int = 180
DETACHMENT_FACTOR: Decimal = Decimal("2.0")  # unrealized ≥ 2× cost basis
DRAWDOWN_AUDIT_PCT: Decimal = Decimal("25")
DRAWDOWN_AUDIT_WINDOW_DAYS: int = 90

# Minimum position value before we'll evaluate it. Below this, tips are
# noise (e.g. a $200 starter position doesn't need a coaching ping).
_MIN_POSITION_VALUE: Decimal = Decimal("2500")

# Tickers whose primary revenue/end-market overlaps the user's employer
# (Meta — Ads / Big-Tech / consumer Internet). Holding these correlates
# household income with portfolio income, which violates the human-
# capital constraint.
_BIG_TECH_ADS_OVERLAP: frozenset[str] = frozenset(
    {
        "META",
        "GOOG",
        "GOOGL",
        "AMZN",
        "MSFT",
        "AAPL",
        "NFLX",
        "SNAP",
        "PINS",
        "TTD",
        "RDDT",
        "APP",
        "ROKU",
    }
)

# Tickers/themes whose tailwinds correlate with a startup/VC/fintech employer
# (startup/VC ecosystem — fintech infra, recent IPOs of YC/VC names,
# ZIRP-sensitive fintech). The list is intentionally narrow and curated;
# add tickers via this file rather than the DB so it's reviewable.
_STARTUP_VC_OVERLAP: frozenset[str] = frozenset(
    {"COIN", "AFRM", "UPST", "HOOD", "SOFI", "MQ", "MNDY", "DDOG", "NET", "CRWD", "SNOW", "RBLX"}
)

# Broad-market ETFs — never coached against (they're benchmarks).
_INDEX_ETFS: frozenset[str] = frozenset(
    {"VTI", "VOO", "SPY", "IVV", "RSP", "QQQ", "VXUS", "VEA", "VWO", "BND"}
)

# Cash equivalents — also never coached.
_CASH_EQUIV: frozenset[str] = frozenset({"SGOV", "FDRXX", "SHV", "SPAXX", "CUR:USD", "VMFXX"})


TipCategory = Literal[
    "irr_below_bar",
    "concentration_human_capital",
    "concentration_limit",
    "thesis_stale",
    "multiples_detachment",
    "drawdown_audit",
]
TipSeverity = Literal["info", "warning", "high"]


class CoachingTip(BaseModel):
    category: TipCategory
    severity: TipSeverity
    ticker: str
    name: str | None
    headline: str
    detail: str
    suggested_action: str
    context: dict[str, str]


class CoachingRubric(BaseModel):
    irr_floor_pct: float
    min_hold_years_for_irr_check: float
    concentration_max_pct: float
    thesis_stale_days: int
    multiples_detachment_factor: float
    drawdown_audit_pct: float
    drawdown_audit_window_days: int


class CoachingResult(BaseModel):
    generated_at: datetime
    rubric_summary: str
    tips: list[CoachingTip]
    positions_evaluated: int
    rubric: CoachingRubric


@dataclass
class _PositionState:
    """Working-copy aggregate of one ticker across all active accounts."""

    ticker: str
    name: str | None
    qty: Decimal
    value: Decimal
    cost_basis_total: Decimal  # broker-reported, possibly junk
    first_buy: date | None
    last_action: date | None
    bought_total: Decimal  # cash deployed
    sold_total: Decimal  # cash returned


def generate_coaching_tips(session: Session) -> CoachingResult:
    """Build the full set of coaching tips for the current portfolio.

    Pure read — no mutations. Snapshot of "as of today" using the latest
    `holdings_snapshots` date for active accounts. Tips are ordered by
    severity (high → warning → info) then by ticker, so the UI can render
    them in a stable, scannable list.
    """
    accts = active_account_ids(session)
    tips: list[CoachingTip] = []
    if not accts:
        return CoachingResult(
            generated_at=datetime.now(UTC),
            rubric_summary=_rubric_summary(),
            tips=[],
            positions_evaluated=0,
            rubric=_rubric_payload(),
        )

    today = _latest_snapshot_date(session, accts)
    if today is None:
        return CoachingResult(
            generated_at=datetime.now(UTC),
            rubric_summary=_rubric_summary(),
            tips=[],
            positions_evaluated=0,
            rubric=_rubric_payload(),
        )

    positions = _aggregate_positions(session, accts, today)
    portfolio_total = sum((p.value for p in positions.values()), Decimal(0))
    last_decisions_by_ticker = _last_decisions_by_ticker(session)
    last_trim_or_sell_by_ticker = _last_trim_or_sell_by_ticker(session)
    # Cross-project: the user maintains research theses in the companion
    # `earnings-summary` project. A position with a thesis there should
    # NOT trigger "has no thesis on file" — that's a false positive that
    # ignores their actual work. Empty dict if the companion isn't set up
    # or its DB is in a weird state; coaching degrades to "decision log
    # only" gracefully.
    es_thesis_by_ticker = {
        t: s
        for t, s in earnings_summary_svc.summary_by_ticker(
            list(positions.keys())
        ).items()
        if s.thesis_status is not None or s.thesis_summary is not None
    }

    evaluated = 0
    for ticker, p in positions.items():
        if p.ticker in _CASH_EQUIV or p.ticker in _INDEX_ETFS:
            continue
        if p.value < _MIN_POSITION_VALUE or p.qty <= 0:
            continue
        evaluated += 1

        last_decision = last_decisions_by_ticker.get(ticker)
        last_trim = last_trim_or_sell_by_ticker.get(ticker)

        # ---- IRR below bar ------------------------------------------------
        if p.first_buy is not None:
            years_held = Decimal((today - p.first_buy).days) / Decimal("365.25")
            # `bought_total` only sums transactions at the current broker.
            # ACATS-in capital lives in `cost_basis_total` via overrides.
            # Use the larger of the two so transferred positions don't show
            # an artificially inflated IRR from a too-small denominator.
            denom = max(p.cost_basis_total, p.bought_total)
            cost_basis_source = (
                "override" if p.cost_basis_total > p.bought_total else "transactions"
            )
            if years_held >= MIN_HOLD_YEARS_FOR_IRR and denom > 0:
                # CAGR proxy: (value + sold) / deployed, annualized.
                ratio = (p.value + p.sold_total) / denom
                # Decimal-friendly nth-root via float — IRR coaching is
                # diagnostic, not finance-grade.
                cagr_pct = (float(ratio) ** (1 / float(years_held)) - 1) * 100
                if cagr_pct < float(IRR_FLOOR_PCT):
                    tips.append(
                        CoachingTip(
                            category="irr_below_bar",
                            severity="high" if cagr_pct < 0 else "warning",
                            ticker=ticker,
                            name=p.name,
                            headline=(
                                f"{ticker} returning ~{cagr_pct:.1f}%/yr over "
                                f"{years_held:.1f}y — below {IRR_FLOOR_PCT}% bar"
                            ),
                            detail=(
                                f"First buy {p.first_buy}. Total deployed "
                                f"${_int(denom)}, value today "
                                f"${_int(p.value)}, realized "
                                f"${_int(p.sold_total)}. "
                                f"Non-index positions need to clear "
                                f"{IRR_FLOOR_PCT}%/yr to justify the "
                                f"concentration + tax cost vs SPY."
                            ),
                            suggested_action=(
                                "Re-underwrite the thesis or rotate. If the "
                                "thesis is intact and the gap is purely "
                                "multiple compression, document why before "
                                "the next hold cycle."
                            ),
                            context={
                                "years_held": f"{years_held:.2f}",
                                "cagr_pct": f"{cagr_pct:.2f}",
                                "bought_total": _int(p.bought_total),
                                "cost_basis_total": _int(p.cost_basis_total),
                                "cost_basis_source": cost_basis_source,
                                "sold_total": _int(p.sold_total),
                                "value": _int(p.value),
                            },
                        )
                    )

        # ---- Concentration -----------------------------------------------
        if portfolio_total > 0:
            weight_pct = (p.value / portfolio_total) * Decimal(100)
            if weight_pct > CONCENTRATION_MAX_PCT:
                bucket = _human_capital_bucket(ticker)
                if bucket is not None:
                    tips.append(
                        CoachingTip(
                            category="concentration_human_capital",
                            severity="high",
                            ticker=ticker,
                            name=p.name,
                            headline=(
                                f"{ticker} is {weight_pct:.1f}% of portfolio "
                                f"AND overlaps {bucket} (human-capital constraint)"
                            ),
                            detail=(
                                f"Position value ${_int(p.value)} of "
                                f"${_int(portfolio_total)} total. Stacking "
                                f"this on top of your employer income "
                                f"concentrates household cashflow risk on "
                                f"the same end-market."
                            ),
                            suggested_action=(
                                f"Trim toward ≤{CONCENTRATION_MAX_PCT}%. "
                                "Fund SGOV reserve with proceeds; deploy "
                                f"into a satellite uncorrelated to {bucket} "
                                "when multiples compress."
                            ),
                            context={
                                "weight_pct": f"{weight_pct:.2f}",
                                "bucket": bucket,
                                "value": _int(p.value),
                                "portfolio_total": _int(portfolio_total),
                            },
                        )
                    )
                else:
                    tips.append(
                        CoachingTip(
                            category="concentration_limit",
                            severity="warning",
                            ticker=ticker,
                            name=p.name,
                            headline=(
                                f"{ticker} is {weight_pct:.1f}% of portfolio "
                                f"— above {CONCENTRATION_MAX_PCT}% single-name cap"
                            ),
                            detail=(
                                f"Position value ${_int(p.value)} of "
                                f"${_int(portfolio_total)} total. Concentration "
                                "target is 5–10 names with no single name "
                                f"materially above {CONCENTRATION_MAX_PCT}%."
                            ),
                            suggested_action=(
                                "Trim if conviction hasn't strengthened in "
                                "proportion to the size growth. Otherwise "
                                "log a high-conviction Hold decision so "
                                "future-you can audit."
                            ),
                            context={
                                "weight_pct": f"{weight_pct:.2f}",
                                "value": _int(p.value),
                                "portfolio_total": _int(portfolio_total),
                            },
                        )
                    )

        # ---- Thesis stale -------------------------------------------------
        # A position has a thesis on file if EITHER (a) there's a
        # decision-log entry in the portfolio-tracker DB, OR (b) the
        # companion earnings-summary project tracks the ticker with a
        # non-empty thesis_state / thesis_summary. We treat both as
        # equally valid — the user splits their pre-trade pulse-checks
        # (decision log) from their long-form research (earnings-summary
        # briefs), and the coaching engine shouldn't nag about thesis
        # absence when the work just lives elsewhere.
        es_summary = es_thesis_by_ticker.get(ticker)
        if last_decision is None and es_summary is None:
            tips.append(
                CoachingTip(
                    category="thesis_stale",
                    severity="warning",
                    ticker=ticker,
                    name=p.name,
                    headline=f"{ticker} has no thesis on file",
                    detail=(
                        f"Currently held position (${_int(p.value)}) with "
                        "no decision-log entry and no earnings-summary "
                        "thesis. Without a written thesis you can't audit "
                        "invalidation triggers when the position moves "
                        "against you."
                    ),
                    suggested_action=(
                        "Open the decision log card on the Dashboard and "
                        "write a 1-paragraph thesis + invalidation triggers, "
                        "OR start a research brief in earnings-summary for "
                        "this ticker."
                    ),
                    context={"value": _int(p.value)},
                )
            )
        elif last_decision is not None:
            stale_days = (today - last_decision).days
            if stale_days > THESIS_STALE_DAYS:
                tips.append(
                    CoachingTip(
                        category="thesis_stale",
                        severity="info",
                        ticker=ticker,
                        name=p.name,
                        headline=(
                            f"{ticker} thesis is {stale_days}d old "
                            f"(> {THESIS_STALE_DAYS}d threshold)"
                        ),
                        detail=(
                            f"Last decision logged {last_decision}. "
                            "Re-confirm or revise — markets and fundamentals "
                            "have moved since the original write-up."
                        ),
                        suggested_action=(
                            "Log a new 'hold' decision noting what changed "
                            "(or didn't) since the prior thesis. Refresh the "
                            "invalidation triggers."
                        ),
                        context={
                            "stale_days": str(stale_days),
                            "last_decision_date": last_decision.isoformat(),
                        },
                    )
                )
        # When `last_decision is None` but the earnings-summary thesis
        # exists, no tip fires — the work is recorded, just not in the
        # decision log. Future enhancement: surface earnings-summary
        # thesis age too once that table has reliable timestamps.

        # ---- Multiples detachment (no trim since position 2x'd) -----------
        # Same denom convention as IRR — see comment in that block. Using
        # `bought_total` alone would flag ACATS-in positions as detached
        # purely because the broker-side deployment is small.
        mtm_denom = max(p.cost_basis_total, p.bought_total)
        mtm_cost_basis_source = (
            "override" if p.cost_basis_total > p.bought_total else "transactions"
        )
        if mtm_denom > 0:
            mtm_ratio = p.value / mtm_denom
            if mtm_ratio >= DETACHMENT_FACTOR:
                recent_trim = (
                    last_trim is not None and (today - last_trim).days <= THESIS_STALE_DAYS
                )
                if not recent_trim:
                    tips.append(
                        CoachingTip(
                            category="multiples_detachment",
                            severity="warning",
                            ticker=ticker,
                            name=p.name,
                            headline=(
                                f"{ticker} unrealized {mtm_ratio:.1f}× cost "
                                "with no trim/sell in 180d"
                            ),
                            detail=(
                                f"Deployed ${_int(mtm_denom)}, position "
                                f"value ${_int(p.value)}. Strategic Directive 3: "
                                "trim satellites when multiples detach from "
                                "fundamentals; fund SGOV with the proceeds."
                            ),
                            suggested_action=(
                                "If the thesis is still intact at the higher "
                                "multiple, log a 'hold' decision with "
                                "explicit reasoning. Otherwise trim 10-25% "
                                "and log the rationale."
                            ),
                            context={
                                "mtm_ratio": f"{mtm_ratio:.2f}",
                                "bought_total": _int(p.bought_total),
                                "cost_basis_total": _int(p.cost_basis_total),
                                "cost_basis_source": mtm_cost_basis_source,
                                "value": _int(p.value),
                            },
                        )
                    )

        # ---- Drawdown without thesis audit --------------------------------
        if p.cost_basis_total > 0:
            unrealized_pct = (p.value - p.cost_basis_total) / p.cost_basis_total * Decimal(100)
            if unrealized_pct <= -DRAWDOWN_AUDIT_PCT:
                stale_audit = (
                    last_decision is None
                    or (today - last_decision).days > DRAWDOWN_AUDIT_WINDOW_DAYS
                )
                if stale_audit:
                    tips.append(
                        CoachingTip(
                            category="drawdown_audit",
                            severity="high",
                            ticker=ticker,
                            name=p.name,
                            headline=(
                                f"{ticker} down {unrealized_pct:.1f}% with no "
                                f"thesis audit in {DRAWDOWN_AUDIT_WINDOW_DAYS}d"
                            ),
                            detail=(
                                f"Cost basis ${_int(p.cost_basis_total)} vs "
                                f"value ${_int(p.value)}. Macro drawdowns "
                                "trigger micro-economic audits, not automatic "
                                "holds. Liquidate immediately if the drawdown "
                                "coincides with a broken micro-thesis."
                            ),
                            suggested_action=(
                                "Open the decision log: write whether the "
                                "drawdown reflects multiple compression "
                                "(hold/add) or a broken thesis (liquidate). "
                                "Cite specific KPIs."
                            ),
                            context={
                                "unrealized_pct": f"{unrealized_pct:.2f}",
                                "cost_basis_total": _int(p.cost_basis_total),
                                "value": _int(p.value),
                                "last_decision_date": (
                                    last_decision.isoformat() if last_decision else "never"
                                ),
                            },
                        )
                    )

    tips.sort(key=lambda t: (_severity_rank(t.severity), t.category, t.ticker))

    return CoachingResult(
        generated_at=datetime.now(UTC),
        rubric_summary=_rubric_summary(),
        tips=tips,
        positions_evaluated=evaluated,
        rubric=_rubric_payload(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aggregate_positions(
    session: Session, accts: frozenset[int], today: date
) -> dict[str, _PositionState]:
    """Roll up the latest snapshot + lifetime tx history per ticker.

    Cost basis is merged with `cost_basis_overrides`: where the user has
    set a manual override (or an `inferred_acats` row exists) the
    override wins over the broker-reported snapshot value. Same policy
    Holdings + Trade Analysis + Trade Timeline use — keeps drawdown and
    multiples-detachment tips computed against the same cost basis the
    UI shows elsewhere.
    """
    overrides: dict[tuple[int, int], Decimal] = {
        (o.account_id, o.security_id): Decimal(o.total_cost_basis)
        for o in session.execute(select(CostBasisOverride)).scalars().all()
    }
    snap_rows = session.execute(
        select(
            Security.ticker,
            Security.name,
            HoldingSnapshot.account_id,
            HoldingSnapshot.security_id,
            HoldingSnapshot.quantity,
            HoldingSnapshot.institution_value,
            HoldingSnapshot.cost_basis,
        )
        .join(HoldingSnapshot, HoldingSnapshot.security_id == Security.security_id)
        .where(HoldingSnapshot.snapshot_date == today)
        .where(HoldingSnapshot.account_id.in_(accts))
        .where(Security.ticker.is_not(None))
        .where(Security.is_cash_equivalent.is_(False))
    ).all()

    by_ticker: dict[str, _PositionState] = {}
    for ticker, name, account_id, security_id, qty, val, cb in snap_rows:
        if not ticker:
            continue
        t = ticker.upper()
        p = by_ticker.get(t)
        if p is None:
            p = _PositionState(
                ticker=t,
                name=name,
                qty=Decimal(0),
                value=Decimal(0),
                cost_basis_total=Decimal(0),
                first_buy=None,
                last_action=None,
                bought_total=Decimal(0),
                sold_total=Decimal(0),
            )
            by_ticker[t] = p
        p.qty += Decimal(qty or 0)
        if val is not None:
            p.value += Decimal(val)
        # Override wins over broker-reported cost basis. Without this,
        # ACATS-in positions with $0 broker cost inflate drawdown +
        # multiples-detachment tips.
        override = overrides.get((account_id, security_id))
        if override is not None:
            p.cost_basis_total += override
        elif cb is not None:
            p.cost_basis_total += Decimal(cb)

    # Lifetime buy/sell aggregates + first_buy / last_action
    tx_rows = session.execute(
        select(
            Security.ticker,
            InvestmentTransaction.date,
            InvestmentTransaction.type,
            InvestmentTransaction.amount,
        )
        .join(InvestmentTransaction, InvestmentTransaction.security_id == Security.security_id)
        .where(InvestmentTransaction.account_id.in_(accts))
        .where(Security.is_cash_equivalent.is_(False))
        .where(Security.ticker.is_not(None))
    ).all()
    for ticker, tx_date, tx_type, amount in tx_rows:
        if not ticker or amount is None:
            continue
        t = ticker.upper()
        p = by_ticker.get(t)
        if p is None:
            continue  # only care about tickers currently held
        mag = abs(Decimal(amount))
        if tx_type == InvestmentTransactionType.BUY.value:
            p.bought_total += mag
            if p.first_buy is None or tx_date < p.first_buy:
                p.first_buy = tx_date
            if p.last_action is None or tx_date > p.last_action:
                p.last_action = tx_date
        elif tx_type == InvestmentTransactionType.SELL.value:
            p.sold_total += mag
            if p.last_action is None or tx_date > p.last_action:
                p.last_action = tx_date

    return by_ticker


def _latest_snapshot_date(session: Session, accts: frozenset[int]) -> date | None:
    return session.execute(
        select(HoldingSnapshot.snapshot_date)
        .where(HoldingSnapshot.account_id.in_(accts))
        .order_by(HoldingSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def _last_decisions_by_ticker(session: Session) -> dict[str, date]:
    """Latest decision_date per ticker (any action). Tickers with no log
    are absent from the dict (caller treats absence as 'stale')."""
    rows = session.execute(
        select(TradeDecision.ticker, TradeDecision.decision_date).order_by(
            TradeDecision.decision_date.desc()
        )
    ).all()
    out: dict[str, date] = {}
    for ticker, decision_date in rows:
        key = ticker.upper()
        if key not in out:
            out[key] = decision_date
    return out


def _last_trim_or_sell_by_ticker(session: Session) -> dict[str, date]:
    """Latest decision_date per ticker where action ∈ {trim, sell}."""
    rows = session.execute(
        select(TradeDecision.ticker, TradeDecision.decision_date)
        .where(TradeDecision.action.in_(["trim", "sell"]))
        .order_by(TradeDecision.decision_date.desc())
    ).all()
    out: dict[str, date] = {}
    for ticker, decision_date in rows:
        key = ticker.upper()
        if key not in out:
            out[key] = decision_date
    return out


def _human_capital_bucket(ticker: str) -> str | None:
    """Return the human-capital overlap bucket name (or None if uncorrelated)."""
    if ticker in _BIG_TECH_ADS_OVERLAP:
        return "Big Tech / Ads (Meta)"
    if ticker in _STARTUP_VC_OVERLAP:
        return "Startup / VC ecosystem"
    return None


def _severity_rank(s: TipSeverity) -> int:
    return {"high": 0, "warning": 1, "info": 2}[s]


def _int(d: Decimal) -> str:
    """Round + format a Decimal for the headline. Magnitude only."""
    return f"{int(abs(d).quantize(Decimal('1'))):,}"


def _rubric_summary() -> str:
    return (
        f"Non-index positions need ≥{IRR_FLOOR_PCT}% IRR over "
        f"{MIN_HOLD_YEARS_FOR_IRR}+ year holds. Single-name cap "
        f"{CONCENTRATION_MAX_PCT}%; theses ≤{THESIS_STALE_DAYS}d stale. "
        f"Trim at {DETACHMENT_FACTOR}× detachment. Audit theses on "
        f"{DRAWDOWN_AUDIT_PCT}% drawdowns. See CIO_CONTEXT.md."
    )


def _rubric_payload() -> CoachingRubric:
    return CoachingRubric(
        irr_floor_pct=float(IRR_FLOOR_PCT),
        min_hold_years_for_irr_check=float(MIN_HOLD_YEARS_FOR_IRR),
        concentration_max_pct=float(CONCENTRATION_MAX_PCT),
        thesis_stale_days=THESIS_STALE_DAYS,
        multiples_detachment_factor=float(DETACHMENT_FACTOR),
        drawdown_audit_pct=float(DRAWDOWN_AUDIT_PCT),
        drawdown_audit_window_days=DRAWDOWN_AUDIT_WINDOW_DAYS,
    )
