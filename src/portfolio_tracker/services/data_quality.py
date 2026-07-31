"""Data-quality findings surfaced to the user.

The point of this module is honesty: when our derived numbers (TWR,
unrealized P&L, weighted-avg cost) depend on data we don't have or had
to model, we say so explicitly. Each finding includes a recommended
action where one exists.

The report is read-only. Findings are computed on demand — no caching.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    Benchmark,
    CostBasisOverride,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    PolicyWeight,
    Price,
    Security,
    StockSplit,
    TickerOverride,
)
from portfolio_tracker.schemas import DataQualityFindingOut, DataQualityReportOut
from portfolio_tracker.services.active_items import active_account_ids, valued_account_ids
from portfolio_tracker.services.performance import (
    _ABNORMAL_DAILY_RETURN,  # pyright: ignore[reportPrivateUsage]
    _daily_external_cashflows,  # pyright: ignore[reportPrivateUsage]
    _daily_portfolio_value,  # pyright: ignore[reportPrivateUsage]
    _reverse_transaction_quantity,  # pyright: ignore[reportPrivateUsage]
    partial_snapshot_dates,
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
OVERLAPPING_BROKER_CONNECTIONS = "overlapping_broker_connections"
MISSING_POLICY_BENCHMARK = "missing_policy_benchmark"
OVERRIDE_DISAGREES_WITH_BROKER = "override_disagrees_with_broker"
UNEXPLAINED_HOLDINGS_CHANGE = "unexplained_holdings_change"
CASHFLOW_WITHOUT_VALUE = "cashflow_without_value"
PARTIAL_SNAPSHOT_DAY = "partial_snapshot_day"

# Lookback for the "holdings moved with no trade behind them" check. Long
# enough to catch a feed that quietly stopped weeks ago, short enough that a
# single old gap doesn't shout forever after it's been backfilled.
_UNEXPLAINED_CHANGE_LOOKBACK_DAYS = 60

# A position's share count is "unexplained" when the observed change and the
# change implied by its transactions differ by more than BOTH of these. The
# fractional floor absorbs fractional-share dividend reinvestment and broker
# rounding; the absolute floor stops sub-share noise on tiny positions from
# generating findings.
_UNEXPLAINED_QTY_TOLERANCE_FRACTION: Decimal = Decimal("0.01")
_UNEXPLAINED_QTY_TOLERANCE_SHARES: Decimal = Decimal("0.5")

# Tolerance for "broker disagrees with override" comparisons. Brokers
# round and may differ by rounding noise from the user's stated total —
# below this fraction we treat the broker's value as effectively the
# same. 2% is wide enough to ignore typical rounding + small dividend
# reinvestment drift, narrow enough to catch real divergence ($1k on a
# $50k position).
_OVERRIDE_DRIFT_TOLERANCE: Decimal = Decimal("0.02")


def build_report(session: Session, now: datetime | None = None) -> DataQualityReportOut:
    """Every finding, as of `now` (defaults to wall-clock).

    `now` exists so the published v1 fixtures are reproducible. Staleness is
    the one check measured against the clock rather than against the data, so
    with a live `now()` the generated artifact drifted a little more each day
    and `test_fixtures_have_no_drift` failed on an untouched checkout roughly a
    week after any regeneration. Freezing the reference time makes the fixture
    a function of the seeded rows alone.
    """
    reference = now or datetime.now(UTC)
    findings: list[DataQualityFindingOut] = []
    findings.extend(_find_missing_cost_basis(session))
    findings.extend(_find_untickered_securities(session))
    findings.extend(_find_securities_without_prices(session))
    findings.extend(_find_anomalous_backfill_days(session))
    findings.extend(_find_stale_items(session, reference))
    findings.extend(_find_sparse_forward_snapshots(session))
    findings.extend(_find_overlapping_broker_connections(session))
    findings.extend(_find_missing_policy_benchmarks(session))
    findings.extend(_find_overrides_disagreeing_with_broker(session))
    findings.extend(_find_unexplained_holdings_changes(session))
    findings.extend(_find_cashflow_without_value(session))
    findings.extend(_find_partial_snapshot_days(session))

    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f.severity] += 1

    return DataQualityReportOut(
        generated_at=reference,
        findings=findings,
        summary_counts=dict(counts),
    )


# --- finders ---------------------------------------------------------------


def _find_missing_cost_basis(session: Session) -> list[DataQualityFindingOut]:
    """Per-account holdings where the broker didn't provide cost basis AND
    the user hasn't set a manual override.
    """
    accts = active_account_ids(session)
    if not accts:
        return []
    latest_date = session.execute(
        select(func.max(HoldingSnapshot.snapshot_date)).where(HoldingSnapshot.account_id.in_(accts))
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
        .where(HoldingSnapshot.account_id.in_(accts))
        .where(HoldingSnapshot.cost_basis.is_(None))
        .where(HoldingSnapshot.quantity > 0)
    ).all()

    findings: list[DataQualityFindingOut] = []
    for h, a, s in rows:
        if (a.account_id, s.security_id) in overridden_keys:
            continue
        ticker = s.ticker or "unknown"
        current_price = float(h.institution_price) if h.institution_price is not None else None
        price_hint = (
            f" Current price ≈ ${current_price:,.0f}/share." if current_price is not None else ""
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
    accts = active_account_ids(session)
    if not accts:
        return []
    latest_date = session.execute(
        select(func.max(HoldingSnapshot.snapshot_date)).where(HoldingSnapshot.account_id.in_(accts))
    ).scalar_one_or_none()
    if latest_date is None:
        return []

    overridden_security_ids = {
        ov.security_id for ov in session.execute(select(TickerOverride)).scalars().all()
    }

    rows = session.execute(
        select(Security, func.sum(HoldingSnapshot.institution_value))
        .join(HoldingSnapshot, HoldingSnapshot.security_id == Security.security_id)
        .where(HoldingSnapshot.snapshot_date == latest_date)
        .where(HoldingSnapshot.account_id.in_(accts))
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
    accts = active_account_ids(session)
    if not accts:
        return []
    latest_date = session.execute(
        select(func.max(HoldingSnapshot.snapshot_date)).where(HoldingSnapshot.account_id.in_(accts))
    ).scalar_one_or_none()
    if latest_date is None:
        return []

    held_securities = (
        session.execute(
            select(Security)
            .join(HoldingSnapshot, HoldingSnapshot.security_id == Security.security_id)
            .where(HoldingSnapshot.snapshot_date == latest_date)
            .where(HoldingSnapshot.account_id.in_(accts))
            .where(Security.ticker.is_not(None))
            .where(Security.is_cash_equivalent.is_(False))
            .where(HoldingSnapshot.quantity > 0)
            .group_by(Security.security_id)
        )
        .scalars()
        .all()
    )

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


def _find_stale_items(session: Session, now: datetime) -> list[DataQualityFindingOut]:
    """Items that haven't been refreshed in over a week, measured from `now`.

    Skips items the user has flagged as data-inactive — those are kept
    around for connection-slot reasons but explicitly aren't expected to
    influence the numbers, so a stale snapshot doesn't matter.
    """
    threshold = now - timedelta(days=7)
    rows = (
        session.execute(
            select(Item)
            .where(Item.is_data_active.is_(True))
            .where((Item.last_refreshed_at.is_(None)) | (Item.last_refreshed_at < threshold))
        )
        .scalars()
        .all()
    )

    findings: list[DataQualityFindingOut] = []
    for item in rows:
        last = item.last_refreshed_at.isoformat() if item.last_refreshed_at is not None else "never"
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
    accts = active_account_ids(session)
    if not accts:
        return []
    distinct_dates = session.execute(
        select(func.count(func.distinct(HoldingSnapshot.snapshot_date))).where(
            HoldingSnapshot.account_id.in_(accts)
        )
    ).scalar_one()
    if distinct_dates >= 7:
        return []
    return [
        DataQualityFindingOut(
            category=SPARSE_FORWARD_SNAPSHOTS,
            severity=INFO,
            title=(f"Only {distinct_dates} day(s) of forward snapshots exist"),
            detail=(
                "The performance chart currently falls back to a 365-day "
                "transaction-walk reconstruction because we don't have "
                "enough observed snapshots to plot. Backfilled values are "
                "modeled from Plaid's transaction history and will drift "
                "on incomplete records."
            ),
            recommended_action=(
                "Schedule the snapshot job daily. After ~1 week of forward "
                "snapshots, the chart switches to observed data automatically."
            ),
            context={"distinct_snapshot_dates": str(distinct_dates)},
        )
    ]


def _find_overlapping_broker_connections(
    session: Session,
) -> list[DataQualityFindingOut]:
    """Flag the same brokerage being reachable through two aggregators.

    When Robinhood is connected via *both* Plaid and SnapTrade, the same
    physical accounts get snapshotted twice and the same trades land in
    `investment_transactions` twice — every aggregation (V, P&L, turnover)
    silently double-counts. The fix is to mark one of the duplicate items
    `is_data_active=False`; this finding surfaces the situation so the
    user knows to do that.

    Heuristic: group by case-insensitive `institution_name`, count items
    where `is_data_active=True`. >1 active item per institution is the
    overlap.
    """
    rows = (
        session.execute(select(Item).order_by(Item.institution_name, Item.source)).scalars().all()
    )

    by_institution: dict[str, list[Item]] = defaultdict(list)
    for item in rows:
        key = (item.institution_name or "").strip().lower()
        if not key:
            continue
        by_institution[key].append(item)

    findings: list[DataQualityFindingOut] = []
    for _key, group in by_institution.items():
        active = [i for i in group if i.is_data_active]
        if len(active) <= 1:
            continue
        sources = ", ".join(sorted({i.source for i in active}))
        names = ", ".join(f"#{i.item_id}({i.source})" for i in active)
        findings.append(
            DataQualityFindingOut(
                category=OVERLAPPING_BROKER_CONNECTIONS,
                severity=WARNING,
                title=(
                    f"{group[0].institution_name}: connected via {sources} "
                    f"— data is being double-counted"
                ),
                detail=(
                    f"{len(active)} active items reference "
                    f"{group[0].institution_name}: {names}. The same physical "
                    f"accounts are being snapshotted by both, so holdings, "
                    f"transactions, V series, and trade-analysis all "
                    f"double-count this brokerage's activity. Mark one of "
                    f"the duplicates as data-inactive (PATCH "
                    f"/api/plaid/items/{{id}}/data-active) to keep the "
                    f"connection alive while excluding its data from "
                    f"aggregations."
                ),
                recommended_action=(
                    "Decide which aggregator should be the source of truth "
                    "(SnapTrade typically has more transaction history; "
                    "Plaid is more reliable for current-day holdings). Mark "
                    "the other one is_data_active=False on the Items page."
                ),
                context={
                    "institution": group[0].institution_name or "",
                    "active_item_ids": ",".join(str(i.item_id) for i in active),
                    "sources": sources,
                },
            )
        )
    return findings


def _find_missing_policy_benchmarks(
    session: Session,
) -> list[DataQualityFindingOut]:
    """Flag policy tickers with no rows in the `benchmarks` table.

    The synthetic policy line is the user's intended allocation valued
    at historical benchmark closes. If a policy ticker has no benchmark
    data (because the user added it after the last benchmarks-job run),
    the policy series silently renormalizes around the missing weight
    — which produces an approximated (not zero, but not exact) line.
    Run `python -m portfolio_tracker.jobs.benchmarks --start <date>` to
    fix.
    """
    policy_tickers = (
        session.execute(select(PolicyWeight.ticker).where(PolicyWeight.weight_bps > 0))
        .scalars()
        .all()
    )
    if not policy_tickers:
        return []

    found_symbols = set(
        session.execute(select(Benchmark.symbol).where(Benchmark.symbol.in_(policy_tickers)))
        .scalars()
        .all()
    )
    missing = [t for t in policy_tickers if t not in found_symbols]
    if not missing:
        return []

    return [
        DataQualityFindingOut(
            category=MISSING_POLICY_BENCHMARK,
            severity=WARNING,
            title=(f"Policy benchmark missing for: {', '.join(missing)}"),
            detail=(
                f"Your policy weights reference {len(missing)} ticker(s) "
                f"({', '.join(missing)}) that have no rows in the "
                f"`benchmarks` table. The synthetic-policy line on the "
                f"performance chart renormalizes around the missing "
                f"weights so the line still represents 100% deployed "
                f"capital, but it's an approximation of your true policy "
                f"mix until those benchmarks are pulled."
            ),
            recommended_action=(
                "Run `python -m portfolio_tracker.jobs.benchmarks "
                "--start 2024-01-01`. The benchmarks job pulls every "
                "ticker referenced in policy_weights automatically."
            ),
            context={"missing_tickers": ",".join(missing)},
        )
    ]


def _find_overrides_disagreeing_with_broker(
    session: Session,
) -> list[DataQualityFindingOut]:
    """Fires when a CostBasisOverride exists AND the broker now reports a
    plausible cost basis on the same (account, security) that disagrees
    with the user's override.

    Common trigger: SoFi via Plaid used to omit cost basis entirely, so
    the user entered an override. Months later Plaid backfills cost basis
    on SoFi's side; the override silently keeps winning even though the
    broker now has its own value. This finding surfaces the disagreement
    so the user can pick: trust the broker (delete the override), or
    keep the override (acknowledge and ignore).

    We compare against the most recent `holdings_snapshots.cost_basis`
    for the (account, security). Broker values of zero or NULL are
    treated as "broker doesn't have it" and skipped — the override is
    still the only source of truth in that case. The drift tolerance
    accounts for broker rounding + small in-period reinvestment that the
    user's static override wouldn't reflect.

    Severity is `info` rather than `warning`: a disagreement isn't a
    bug, just a decision point. The user may well prefer their override
    (e.g., they enriched ACATS-in cost with carryover from another
    broker). The finding's recommended_action is to reconcile, not to
    delete.
    """
    accts = active_account_ids(session)
    if not accts:
        return []

    overrides = session.execute(select(CostBasisOverride)).scalars().all()
    if not overrides:
        return []

    latest_date = session.execute(
        select(func.max(HoldingSnapshot.snapshot_date)).where(HoldingSnapshot.account_id.in_(accts))
    ).scalar_one_or_none()
    if latest_date is None:
        return []

    # Build a fast lookup: (account_id, security_id) -> (broker_cb, qty, value, ticker, account_name, security_name)
    snap_rows = session.execute(
        select(
            HoldingSnapshot.account_id,
            HoldingSnapshot.security_id,
            HoldingSnapshot.cost_basis,
            HoldingSnapshot.quantity,
            HoldingSnapshot.institution_value,
            Security.ticker,
            Security.name,
            Account.name,
        )
        .join(Security, Security.security_id == HoldingSnapshot.security_id)
        .join(Account, Account.account_id == HoldingSnapshot.account_id)
        .where(HoldingSnapshot.snapshot_date == latest_date)
        .where(HoldingSnapshot.account_id.in_(accts))
    ).all()
    by_key = {(row[0], row[1]): row[2:] for row in snap_rows}

    findings: list[DataQualityFindingOut] = []
    for ov in overrides:
        row = by_key.get((ov.account_id, ov.security_id))
        if row is None:
            # Override exists but position no longer held — not a quality
            # issue, just a stale override. Skip silently.
            continue
        broker_cb, qty, value, ticker, security_name, account_name = row
        if broker_cb is None:
            continue  # broker still doesn't have it; override is the only source
        broker_cb_dec = Decimal(broker_cb)
        if broker_cb_dec <= 0:
            continue  # $0 or negative is broker's "I don't have this" signal
        override_dec = Decimal(ov.total_cost_basis)
        if override_dec <= 0:
            continue  # safety; should never happen
        # Fractional drift against the larger value — symmetric.
        denom = max(override_dec, broker_cb_dec)
        drift = abs(broker_cb_dec - override_dec) / denom
        if drift <= _OVERRIDE_DRIFT_TOLERANCE:
            continue
        ticker_label = ticker or "unknown"
        findings.append(
            DataQualityFindingOut(
                category=OVERRIDE_DISAGREES_WITH_BROKER,
                severity=INFO,
                title=(f"{ticker_label} in {account_name}: override and broker disagree"),
                detail=(
                    f"Your override says ${float(override_dec):,.0f} total "
                    f"cost basis, but the broker now reports "
                    f"${float(broker_cb_dec):,.0f} "
                    f"(drift {float(drift) * 100:.1f}%). "
                    f"Source: {ov.source}. "
                    f"Position currently {float(qty or 0):,.4f} sh worth "
                    f"${float(value or 0):,.0f}. "
                    f"The override is still being used everywhere; "
                    f"this is just a heads-up."
                ),
                recommended_action=(
                    "Reconcile: if the broker is now correct (e.g., SoFi "
                    "finally backfilled Plaid cost basis), delete the "
                    "override at /api/overrides/cost-basis/{account_id}/"
                    "{security_id}. If your override is the right number "
                    "(ACATS carryover, manual reconciliation), no action "
                    "needed — note the broker disagrees so future re-syncs "
                    "don't surprise you."
                ),
                context={
                    "ticker": ticker_label,
                    "account_id": str(ov.account_id),
                    "account_name": account_name,
                    "security_id": str(ov.security_id),
                    "security_name": security_name or "",
                    "override_value": str(override_dec),
                    "broker_value": str(broker_cb_dec),
                    "drift_pct": f"{float(drift) * 100:.2f}",
                    "override_source": ov.source,
                    "override_endpoint": "/api/overrides/cost-basis",
                },
            )
        )
    return findings


def _find_unexplained_holdings_changes(
    session: Session,
) -> list[DataQualityFindingOut]:
    """Positions whose share count moved without transactions to explain it.

    This is the check that catches a silently-dead transaction feed, and it is
    the one the report was missing when that whole class of bug went unnoticed.

    Holdings and transactions arrive through independent code paths. Holdings
    come from a daily snapshot; transactions come from a separate pull that can
    stop — a job with no transaction leg at all, an aggregator that quietly
    drops investment activity, a connection needing re-auth for one scope but
    not the other. Nothing about the resulting state LOOKS broken: holdings are
    current, the value series is right, Modified Dietz keeps reporting sensible
    numbers, and the last transaction just happens to be old. `stale_item`
    doesn't fire, because the item IS refreshing.

    What breaks is every position-level engine. `position_alpha`,
    `exit_quality` and turnover all derive P&L from trades, so a position that
    appears with no buy behind it has an implied cost of zero and its entire
    market value is booked as profit. Observed on the live book at 2026-07-30:
    the Plaid-sourced accounts had had no transaction pull in ~3 months, 216
    shares of UBER and 38 of BKNG had materialised with no trade, and the
    panel's "Actual P&L" card read $56,655 against a true value-based gain of
    about $25k. The two headline numbers on the same panel disagreed by more
    than the entire real gain, which is the only reason it got caught.

    So: compare each position's observed share change against the change its
    transactions imply, over the same interval. Divergence means the two feeds
    have drifted apart. Direction matters for the diagnosis but not the
    severity — shares appearing inflates P&L, shares vanishing invents losses.

    Securities that split inside the window are skipped: a split moves share
    counts with no transaction by design, so it makes the comparison
    meaningless rather than merely noisy.
    """
    accts = valued_account_ids(session)
    if not accts:
        return []

    latest = session.execute(
        select(func.max(HoldingSnapshot.snapshot_date)).where(HoldingSnapshot.account_id.in_(accts))
    ).scalar_one_or_none()
    if latest is None:
        return []
    window_start = latest - timedelta(days=_UNEXPLAINED_CHANGE_LOOKBACK_DAYS)

    # Anchor on COMPLETE snapshots only. A partially-synced endpoint would show
    # every account that didn't report as having gone from nothing to a full
    # book, or vice versa.
    candidates = set(
        session.execute(
            select(HoldingSnapshot.snapshot_date)
            .where(HoldingSnapshot.snapshot_date >= window_start)
            .where(HoldingSnapshot.snapshot_date <= latest)
            .where(HoldingSnapshot.account_id.in_(accts))
            .distinct()
        )
        .scalars()
        .all()
    )
    complete = sorted(candidates - set(partial_snapshot_dates(session, candidates)))
    if len(complete) < 2:
        return []
    start_date, end_date = complete[0], complete[-1]

    observed: dict[tuple[int, int], list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for idx, on_date in ((0, start_date), (1, end_date)):
        for account_id, security_id, quantity in session.execute(
            select(
                HoldingSnapshot.account_id,
                HoldingSnapshot.security_id,
                HoldingSnapshot.quantity,
            )
            .where(HoldingSnapshot.snapshot_date == on_date)
            .where(HoldingSnapshot.account_id.in_(accts))
        ).all():
            observed[(account_id, security_id)][idx] += Decimal(quantity)

    implied: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    txs = (
        session.execute(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.date > start_date)
            .where(InvestmentTransaction.date <= end_date)
            .where(InvestmentTransaction.account_id.in_(accts))
            .where(InvestmentTransaction.security_id.is_not(None))
        )
        .scalars()
        .all()
    )
    for tx in txs:
        if tx.security_id is None:
            continue
        # `_reverse_transaction_quantity` returns the delta that UNDOES the
        # transaction, so the forward effect is its negation. Reusing it binds
        # this check to the same per-source sign conventions the walk-back
        # uses — if those change, both move together.
        reverse = _reverse_transaction_quantity(tx)
        if reverse is not None:
            implied[(tx.account_id, tx.security_id)] += -reverse

    split_sids = set(
        session.execute(
            select(StockSplit.security_id)
            .where(StockSplit.split_date > start_date)
            .where(StockSplit.split_date <= end_date)
            .distinct()
        )
        .scalars()
        .all()
    )

    # Cash equivalents are exempt. Their "quantity" is a dollar balance that
    # every transaction moves — a buy debits it, a dividend credits it, a fee
    # nibbles it — so it legitimately drifts against a share-delta comparison
    # that only counts position-changing events. Including them produced one
    # guaranteed false positive per cash-holding account and nothing else.
    cash_sids = set(
        session.execute(select(Security.security_id).where(Security.is_cash_equivalent.is_(True)))
        .scalars()
        .all()
    )

    account_names = _account_names(session, accts)
    security_labels = {
        security_id: ticker or name or f"security #{security_id}"
        for security_id, ticker, name in session.execute(
            select(Security.security_id, Security.ticker, Security.name)
        ).all()
    }

    findings: list[DataQualityFindingOut] = []
    for (account_id, security_id), (qty_start, qty_end) in sorted(observed.items()):
        if security_id in split_sids or security_id in cash_sids:
            continue
        actual = qty_end - qty_start
        expected = implied.get((account_id, security_id), Decimal(0))
        gap = actual - expected
        scale = max(abs(qty_end), abs(qty_start))
        if abs(gap) <= _UNEXPLAINED_QTY_TOLERANCE_SHARES:
            continue
        if abs(gap) <= scale * _UNEXPLAINED_QTY_TOLERANCE_FRACTION:
            continue

        label = security_labels.get(security_id, f"security #{security_id}")
        account = account_names.get(account_id, f"account #{account_id}")
        direction = "appeared without a purchase" if gap > 0 else "left without a sale"
        findings.append(
            DataQualityFindingOut(
                category=UNEXPLAINED_HOLDINGS_CHANGE,
                severity=WARNING,
                title=f"{label} in {account}: {abs(gap):,.4f} shares {direction}",
                detail=(
                    f"Between {start_date} and {end_date}, {label} in {account} went from "
                    f"{qty_start:,.4f} to {qty_end:,.4f} shares (a change of {actual:+,.4f}), "
                    f"but the recorded transactions only account for {expected:+,.4f}. "
                    f"{abs(gap):,.4f} shares are unexplained. Position-level P&L — the "
                    f"'Actual P&L' and 'Alpha vs SPY' cards, exit quality, turnover — "
                    f"treats shares with no purchase behind them as pure profit and shares "
                    f"that vanish as a loss, so those numbers are wrong for this holding. "
                    f"Modified Dietz reads value rather than trades and is unaffected; a "
                    f"large disagreement between the two is this bug."
                ),
                recommended_action=(
                    "Run `python -m portfolio_tracker.jobs.backfill` to re-pull Plaid "
                    "investment transactions, or `python -m portfolio_tracker.jobs."
                    "daily_refresh` for every source. If the gap survives a backfill the "
                    "broker isn't reporting those trades and they need a manual row."
                ),
                context={
                    "account_id": str(account_id),
                    "security_id": str(security_id),
                    "ticker": label,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "observed_change": f"{actual:.4f}",
                    "transaction_implied_change": f"{expected:.4f}",
                    "unexplained_shares": f"{gap:.4f}",
                },
            )
        )
    return findings


def _find_cashflow_without_value(session: Session) -> list[DataQualityFindingOut]:
    """Active accounts that emit external cashflow but never report holdings.

    Return math only holds when the value series and the cashflow series cover
    the same accounts. Modified Dietz is
    ``(V_end - V_start - sum(C)) / (V_start + sum(C_i * w_i))`` — an account
    contributing to C but never to V has its deposits subtracted as
    money-you-put-in while the assets they bought never appear as
    money-you-have, so every dollar saved there reads as a dollar lost.

    `valued_account_ids` already excludes these accounts from the performance
    series. This finding is what keeps that exclusion from being silent: the
    user should know their 401(k) sits outside the reported return rather than
    discovering it by reconciling by hand. The live case was the Fidelity-held
    META 401(k), which syncs payroll deferrals through SnapTrade but exposes no
    positions — $76,823 of contributions against zero snapshots, $11,651 of it
    inside the default window, dragging the reported return down ~1.8pp.
    """
    orphans = sorted(active_account_ids(session) - valued_account_ids(session))
    if not orphans:
        return []

    rows = session.execute(
        select(
            InvestmentTransaction.account_id,
            func.count(),
            func.min(InvestmentTransaction.date),
            func.max(InvestmentTransaction.date),
        )
        .where(InvestmentTransaction.account_id.in_(orphans))
        .group_by(InvestmentTransaction.account_id)
    ).all()
    activity = {account_id: (n, first, last) for account_id, n, first, last in rows}
    account_names = _account_names(session, frozenset(orphans))

    findings: list[DataQualityFindingOut] = []
    for account_id in orphans:
        if account_id not in activity:
            # Linked but entirely inert — no holdings AND no transactions. It
            # can't distort anything, so it isn't worth the user's attention.
            continue
        count, first, last = activity[account_id]
        name = account_names.get(account_id, f"account #{account_id}")
        findings.append(
            DataQualityFindingOut(
                category=CASHFLOW_WITHOUT_VALUE,
                severity=WARNING,
                title=f"{name}: transactions but no holdings — excluded from return math",
                detail=(
                    f"{name} has {count} transactions ({first} to {last}) but has never "
                    f"reported a holdings snapshot, so its assets are absent from the "
                    f"portfolio value series. Its contributions are therefore EXCLUDED "
                    f"from the performance calculation — counting them while their assets "
                    f"stay invisible would book real savings as investment loss. The "
                    f"consequence is that this account's balance and its returns are "
                    f"missing from every number on the Performance panel."
                ),
                recommended_action=(
                    "If the provider can expose positions for this account, re-link it so "
                    "holdings sync and it rejoins the return math. If it can't — many "
                    "employer plans only expose contributions — this is working as "
                    "intended; read the reported return as covering brokerage assets only."
                ),
                context={
                    "account_id": str(account_id),
                    "transaction_count": str(count),
                    "first_transaction": first.isoformat(),
                    "last_transaction": last.isoformat(),
                },
            )
        )
    return findings


def _find_partial_snapshot_days(session: Session) -> list[DataQualityFindingOut]:
    """Days where only some accounts reported, so V covered part of the book.

    A snapshot run can fail per-item and still write rows for the items that
    succeeded. The day's total is then the sum of an arbitrary slice of the
    portfolio — not a portfolio value. The performance series now drops these
    days (see `_forward_values_from_snapshots`); this finding is what makes the
    drop visible and names the sync that failed.

    Four such days existed on the live book, the worst reading $158,124 against
    $649,124 two sessions later — a fabricated 76% single-day drawdown that
    poisoned every volatility and drawdown statistic taken off the series.
    """
    accts = valued_account_ids(session)
    if not accts:
        return []
    latest = session.execute(
        select(func.max(HoldingSnapshot.snapshot_date)).where(HoldingSnapshot.account_id.in_(accts))
    ).scalar_one_or_none()
    if latest is None:
        return []

    candidates = set(
        session.execute(
            select(HoldingSnapshot.snapshot_date)
            .where(HoldingSnapshot.snapshot_date >= latest - timedelta(days=365))
            .where(HoldingSnapshot.account_id.in_(accts))
            .distinct()
        )
        .scalars()
        .all()
    )
    partial = partial_snapshot_dates(session, candidates)
    if not partial:
        return []
    account_names = _account_names(session, accts)

    findings: list[DataQualityFindingOut] = []
    for on_date, missing in sorted(partial.items()):
        names = ", ".join(
            sorted(account_names.get(account_id, f"#{account_id}") for account_id in missing)
        )
        findings.append(
            DataQualityFindingOut(
                category=PARTIAL_SNAPSHOT_DAY,
                severity=WARNING,
                title=f"{on_date}: {len(missing)} account(s) missing from the snapshot",
                detail=(
                    f"On {on_date} these accounts did not report holdings: {names}. The "
                    f"day's total would have been the sum of only part of the portfolio, "
                    f"so it is EXCLUDED from the value series rather than plotted as a "
                    f"crash and a recovery. Drawdown and volatility over any window "
                    f"containing this date skip the day entirely."
                ),
                recommended_action=(
                    "One aggregator leg of that day's refresh failed. If it keeps "
                    "happening for the same institution, check that connection's auth on "
                    "the Accounts page; a one-off is usually a provider timeout and needs "
                    "no action."
                ),
                context={
                    "date": on_date.isoformat(),
                    "missing_account_ids": ",".join(str(a) for a in sorted(missing)),
                    "missing_accounts": names,
                },
            )
        )
    return findings


def _account_names(session: Session, account_ids: frozenset[int]) -> dict[int, str]:
    """`account_id -> display name` for the given accounts."""
    return {
        account_id: name
        for account_id, name in session.execute(
            select(Account.account_id, Account.name).where(Account.account_id.in_(account_ids))
        ).all()
    }
