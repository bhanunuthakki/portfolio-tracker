"""Chronological trade timeline with SPY counterfactual.

Pulls authoritative closed-lot data from `tax_form_realized_lots` (1099-B) plus
currently-held open positions from `holdings_snapshots`, computes per-trade
alpha vs SPY for the same holding window, and returns the result in
chronological order so the UI can render a "what trades cost/earned alpha"
timeline.

For each row:
  spy_counterfactual = cost_basis * (SPY_at_disposed / SPY_at_acquired - 1)
  alpha              = realized_gain - spy_counterfactual

For closed lots with "Various" acquired_date (multi-tax-lot rollups in the
1099), we substitute (disposed_date - 180 days) as a rough mid-point proxy so
the SPY benchmark window is reasonable. Those rows are flagged
`acquired_approx=True` so the UI can render them differently.

For open positions, "acquired_date" = first_buy from investment_transactions;
"disposed_date" = today; "proceeds" = current market value.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    Benchmark,
    CostBasisOverride,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
    TaxFormImport,
    TaxFormRealizedLot,
)
from portfolio_tracker.services.active_items import active_account_ids

_VARIOUS_PROXY_DAYS: int = 180  # treat "Various" as ~6mo before disposed
_SPY_SYMBOL: str = "SPY"


class TimelineRow(BaseModel):
    # Identity
    row_kind: str                       # "closed" | "open"
    ticker: str | None
    name: str | None
    description: str                    # display name (incl. option strings)

    # Dates
    acquired_date: date | None          # actual or proxy
    disposed_date: date                 # disposal or today (for open)
    acquired_approx: bool               # True if Various/proxy
    holding_days: int

    # Money (all Decimal, JSON-serialized as strings)
    quantity: Decimal | None
    proceeds: Decimal                   # 1099 proceeds or current mkt value
    cost_basis: Decimal
    realized_gain: Decimal              # gain_or_loss from 1099 OR unrealized
    return_pct: float | None            # gain / cost_basis (None if cost=0)

    # SPY counterfactual
    spy_start_price: Decimal | None
    spy_end_price: Decimal | None
    spy_return_pct: float | None
    spy_counterfactual_dollars: Decimal | None
    alpha_dollars: Decimal | None       # gain - spy_counterfactual

    # Provenance
    source: str                         # "1099" | "broker"
    broker: str | None                  # for 1099 rows
    tax_year: int | None
    term: str | None                    # short / long / None for open


class YearSummary(BaseModel):
    year: int
    closed_count: int
    realized_total: Decimal
    spy_counterfactual_total: Decimal
    alpha_total: Decimal


class TradeTimelineResult(BaseModel):
    rows: list[TimelineRow]
    by_year: list[YearSummary]
    notes: list[str]


@dataclass
class _SpyLookup:
    """Lazy SPY price cache. Walks backward to nearest trading day if a
    weekend/holiday date is asked for."""

    session: Session
    _cache: dict[date, Decimal | None]

    @classmethod
    def for_session(cls, session: Session) -> "_SpyLookup":
        return cls(session=session, _cache={})

    def price_on(self, d: date | None) -> Decimal | None:
        if d is None:
            return None
        if d in self._cache:
            return self._cache[d]
        # Try exact date first
        price = self.session.execute(
            select(Benchmark.close)
            .where(Benchmark.symbol == _SPY_SYMBOL)
            .where(Benchmark.date == d)
        ).scalar_one_or_none()
        if price is None:
            # Walk backward up to 7 days to find nearest trading day
            price = self.session.execute(
                select(Benchmark.close)
                .where(Benchmark.symbol == _SPY_SYMBOL)
                .where(Benchmark.date <= d)
                .where(Benchmark.date >= d - timedelta(days=7))
                .order_by(Benchmark.date.desc())
                .limit(1)
            ).scalar_one_or_none()
        self._cache[d] = price
        return price


def build_timeline(
    session: Session,
    year: int | None = None,
    include_open: bool = True,
) -> TradeTimelineResult:
    spy = _SpyLookup.for_session(session)
    notes: list[str] = []

    rows: list[TimelineRow] = []
    rows.extend(_closed_lot_rows(session, spy, year))
    if include_open:
        rows.extend(_open_position_rows(session, spy))

    # Sort by disposed_date descending so most recent appears first.
    rows.sort(key=lambda r: r.disposed_date, reverse=True)

    by_year = _per_year_rollup(rows)

    if any(r.acquired_approx for r in rows):
        notes.append(
            "Some 1099 lots have 'Various' acquired dates (multi-lot rollups). "
            "SPY counterfactual uses disposed_date - 180d as an acquired-date proxy "
            "for those rows; alpha is approximate."
        )
    if include_open and any(r.row_kind == "open" for r in rows):
        notes.append(
            "Open positions show unrealized P&L vs. SPY using first_buy as the "
            "benchmark start date. Cost basis is from holdings snapshots (broker-reported)."
        )

    return TradeTimelineResult(rows=rows, by_year=by_year, notes=notes)


def _closed_lot_rows(
    session: Session,
    spy: _SpyLookup,
    year: int | None,
) -> list[TimelineRow]:
    stmt = (
        select(TaxFormRealizedLot, TaxFormImport)
        .join(TaxFormImport, TaxFormImport.import_id == TaxFormRealizedLot.import_id)
        .order_by(TaxFormRealizedLot.disposed_date.desc())
    )
    if year is not None:
        stmt = stmt.where(TaxFormImport.tax_year == year)

    out: list[TimelineRow] = []
    for lot, imp in session.execute(stmt).all():
        if lot.disposed_date is None:
            continue  # broken row; skip
        acquired = lot.acquired_date
        acquired_approx = lot.acquired_various or acquired is None
        if acquired_approx:
            acquired = lot.disposed_date - timedelta(days=_VARIOUS_PROXY_DAYS)

        holding_days = (lot.disposed_date - acquired).days if acquired else 0

        cost_basis = lot.cost_basis or Decimal("0")
        gain = lot.net_gain_loss or Decimal("0")
        return_pct = (
            float(gain / cost_basis) if cost_basis and cost_basis != 0 else None
        )

        spy_start = spy.price_on(acquired)
        spy_end = spy.price_on(lot.disposed_date)
        spy_return_pct: float | None = None
        spy_counterfactual: Decimal | None = None
        alpha: Decimal | None = None
        if spy_start and spy_end and spy_start > 0:
            spy_return_pct = float(spy_end / spy_start) - 1.0
            spy_counterfactual = cost_basis * (
                Decimal(str(spy_end)) / Decimal(str(spy_start)) - Decimal("1")
            )
            alpha = gain - spy_counterfactual

        out.append(
            TimelineRow(
                row_kind="closed",
                ticker=lot.symbol,
                name=None,
                description=lot.description,
                acquired_date=acquired,
                disposed_date=lot.disposed_date,
                acquired_approx=acquired_approx,
                holding_days=holding_days,
                quantity=lot.quantity,
                proceeds=lot.proceeds or Decimal("0"),
                cost_basis=cost_basis,
                realized_gain=gain,
                return_pct=return_pct,
                spy_start_price=spy_start,
                spy_end_price=spy_end,
                spy_return_pct=spy_return_pct,
                spy_counterfactual_dollars=spy_counterfactual,
                alpha_dollars=alpha,
                source="1099",
                broker=imp.broker,
                tax_year=imp.tax_year,
                term=lot.term,
            )
        )
    return out


def _open_position_rows(
    session: Session,
    spy: _SpyLookup,
) -> list[TimelineRow]:
    """Currently-held positions (latest snapshot) with unrealized P&L and SPY
    benchmark from first-buy date."""
    active_accts = active_account_ids(session)
    if not active_accts:
        return []

    # Latest snapshot per (account_id, security_id) — same pattern as portfolio.py
    latest_dates_sq = (
        select(
            HoldingSnapshot.account_id,
            HoldingSnapshot.security_id,
            func.max(HoldingSnapshot.snapshot_date).label("max_date"),
        )
        .where(HoldingSnapshot.account_id.in_(active_accts))
        .group_by(HoldingSnapshot.account_id, HoldingSnapshot.security_id)
        .subquery()
    )
    stmt = (
        select(HoldingSnapshot, Security)
        .join(Security, Security.security_id == HoldingSnapshot.security_id)
        .join(
            latest_dates_sq,
            and_(
                HoldingSnapshot.account_id == latest_dates_sq.c.account_id,
                HoldingSnapshot.security_id == latest_dates_sq.c.security_id,
                HoldingSnapshot.snapshot_date == latest_dates_sq.c.max_date,
            ),
        )
        .where(HoldingSnapshot.quantity > 0)
    )

    # Cost-basis overrides keyed on (account_id, security_id) — same merge
    # policy as `portfolio.py` and `trade_analysis.py`. Override wins over
    # the broker-reported `snap.cost_basis`. Without this, ACATS-in
    # positions (where the receiving broker reports $0 cost) inflate alpha
    # spuriously on the timeline. See data_quality finding
    # `override_disagrees_with_broker` for visibility into divergence.
    overrides: dict[tuple[int, int], Decimal] = {
        (o.account_id, o.security_id): Decimal(o.total_cost_basis)
        for o in session.execute(select(CostBasisOverride)).scalars().all()
    }

    # Aggregate by ticker (collapse across accounts).
    by_ticker: dict[str, dict] = defaultdict(
        lambda: {"qty": Decimal("0"), "value": Decimal("0"), "cost": Decimal("0"), "name": None}
    )
    for snap, sec in session.execute(stmt).all():
        if not sec.ticker:
            continue
        agg = by_ticker[sec.ticker]
        agg["qty"] += snap.quantity or Decimal("0")
        if snap.institution_value is not None:
            agg["value"] += snap.institution_value
        elif snap.market_value is not None:
            agg["value"] += snap.market_value
        # Per-account cost basis: override wins over broker-reported.
        override = overrides.get((snap.account_id, snap.security_id))
        if override is not None:
            agg["cost"] += override
        elif snap.cost_basis is not None:
            agg["cost"] += snap.cost_basis
        agg["name"] = agg["name"] or sec.name

    if not by_ticker:
        return []

    # Pull first-buy date per ticker for the benchmark window.
    first_buys = _first_buy_per_ticker(session, active_accts, list(by_ticker.keys()))

    today = date.today()
    out: list[TimelineRow] = []
    for ticker, agg in by_ticker.items():
        if agg["qty"] == 0 or agg["value"] == 0:
            continue
        cost = agg["cost"] or Decimal("0")
        value = agg["value"]
        # Skip rows where cost basis is clearly missing/garbage from the
        # broker snapshot — alpha would be meaningless and dominate the
        # display (e.g. VTI showing 200,000% "gain" because one fractional
        # buy is the only cost-bearing row). Heuristic: cost must be at
        # least $500 AND at least 5% of value. Filters out the obvious
        # broken rows; might also filter rare legit 20-baggers, that's OK.
        if cost < Decimal("500") or cost * Decimal("20") < value:
            continue
        unrealized = value - cost
        return_pct = float(unrealized / cost) if cost > 0 else None

        first_buy = first_buys.get(ticker) or (today - timedelta(days=365))
        holding_days = (today - first_buy).days

        spy_start = spy.price_on(first_buy)
        spy_end = spy.price_on(today)
        spy_return_pct: float | None = None
        spy_counterfactual: Decimal | None = None
        alpha: Decimal | None = None
        if spy_start and spy_end and spy_start > 0 and cost > 0:
            spy_return_pct = float(spy_end / spy_start) - 1.0
            spy_counterfactual = cost * (
                Decimal(str(spy_end)) / Decimal(str(spy_start)) - Decimal("1")
            )
            alpha = unrealized - spy_counterfactual

        out.append(
            TimelineRow(
                row_kind="open",
                ticker=ticker,
                name=agg["name"],
                description=agg["name"] or ticker,
                acquired_date=first_buy,
                disposed_date=today,
                acquired_approx=ticker not in first_buys,
                holding_days=holding_days,
                quantity=agg["qty"],
                proceeds=value,
                cost_basis=cost,
                realized_gain=unrealized,
                return_pct=return_pct,
                spy_start_price=spy_start,
                spy_end_price=spy_end,
                spy_return_pct=spy_return_pct,
                spy_counterfactual_dollars=spy_counterfactual,
                alpha_dollars=alpha,
                source="broker",
                broker=None,
                tax_year=None,
                term=None,
            )
        )
    return out


def _first_buy_per_ticker(
    session: Session,
    account_ids: list[int],
    tickers: list[str],
) -> dict[str, date]:
    if not tickers:
        return {}
    rows = session.execute(
        select(
            Security.ticker,
            func.min(InvestmentTransaction.date).label("first_buy"),
        )
        .join(Security, Security.security_id == InvestmentTransaction.security_id)
        .where(InvestmentTransaction.account_id.in_(account_ids))
        .where(Security.ticker.in_(tickers))
        .where(InvestmentTransaction.type == "buy")
        .group_by(Security.ticker)
    ).all()
    return {t: d for t, d in rows if d is not None}


def _per_year_rollup(rows: Iterable[TimelineRow]) -> list[YearSummary]:
    """Annual totals across closed-lot rows only (open positions are
    point-in-time and don't roll into a tax year)."""
    bucket: dict[int, dict[str, Decimal | int]] = defaultdict(
        lambda: {"count": 0, "realized": Decimal("0"), "spy": Decimal("0"), "alpha": Decimal("0")}
    )
    for r in rows:
        if r.row_kind != "closed":
            continue
        yr = r.disposed_date.year
        bucket[yr]["count"] = int(bucket[yr]["count"]) + 1  # type: ignore[arg-type]
        bucket[yr]["realized"] += r.realized_gain
        if r.spy_counterfactual_dollars is not None:
            bucket[yr]["spy"] += r.spy_counterfactual_dollars
        if r.alpha_dollars is not None:
            bucket[yr]["alpha"] += r.alpha_dollars
    return [
        YearSummary(
            year=yr,
            closed_count=int(b["count"]),  # type: ignore[arg-type]
            realized_total=Decimal(b["realized"]),
            spy_counterfactual_total=Decimal(b["spy"]),
            alpha_total=Decimal(b["alpha"]),
        )
        for yr, b in sorted(bucket.items())
    ]
