"""Data-quality findings surfaced to the user.

The point of this module is honesty: when our derived numbers (TWR,
unrealized P&L, weighted-avg cost) depend on data we don't have or had
to model, we say so explicitly. Each finding includes a recommended
action where one exists.

The report is read-only. Findings are computed on demand — no caching.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    CostBasisOverride,
    HoldingSnapshot,
    Item,
    Price,
    Security,
    TickerOverride,
)
from portfolio_tracker.schemas import DataQualityFindingOut, DataQualityReportOut
from portfolio_tracker.services.performance import (
    _ABNORMAL_DAILY_RETURN,
    _daily_external_cashflows,
    _daily_portfolio_value,
)

# --- severity values --------------------------------------------------------

INFO = "info"
WARNING = "warning"
ERROR = "error"

# --- finding categories -----------------------------------------------------

MISSING_COST_BASIS = "missing_cost_basis"
UNTICKERED_SECURITY = "untickered_security"
NO_HISTORICAL_PRICES = "no_historical_prices"
ANOMALOUS_BACKFILL_DAY = "anomalous_backfill_day"
STALE_ITEM = "stale_item"
SPARSE_FORWARD_SNAPSHOTS = "sparse_forward_snapshots"


def build_report(session: Session) -> DataQualityReportOut:
    findings: list[DataQualityFindingOut] = []
    findings.extend(_find_missing_cost_basis(session))
    findings.extend(_find_untickered_securities(session))
    findings.extend(_find_securities_without_prices(session))
    findings.extend(_find_anomalous_backfill_days(session))
    findings.extend(_find_stale_items(session))
    findings.extend(_find_sparse_forward_snapshots(session))

    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f.severity] += 1

    return DataQualityReportOut(
        generated_at=datetime.now(timezone.utc),
        findings=findings,
        summary_counts=dict(counts),
    )


# --- finders ---------------------------------------------------------------


def _find_missing_cost_basis(session: Session) -> list[DataQualityFindingOut]:
    """Per-account holdings where the broker didn't provide cost basis AND
    the user hasn't set a manual override.
    """
    latest_date = session.execute(
        select(func.max(HoldingSnapshot.snapshot_date))
    ).scalar_one_or_none()
    if latest_date is None:
        return []

    overridden_keys = {
        (ov.account_id, ov.security_id)
        for ov in session.execute(select(CostBasisOverride)).scalars().all()
    }

    rows = session.execute(
        select(HoldingSnapshot, Account, Security)
        .join(Account, Account.account_id == HoldingSnapshot.account_id)
        .join(Security, Security.security_id == HoldingSnapshot.security_id)
        .where(HoldingSnapshot.snapshot_date == latest_date)
        .where(HoldingSnapshot.cost_basis.is_(None))
        .where(HoldingSnapshot.quantity > 0)
    ).all()

    findings: list[DataQualityFindingOut] = []
    for h, a, s in rows:
        if (a.account_id, s.security_id) in overridden_keys:
            continue
        ticker = s.ticker or "unknown"
        current_price = (
            float(h.institution_price) if h.institution_price is not None else None
        )
        price_hint = (
            f" Current price ≈ ${current_price:.2f}/share."
            if current_price is not None
            else ""
        )
        findings.append(
            DataQualityFindingOut(
                category=MISSING_COST_BASIS,
                severity=INFO,
                title=f"{ticker} in {a.name}: no cost basis from broker",
                detail=(
                    f"{float(h.quantity):,.2f} shares of {ticker} ({s.name or 'unnamed'}) "
                    f"in {a.name}, current value "
                    f"${float(h.institution_value or 0):,.0f}.{price_hint} "
                    f"The broker didn't supply cost basis through Plaid, so "
                    f"unrealized P&L can't be computed for this position."
                ),
                recommended_action=(
                    "Look up the total amount you paid (price × shares + fees) "
                    "from your brokerage statements or the Transactions page. "
                    "Enter it as TOTAL DOLLARS PAID. Once set, weighted-avg "
                    "cost and unrealized P&L will populate."
                ),
                context={
                    "ticker": ticker,
                    "account_id": str(a.account_id),
                    "account_name": a.name,
                    "security_id": str(s.security_id),
                    "security_name": s.name or "",
                    "quantity": str(h.quantity),
                    "institution_value": str(h.institution_value or ""),
                    "institution_price": str(h.institution_price or ""),
                    "override_endpoint": "/api/overrides/cost-basis",
                },
            )
        )
    return findings


def _find_untickered_securities(session: Session) -> list[DataQualityFindingOut]:
    """Securities Plaid returned without a recognizable ticker AND no user
    override has been set.

    Common causes: OCC option contracts (no ticker by design), mutual
    funds with internal codes, foreign listings yfinance doesn't carry.
    Doesn't affect today's valuation (Plaid provides current price) but
    blocks historical backfill for that position.
    """
    latest_date = session.execute(
        select(func.max(HoldingSnapshot.snapshot_date))
    ).scalar_one_or_none()
    if latest_date is None:
        return []

    overridden_security_ids = {
        ov.security_id
        for ov in session.execute(select(TickerOverride)).scalars().all()
    }

    rows = session.execute(
        select(Security, func.sum(HoldingSnapshot.institution_value))
        .join(HoldingSnapshot, HoldingSnapshot.security_id == Security.security_id)
        .where(HoldingSnapshot.snapshot_date == latest_date)
        .where(Security.ticker.is_(None))
        .where(HoldingSnapshot.quantity > 0)
        .group_by(Security.security_id)
    ).all()

    findings: list[DataQualityFindingOut] = []
    for s, total_value in rows:
        if s.security_id in overridden_security_ids:
            continue
        findings.append(
            DataQualityFindingOut(
                category=UNTICKERED_SECURITY,
                severity=INFO,
                title=f"Security '{s.name or s.plaid_security_id}' has no ticker",
                detail=(
                    f"Plaid identified this security as "
                    f"{s.name or s.plaid_security_id!r} with no ticker symbol. "
                    f"Currently held value: ${float(total_value or 0):,.0f}. "
                    f"Common cause: OCC options contracts, mutual funds with "
                    f"non-public codes, or thinly traded foreign listings."
                ),
                recommended_action=(
                    "If this is a public security with a yfinance-compatible "
                    "ticker (e.g., 'AAPL', '7203.T' for Tokyo-listed Toyota), "
                    "enter it below and re-run the prices job. Leave blank for "
                    "true non-tickered instruments like options."
                ),
                context={
                    "security_id": str(s.security_id),
                    "plaid_security_id": s.plaid_security_id,
                    "security_name": s.name or "",
                    "override_endpoint": "/api/overrides/ticker",
                },
            )
        )
    return findings


def _find_securities_without_prices(session: Session) -> list[DataQualityFindingOut]:
    """Securities WITH a ticker but yfinance returned no price history.

    Distinct from untickered: these have a ticker we tried to fetch but
    couldn't resolve. Common cause: delisted issues, foreign-listed
    versions yfinance doesn't carry.
    """
    latest_date = session.execute(
        select(func.max(HoldingSnapshot.snapshot_date))
    ).scalar_one_or_none()
    if latest_date is None:
        return []

    held_securities = session.execute(
        select(Security)
        .join(HoldingSnapshot, HoldingSnapshot.security_id == Security.security_id)
        .where(HoldingSnapshot.snapshot_date == latest_date)
        .where(Security.ticker.is_not(None))
        .where(Security.is_cash_equivalent.is_(False))
        .where(HoldingSnapshot.quantity > 0)
        .group_by(Security.security_id)
    ).scalars().all()

    findings: list[DataQualityFindingOut] = []
    for s in held_securities:
        price_count = session.execute(
            select(func.count(Price.date)).where(Price.security_id == s.security_id)
        ).scalar_one()
        if price_count > 0:
            continue
        findings.append(
            DataQualityFindingOut(
                category=NO_HISTORICAL_PRICES,
                severity=WARNING,
                title=f"{s.ticker}: no historical price data on file",
                detail=(
                    f"Ticker {s.ticker} ({s.name or 'unnamed'}) was fetched "
                    f"but yfinance returned no daily close prices. Without "
                    f"prices, transaction-walk backfill can't value this "
                    f"position on past dates — its weight in your historical "
                    f"portfolio curve is implicitly 0."
                ),
                recommended_action=(
                    "Re-run `python -m portfolio_tracker.jobs.prices "
                    "--start 2024-01-01`. If still empty, the symbol may be "
                    "a foreign listing yfinance doesn't carry."
                ),
                context={"ticker": s.ticker or "", "name": s.name or ""},
            )
        )
    return findings


def _find_anomalous_backfill_days(session: Session) -> list[DataQualityFindingOut]:
    """Days in the reconstructed series with implausibly large daily returns.

    These are flagged inside `_chain_twr` already (TWR is held flat for
    them) but we still want the user to see the dates and the size of the
    discontinuity so they can investigate the underlying transactions.
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=730)
    daily_value = _daily_portfolio_value(session, start_date, end_date)
    if len(daily_value) < 2:
        return []
    daily_cf = _daily_external_cashflows(session, start_date, end_date)
    sorted_dates = sorted(daily_value.keys())

    findings: list[DataQualityFindingOut] = []
    for i in range(1, len(sorted_dates)):
        d_prev, d_curr = sorted_dates[i - 1], sorted_dates[i]
        v_prev = daily_value[d_prev]
        v_curr = daily_value[d_curr]
        if v_prev <= 0:
            continue
        cf = daily_cf.get(d_curr, Decimal(0))
        raw_return = (v_curr - v_prev - cf) / v_prev
        if abs(raw_return) <= _ABNORMAL_DAILY_RETURN:
            continue
        findings.append(
            DataQualityFindingOut(
                category=ANOMALOUS_BACKFILL_DAY,
                severity=WARNING,
                title=(
                    f"{d_curr.isoformat()}: reconstructed value jumped "
                    f"{float(raw_return) * 100:+.0f}% with no recorded cashflow"
                ),
                detail=(
                    f"On {d_curr.isoformat()}, the transaction-walk "
                    f"reconstruction shows the portfolio going from "
                    f"${float(v_prev):,.0f} to ${float(v_curr):,.0f} "
                    f"(net cashflow on this date: ${float(cf):,.0f}). "
                    f"That's {float(raw_return) * 100:+.0f}% in a day — "
                    f"almost certainly an unrecorded position transfer or "
                    f"a transaction Plaid didn't capture. The TWR is held "
                    f"flat for this day to avoid compounding noise."
                ),
                recommended_action=(
                    "Check the Transactions page for this date. If there "
                    "was an ACATS transfer, gifted stock, or intra-account "
                    "move that isn't reflected, you can ignore this; the "
                    "TWR accounts for it. If it's a true data error, "
                    "report to Plaid."
                ),
                context={
                    "date": d_curr.isoformat(),
                    "prev_value": str(v_prev),
                    "curr_value": str(v_curr),
                    "raw_return": str(raw_return),
                },
            )
        )
    # Cap the list so a noisy backfill doesn't drown the report.
    return findings[:25]


def _find_stale_items(session: Session) -> list[DataQualityFindingOut]:
    """Items that haven't been refreshed in over a week."""
    threshold = datetime.now(timezone.utc) - timedelta(days=7)
    rows = session.execute(
        select(Item)
        .where(
            (Item.last_refreshed_at.is_(None))
            | (Item.last_refreshed_at < threshold)
        )
    ).scalars().all()

    findings: list[DataQualityFindingOut] = []
    for item in rows:
        last = (
            item.last_refreshed_at.isoformat()
            if item.last_refreshed_at is not None
            else "never"
        )
        findings.append(
            DataQualityFindingOut(
                category=STALE_ITEM,
                severity=INFO,
                title=f"{item.institution_name or 'Unknown institution'}: stale item",
                detail=(
                    f"Last refreshed: {last}. Items go stale if the "
                    f"snapshot job hasn't run, or if the broker connection "
                    f"requires re-authentication."
                ),
                recommended_action=(
                    "Run `python -m portfolio_tracker.jobs.snapshot`. "
                    "If the run reports auth errors, re-link the Item via "
                    "the Accounts page."
                ),
                context={
                    "item_id": str(item.item_id),
                    "source": item.source,
                    "institution": item.institution_name or "",
                },
            )
        )
    return findings


def _find_sparse_forward_snapshots(session: Session) -> list[DataQualityFindingOut]:
    """Flag the early-stage state where the portfolio chart relies on backfill."""
    distinct_dates = session.execute(
        select(func.count(func.distinct(HoldingSnapshot.snapshot_date)))
    ).scalar_one()
    if distinct_dates >= 7:
        return []
    return [
        DataQualityFindingOut(
            category=SPARSE_FORWARD_SNAPSHOTS,
            severity=INFO,
            title=(
                f"Only {distinct_dates} day(s) of forward snapshots exist"
            ),
            detail=(
                f"The performance chart currently falls back to a 365-day "
                f"transaction-walk reconstruction because we don't have "
                f"enough observed snapshots to plot. Backfilled values are "
                f"modeled from Plaid's transaction history and will drift "
                f"on incomplete records."
            ),
            recommended_action=(
                "Schedule the snapshot job daily. After ~1 week of forward "
                "snapshots, the chart switches to observed data automatically."
            ),
            context={"distinct_snapshot_dates": str(distinct_dates)},
        )
    ]
