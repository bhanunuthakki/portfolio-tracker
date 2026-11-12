"""Data-quality findings surfaced to the user.

The point of this module is honesty: when our derived numbers (TWR,
unrealized P&L, weighted-avg cost) depend on data we don't have or had
to model, we say so explicitly. Each finding includes a recommended
action where one exists.

The report is read-only. Findings are computed on demand — no caching.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    Benchmark,
    CostBasisOverride,
    HoldingSnapshot,
    Item,
    PolicyWeight,
    Price,
    Security,
    TickerOverride,
)
from portfolio_tracker.schemas import DataQualityFindingOut, DataQualityReportOut
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.option_direction import Verdict, examine_option_rows
from portfolio_tracker.services.owner_rulings import zero_value_ruling_contradictions
from portfolio_tracker.services.performance import (
    _ABNORMAL_DAILY_RETURN,  # pyright: ignore[reportPrivateUsage]
    _daily_external_cashflows,  # pyright: ignore[reportPrivateUsage]
    _daily_portfolio_value,  # pyright: ignore[reportPrivateUsage]
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
OPTION_DIRECTION = "option_direction"
OWNER_RULING_CONTRADICTED = "owner_ruling_contradicted"

# Tolerance for "broker disagrees with override" comparisons. Brokers
# round and may differ by rounding noise from the user's stated total —
# below this fraction we treat the broker's value as effectively the
# same. 2% is wide enough to ignore typical rounding + small dividend
# reinvestment drift, narrow enough to catch real divergence ($1k on a
# $50k position).
_OVERRIDE_DRIFT_TOLERANCE: Decimal = Decimal("0.02")


def build_report(session: Session, today: date | None = None) -> DataQualityReportOut:
    """Compute every data-quality finding for the active book.

    `today` pins the two wall-clock-dependent finders — the staleness threshold
    and the backfill-anomaly window — so callers that need a REPRODUCIBLE report
    can get one. The v1 fixture generator is the reason this exists: it seeds
    fixed dates and stamps a fixed `generated_at`, but the finders were still
    reading the real clock, so the committed fixtures drifted on their own as
    the calendar advanced past a threshold.

    Defaults to the real clock, and deliberately derives each default the way
    the corresponding finder used to derive it (local `date.today()` for the
    backfill window, aware `datetime.now(UTC)` for staleness), so live API
    behavior is byte-for-byte unchanged.
    """
    backfill_end = date.today() if today is None else today
    stale_before = (
        datetime.now(UTC) if today is None else datetime.combine(today, time.min, tzinfo=UTC)
    ) - timedelta(days=7)

    findings: list[DataQualityFindingOut] = []
    findings.extend(_find_missing_cost_basis(session))
    findings.extend(_find_untickered_securities(session))
    findings.extend(_find_securities_without_prices(session))
    findings.extend(_find_anomalous_backfill_days(session, backfill_end))
    findings.extend(_find_stale_items(session, stale_before))
    findings.extend(_find_sparse_forward_snapshots(session))
    findings.extend(_find_overlapping_broker_connections(session))
    findings.extend(_find_missing_policy_benchmarks(session))
    findings.extend(_find_overrides_disagreeing_with_broker(session))
    findings.extend(_find_unconfirmed_option_direction(session))
    findings.extend(_find_contradicted_owner_rulings(session, today or date.today()))

    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f.severity] += 1

    return DataQualityReportOut(
        generated_at=datetime.now(UTC),
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


def _find_anomalous_backfill_days(session: Session, end_date: date) -> list[DataQualityFindingOut]:
    """Days in the reconstructed series with implausibly large daily returns.

    These are flagged inside `_chain_twr` already (TWR is held flat for
    them) but we still want the user to see the dates and the size of the
    discontinuity so they can investigate the underlying transactions.

    `end_date` closes the 730-day lookback; `build_report` supplies it so the
    window can be pinned for reproducible reports.
    """
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


def _find_stale_items(session: Session, threshold: datetime) -> list[DataQualityFindingOut]:
    """Items not refreshed since `threshold` (a week ago, per `build_report`).

    Skips items the user has flagged as data-inactive — those are kept
    around for connection-slot reasons but explicitly aren't expected to
    influence the numbers, so a stale snapshot doesn't matter.
    """
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


def _latest_position_fingerprint(session: Session, account_id: int) -> frozenset[tuple[str, str]]:
    """Non-zero positions on the account's latest snapshot, as (security, quantity)."""
    latest = session.scalar(
        select(func.max(HoldingSnapshot.snapshot_date)).where(
            HoldingSnapshot.account_id == account_id
        )
    )
    if latest is None:
        return frozenset()
    rows = session.execute(
        select(Security.ticker, Security.security_id, HoldingSnapshot.quantity)
        .join(Security, Security.security_id == HoldingSnapshot.security_id)
        .where(HoldingSnapshot.account_id == account_id)
        .where(HoldingSnapshot.snapshot_date == latest)
    ).all()
    return frozenset(
        ((ticker or f"security:{security_id}").strip().upper(), f"{Decimal(quantity):.6f}")
        for ticker, security_id, quantity in rows
        if quantity and not (ticker or "").upper().startswith("CUR:")
    )


def _find_overlapping_broker_connections(
    session: Session,
) -> list[DataQualityFindingOut]:
    """Flag one physical brokerage account reachable through two active items.

    When the same account is connected through two aggregators, it is
    snapshotted twice and every aggregation double-counts it. Two active items
    at the same institution are not evidence of that by themselves: a household
    can hold distinct accounts at one broker. The finding requires a concrete
    match between accounts of different items: the same account mask, or the
    same non-empty set of latest positions and quantities.
    """
    rows = (
        session.execute(select(Item).order_by(Item.institution_name, Item.source)).scalars().all()
    )

    by_institution: dict[str, list[Item]] = defaultdict(list)
    for item in rows:
        key = (item.institution_name or "").strip().lower()
        if not key or not item.is_data_active:
            continue
        by_institution[key].append(item)

    findings: list[DataQualityFindingOut] = []
    for group in by_institution.values():
        if len(group) <= 1:
            continue
        accounts = (
            session.execute(
                select(Account).where(Account.item_id.in_([item.item_id for item in group]))
            )
            .scalars()
            .all()
        )
        fingerprints = {
            account.account_id: _latest_position_fingerprint(session, account.account_id)
            for account in accounts
        }
        matches: list[tuple[Account, Account, str]] = []
        for index, left in enumerate(accounts):
            for right in accounts[index + 1 :]:
                if left.item_id == right.item_id:
                    continue
                if left.mask and right.mask and left.mask.strip() == right.mask.strip():
                    matches.append((left, right, "account_mask"))
                elif fingerprints[left.account_id] and (
                    fingerprints[left.account_id] == fingerprints[right.account_id]
                ):
                    matches.append((left, right, "identical_positions"))
        if not matches:
            continue
        sources = ", ".join(sorted({item.source for item in group}))
        pairs = ", ".join(
            f"#{left.account_id}/#{right.account_id} ({basis})" for left, right, basis in matches
        )
        findings.append(
            DataQualityFindingOut(
                category=OVERLAPPING_BROKER_CONNECTIONS,
                severity=WARNING,
                title=(
                    f"{group[0].institution_name}: the same account is connected via "
                    f"{sources} — data is being double-counted"
                ),
                detail=(
                    f"Matching accounts across active items: {pairs}. The same physical "
                    f"account is snapshotted by both connections, so holdings, "
                    f"transactions, V series, and trade analysis double-count it. Mark one "
                    f"of the duplicate items as data-inactive (PATCH "
                    f"/api/plaid/items/{{id}}/data-active) to keep the connection alive "
                    f"while excluding its data from aggregations."
                ),
                recommended_action=(
                    "Decide which aggregator should be the source of truth "
                    "(SnapTrade typically has more transaction history; "
                    "Plaid is more reliable for current-day holdings). Mark "
                    "the other one is_data_active=False on the Items page."
                ),
                context={
                    "institution": group[0].institution_name or "",
                    "active_item_ids": ",".join(str(item.item_id) for item in group),
                    "matched_account_pairs": pairs,
                    "sources": sources,
                },
            )
        )
    return findings


def _find_contradicted_owner_rulings(session: Session, today: date) -> list[DataQualityFindingOut]:
    """Report an owner ruling that stored evidence now contradicts."""
    contradictions = zero_value_ruling_contradictions(session, date.min, today)
    return [
        DataQualityFindingOut(
            category=OWNER_RULING_CONTRADICTED,
            severity=ERROR,
            title=f"Account #{account_id}: zero-value ruling contradicted ({reason})",
            detail=(
                f"Account #{account_id} is ruled a zero-value flow-through account, but "
                f"the stored evidence shows {reason.replace('_', ' ')}. Returns for any "
                f"window with that evidence fail closed until the ruling is reviewed."
            ),
            recommended_action=(
                "Confirm whether the account now holds value, then update or remove "
                "its entry in services/owner_rulings.py."
            ),
            context={"account_id": str(account_id), "reason": reason},
        )
        for account_id, reason in sorted(contradictions.items())
        # An absent account means the ruling has nothing to apply to.
        if reason != "account_missing"
    ]


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


def _find_unconfirmed_option_direction(session: Session) -> list[DataQualityFindingOut]:
    """Option rows from a feed that omits long/short, graded by trade evidence.

    A positive quantity is only trusted as long when broker fills reconcile to a
    long of the same size. Anything unproven is reported, never guessed.
    """
    accts = active_account_ids(session)
    latest = (
        session.execute(
            select(func.max(HoldingSnapshot.snapshot_date)).where(
                HoldingSnapshot.account_id.in_(accts)
            )
        ).scalar_one_or_none()
        if accts
        else None
    )
    if latest is None:
        return []
    masks = {
        account_id: mask
        for account_id, mask in session.execute(
            select(Account.account_id, Account.mask).where(Account.account_id.in_(accts))
        )
    }
    findings: list[DataQualityFindingOut] = []
    for item in examine_option_rows(session, latest, accts):
        short_held = item.row.quantity < 0
        verdict = item.result.verdict
        ctx = {
            "account_mask": str(masks.get(item.account_id) or ""),
            "contract": item.symbol,
            "verdict": verdict.value,
            "evidence": item.result.detail,
        }
        if short_held and verdict is Verdict.SHORT_CONFIRMED:
            findings.append(
                DataQualityFindingOut(
                    category=OPTION_DIRECTION,
                    severity=INFO,
                    title=f"Short {item.symbol} inferred from broker fills",
                    detail="The feed reported this option without a side. Account fills "
                    "net to the held size as a short, so the sign was corrected.",
                    context=ctx,
                )
            )
        elif short_held and verdict is Verdict.LONG_CONFIRMED:
            findings.append(
                DataQualityFindingOut(
                    category=OPTION_DIRECTION,
                    severity=ERROR,
                    title=f"{item.symbol} is stored short but fills show a long",
                    detail="A stored short sign contradicts the broker fills.",
                    recommended_action="Remove the short-call override or verify with the broker.",
                    context=ctx,
                )
            )
        elif not short_held and verdict is Verdict.SHORT_CONFIRMED:
            findings.append(
                DataQualityFindingOut(
                    category=OPTION_DIRECTION,
                    severity=ERROR,
                    title=f"{item.symbol} is stored long but fills show a short",
                    detail="Re-run the option direction job to apply the proven sign.",
                    recommended_action="python -m portfolio_tracker.jobs.reconcile_option_direction",
                    context=ctx,
                )
            )
        elif not short_held and verdict is not Verdict.LONG_CONFIRMED:
            findings.append(
                DataQualityFindingOut(
                    category=OPTION_DIRECTION,
                    severity=WARNING,
                    title=f"Direction of {item.symbol} is unconfirmed",
                    detail="The feed gives no long/short side and broker fills cannot "
                    f"establish it ({item.result.detail}). The position is shown as long.",
                    recommended_action="Verify the side with the broker. If it is short, "
                    "register it with jobs.register_plaid_short_calls.",
                    context=ctx,
                )
            )
    return findings
