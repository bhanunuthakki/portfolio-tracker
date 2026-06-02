"""Holdings, transactions, and performance endpoints."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_tracker.db import get_session
from portfolio_tracker.models import (
    Account,
    CostBasisOverride,
    HoldingSnapshot,
    InvestmentTransaction,
    Security,
)
from portfolio_tracker.schemas import (
    CashflowAuditOut,
    CashflowGroupOut,
    ConsolidatedHoldingOut,
    DataQualityReportOut,
    EarningsEnrichment,
    HoldingByAccountOut,
    HoldingOut,
    InvestmentTransactionOut,
    PerformanceSeries,
    PositioningOut,
)
from portfolio_tracker.services import beta as beta_service
from portfolio_tracker.services import data_quality, performance
from portfolio_tracker.services import earnings_summary as earnings_summary_svc
from portfolio_tracker.services import position_alpha as position_alpha_service
from portfolio_tracker.services import positioning as positioning_service
from portfolio_tracker.services import trade_analysis as trade_analysis_service
from portfolio_tracker.services import trade_timeline as trade_timeline_service
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.beta import BetaResult
from portfolio_tracker.services.performance import (
    _is_external_cashflow,  # pyright: ignore[reportPrivateUsage]
    _load_transaction_overrides,  # pyright: ignore[reportPrivateUsage]
    _signed_cashflow,  # pyright: ignore[reportPrivateUsage]
    effective_classification,
)
from portfolio_tracker.services.trade_analysis import TradeAnalysisResult
from portfolio_tracker.services.trade_timeline import TradeTimelineResult

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.get("/holdings", response_model=list[ConsolidatedHoldingOut])
def latest_holdings_consolidated(
    session: Annotated[Session, Depends(get_session)],
) -> list[ConsolidatedHoldingOut]:
    """Return the latest holdings rolled up by security across all accounts.

    Each consolidated row carries a per-account drill-down so the UI can
    expand it. The TWR / performance pipeline continues to operate on the
    raw per-account snapshot rows — this rollup is presentation only.

    Cost basis falls back to `cost_basis_overrides` when the snapshot's
    value is NULL — see `services/overrides.py` for the merge logic.

    Each row is also enriched with cross-project data from
    `earnings-summary` (next earnings date, thesis status, brief link)
    when that companion project's DB is reachable.
    """
    rows = _latest_holding_rows(session)
    if not rows:
        return []
    snapshot_date = rows[0][0].snapshot_date
    overrides = _load_cost_basis_overrides(session)
    consolidated = _consolidate_holdings(snapshot_date, rows, overrides)
    # Enrich with earnings-summary data (no-op when unavailable)
    tickers = [c.ticker for c in consolidated if c.ticker]
    enrichment = earnings_summary_svc.summary_by_ticker(tickers)
    for c in consolidated:
        if not c.ticker:
            continue
        es = enrichment.get(c.ticker.upper())
        if es is None or (not es.tracked and not es.has_brief and es.next_earnings_date is None):
            continue
        c.earnings = EarningsEnrichment(
            tracked=es.tracked,
            list_type=es.list_type,
            next_earnings_date=es.next_earnings_date,
            thesis_status=es.thesis_status,
            thesis_summary=es.thesis_summary,
            has_brief=es.has_brief,
            latest_brief_iso_date=es.latest_brief_iso_date,
        )
    return consolidated


def _load_cost_basis_overrides(
    session: Session,
) -> dict[tuple[int, int], tuple[Decimal, str]]:
    """Map (account_id, security_id) → (total_cost_basis, source).

    `source` is one of `manual` / `inferred_acats` / `inferred_1099` per
    the row's origin marker (see migration 0012). Consumers use it to
    decide whether to badge the row in the UI.
    """
    rows = session.execute(select(CostBasisOverride)).scalars().all()
    return {(o.account_id, o.security_id): (o.total_cost_basis, o.source) for o in rows}


@router.get("/holdings/by-account", response_model=list[HoldingOut])
def latest_holdings_by_account(
    session: Annotated[Session, Depends(get_session)],
) -> list[HoldingOut]:
    """Per-account snapshot — useful for debugging and for accuracy-sensitive
    consumers. Same data the consolidated endpoint rolls up."""
    rows = _latest_holding_rows(session)
    return [
        HoldingOut(
            snapshot_date=h.snapshot_date,
            account_id=a.account_id,
            account_name=a.name,
            security_id=s.security_id,
            ticker=s.ticker,
            name=s.name,
            quantity=h.quantity,
            institution_price=h.institution_price,
            institution_value=h.institution_value,
            cost_basis=h.cost_basis,
            currency=h.currency,
        )
        for h, a, s in rows
    ]


def _latest_holding_rows(
    session: Session,
) -> list[tuple[HoldingSnapshot, Account, Security]]:
    accts = active_account_ids(session)
    if not accts:
        return []
    latest_date = session.execute(
        select(HoldingSnapshot.snapshot_date)
        .where(HoldingSnapshot.account_id.in_(accts))
        .order_by(HoldingSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_date is None:
        return []
    rows = session.execute(
        select(HoldingSnapshot, Account, Security)
        .join(Account, Account.account_id == HoldingSnapshot.account_id)
        .join(Security, Security.security_id == HoldingSnapshot.security_id)
        .where(HoldingSnapshot.snapshot_date == latest_date)
        .where(HoldingSnapshot.account_id.in_(accts))
        .order_by(Security.ticker, Account.name)
    ).all()
    return [(h, a, s) for h, a, s in rows]


# Below this dollar size we don't bother flagging — a $50 position with a
# $1 reported cost basis isn't worth surfacing as a data-quality issue.
_UNRELIABLE_MIN_VALUE = Decimal("1000")
# Reported cost basis must be at least this fraction of market value to be
# considered plausible. Tuned against the observed pattern of broker rows
# returning $15–$100 cost for positions worth thousands.
_UNRELIABLE_COST_RATIO = Decimal("0.05")


def _is_cost_basis_unreliable(
    cost_basis: Decimal | None, institution_value: Decimal | None
) -> bool:
    """Heuristic: broker-reported cost basis is implausibly low vs market value.

    Catches three broken patterns Plaid/SnapTrade emit for ACATS-in or
    outside-retention positions:
      * Cost basis NULL on a non-trivial position (broker omitted it entirely)
      * Cost basis $0 or negative
      * Cost basis < 5% of market value

    Only fires when the position is worth at least $1k so trivial junk doesn't
    add noise.
    """
    if institution_value is None or institution_value < _UNRELIABLE_MIN_VALUE:
        return False
    if cost_basis is None:
        return True
    if cost_basis <= 0:
        return True
    return (cost_basis / institution_value) < _UNRELIABLE_COST_RATIO


def _consolidate_holdings(
    snapshot_date: date,
    rows: list[tuple[HoldingSnapshot, Account, Security]],
    cost_basis_overrides: dict[tuple[int, int], tuple[Decimal, str]],
) -> list[ConsolidatedHoldingOut]:
    """Group per-account holdings by security_id and compute weighted cost.

    Weighted average cost per share = sum(cost_basis) / sum(quantity), where
    cost_basis is Plaid's TOTAL acquisition cost (price × shares + fees) for
    that holding. When the snapshot's cost_basis is NULL, we fall back to
    `cost_basis_overrides[(account_id, security_id)]`. Weighted avg is
    skipped only when ALL contributing accounts are missing both broker
    and override values, OR when total_quantity is 0.
    """
    grouped: dict[int, list[tuple[HoldingSnapshot, Account, Security]]] = defaultdict(list)
    for h, a, s in rows:
        grouped[s.security_id].append((h, a, s))

    out: list[ConsolidatedHoldingOut] = []
    for security_id, group in grouped.items():
        first_security = group[0][2]
        total_quantity = sum((h.quantity for h, _, _ in group), Decimal(0))
        per_account: list[HoldingByAccountOut] = []
        any_value: bool = False
        any_cost: bool = False
        any_unreliable: bool = False
        total_value = Decimal(0)
        total_cost = Decimal(0)
        currency = "USD"
        for h, a, _ in group:
            override_entry = cost_basis_overrides.get((a.account_id, security_id))
            override_amount = override_entry[0] if override_entry else None
            override_source = override_entry[1] if override_entry else None
            # Override wins when present (including over $0 / wrong broker values).
            # An ACATS-in position often gets `cost_basis=0` from the receiving
            # broker which inflates P&L; the manual / inferred override is the
            # right number.
            if override_amount is not None:
                effective_cost = override_amount
                cost_basis_source: str | None = override_source
                row_unreliable = False  # override is the truth, never unreliable
            else:
                effective_cost = h.cost_basis
                cost_basis_source = None
                row_unreliable = _is_cost_basis_unreliable(effective_cost, h.institution_value)
            if row_unreliable:
                any_unreliable = True
            per_account.append(
                HoldingByAccountOut(
                    account_id=a.account_id,
                    account_name=a.name,
                    quantity=h.quantity,
                    institution_value=h.institution_value,
                    cost_basis=effective_cost,
                    cost_basis_source=cost_basis_source,
                    cost_basis_unreliable=row_unreliable,
                )
            )
            currency = h.currency
            if h.institution_value is not None:
                any_value = True
                total_value += h.institution_value
            if effective_cost is not None:
                any_cost = True
                total_cost += effective_cost
        weighted_avg = (total_cost / total_quantity) if any_cost and total_quantity > 0 else None
        unrealized = (total_value - total_cost) if any_value and any_cost else None
        out.append(
            ConsolidatedHoldingOut(
                snapshot_date=snapshot_date,
                security_id=security_id,
                ticker=first_security.ticker,
                name=first_security.name,
                total_quantity=total_quantity,
                total_value=total_value if any_value else None,
                total_cost_basis=total_cost if any_cost else None,
                weighted_avg_cost_per_share=weighted_avg,
                unrealized_pnl=unrealized,
                accounts=per_account,
                currency=currency,
                has_unreliable_cost_basis=any_unreliable,
            )
        )
    out.sort(key=lambda h: -(float(h.total_value) if h.total_value is not None else 0))
    return out


@router.get("/transactions", response_model=list[InvestmentTransactionOut])
def transactions(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
) -> list[InvestmentTransactionOut]:
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=730)  # 24 months default

    accts = active_account_ids(session)
    if not accts:
        return []
    rows = session.execute(
        select(InvestmentTransaction, Account, Security)
        .join(Account, Account.account_id == InvestmentTransaction.account_id)
        .join(
            Security,
            Security.security_id == InvestmentTransaction.security_id,
            isouter=True,
        )
        .where(InvestmentTransaction.date >= start_date)
        .where(InvestmentTransaction.date <= end_date)
        .where(InvestmentTransaction.account_id.in_(accts))
        .order_by(InvestmentTransaction.date.desc())
        .limit(limit)
    ).all()

    overrides = _load_transaction_overrides(session)
    return [
        InvestmentTransactionOut(
            plaid_investment_transaction_id=t.plaid_investment_transaction_id,
            account_id=a.account_id,
            account_name=a.name,
            security_id=s.security_id if s is not None else None,
            ticker=s.ticker if s is not None else None,
            date=t.date,
            name=t.name,
            quantity=t.quantity,
            amount=t.amount,
            price=t.price,
            fees=t.fees,
            type=t.type,
            subtype=t.subtype,
            currency=t.currency,
            override_classification=overrides.get(t.plaid_investment_transaction_id),
            effective_classification=effective_classification(
                t.type,
                t.subtype,
                overrides.get(t.plaid_investment_transaction_id),
                amount=Decimal(t.amount) if t.amount is not None else None,
                name=t.name,
            ),
        )
        for t, a, s in rows
    ]


@router.get("/position-alpha", response_model=position_alpha_service.PositionAlphaResult)
def position_alpha(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    exclude_broad_index: bool = Query(
        default=False,
        description=(
            "If true, drop VTI/VOO/SPY/IVV/RSP rows from the per-ticker view "
            "and the aggregate. Use to isolate active-pick alpha from passive-"
            "index allocation."
        ),
    ),
) -> position_alpha_service.PositionAlphaResult:
    """Per-ticker dollar alpha vs SPY/QQQ/POLICY for the chosen window.

    Methodology — start of window is a fresh balance sheet. Each ticker's
    starting capital is `qty_at_start × price_at_start`. Each benchmark
    counterfactual invests that starting capital in the benchmark at
    window_start price, then dollar-matches every in-window buy/sell.
    Alpha is the difference in ending values.

    SGOV / cash-equivalents are always skipped (no meaningful 'alpha' on
    cash). Set `exclude_broad_index=true` to also drop broad-market ETFs.

    Returns both the per-ticker breakdown AND a daily time series across
    SPY, QQQ, and POLICY counterfactuals.
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)
    return position_alpha_service.compute_position_alpha(
        session,
        start_date,
        end_date,
        exclude_broad_index=exclude_broad_index,
    )


@router.get("/performance", response_model=PerformanceSeries)
def performance_series(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    include_backfill: bool = Query(
        default=False,
        description=(
            "If true, extend the series backward through transaction-walk "
            "reconstruction (up to ~24 months). Backfilled values are MODELED, "
            "not observed — they can drift on incomplete transactions or "
            "unrecorded transfers, distorting TWR. Use forward snapshots when "
            "accuracy matters."
        ),
    ),
    reserve_amount: float = Query(
        default=0.0,
        ge=0,
        description=(
            "Dollar amount to subtract from V_start, every daily V, AND each "
            "synthetic-benchmark starting capital before computing returns. "
            "Use to carve out an emergency cash reserve so the chart shows "
            "'investable portion only' instead of the full book."
        ),
    ),
    exclude_index_etfs: bool = Query(
        default=False,
        description=(
            "If true, strip broad-market US-equity ETFs (VTI/VOO/SPY/IVV/RSP) "
            "from V on every date and add their buy/sell flows as internal "
            "cashflows. The chart then shows the active stock-picking portion "
            "of the portfolio vs benchmarks of equivalent size — useful for "
            "isolating stock-selection alpha from passive-index allocation."
        ),
    ),
) -> PerformanceSeries:
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = _default_start_date(session, end_date, include_backfill)
    return performance.compute_performance_series(
        session,
        start_date,
        end_date,
        Decimal(str(reserve_amount)),
        exclude_index_etfs,
    )


# Forward snapshots needed before the default chart drops the 365-day
# backfill fallback. Below this, the chart isn't useful on its own — we
# blend in a year of transaction-walk reconstruction to give context.
_MIN_FORWARD_SNAPSHOTS_FOR_OBSERVED_DEFAULT = 7


def _default_start_date(session: Session, end_date: date, include_backfill: bool) -> date:
    """Default chart window — chosen to balance "useful on day one" against
    "doesn't silently show modeled values as observed".

    Behavior:
      * `include_backfill=True` → anchor on the earliest transaction date,
        capped at ~2 years (Plaid investment-tx retention).
      * Else, if you have ≥ N distinct forward snapshot dates → start at
        the earliest snapshot. Extends naturally as the snapshotter runs.
      * Else (fresh install or just a few snapshots) → fall back to 365-day
        transaction-walk backfill so the chart isn't a single dot. The UI
        shows the backfill caveat in the chart caption either way.
    """
    accts = active_account_ids(session)
    if not accts:
        return end_date - timedelta(days=365)
    earliest_snap = session.execute(
        select(func.min(HoldingSnapshot.snapshot_date)).where(HoldingSnapshot.account_id.in_(accts))
    ).scalar_one_or_none()
    earliest_tx = session.execute(
        select(func.min(InvestmentTransaction.date)).where(
            InvestmentTransaction.account_id.in_(accts)
        )
    ).scalar_one_or_none()

    if include_backfill and earliest_tx is not None:
        candidates = [d for d in (earliest_snap, earliest_tx) if d is not None]
        return max(min(candidates), end_date - timedelta(days=730))

    snap_count = session.execute(
        select(func.count(func.distinct(HoldingSnapshot.snapshot_date))).where(
            HoldingSnapshot.account_id.in_(accts)
        )
    ).scalar_one()
    if snap_count >= _MIN_FORWARD_SNAPSHOTS_FOR_OBSERVED_DEFAULT and earliest_snap is not None:
        return earliest_snap

    return end_date - timedelta(days=365)


@router.get("/cashflow-audit", response_model=CashflowAuditOut)
def cashflow_audit(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> CashflowAuditOut:
    """Diagnostic: shows how each (type, subtype) of transaction is classified
    for TWR. Net external cashflow is the dollar amount netted out of the
    portfolio return computation.

    Use this to verify nothing internal (dividends, fees, trades) is being
    counted as cashflow, and that real deposits/withdrawals/transfers ARE.
    """
    if end_date is None:
        end_date = date.today()
    accts = active_account_ids(session)
    if start_date is None:
        # For diagnostics, always go as far back as the data goes (24 months
        # by Plaid's retention) so the user sees every external cashflow.
        earliest_tx = (
            session.execute(
                select(func.min(InvestmentTransaction.date)).where(
                    InvestmentTransaction.account_id.in_(accts)
                )
            ).scalar_one_or_none()
            if accts
            else None
        )
        start_date = earliest_tx or (end_date - timedelta(days=730))

    rows: list[Any] = []
    if accts:
        rows = list(
            session.execute(
                select(
                    InvestmentTransaction.type,
                    InvestmentTransaction.subtype,
                    func.count(InvestmentTransaction.plaid_investment_transaction_id),
                    func.sum(InvestmentTransaction.amount),
                )
                .where(InvestmentTransaction.date >= start_date)
                .where(InvestmentTransaction.date <= end_date)
                .where(InvestmentTransaction.account_id.in_(accts))
                .group_by(InvestmentTransaction.type, InvestmentTransaction.subtype)
                .order_by(InvestmentTransaction.type, InvestmentTransaction.subtype)
            ).all()
        )

    groups: list[CashflowGroupOut] = []
    net_in = Decimal(0)
    for tx_type, tx_subtype, count, total_amount in rows:
        is_external = _is_external_cashflow(tx_type, tx_subtype)
        if is_external:
            # Use the same signed-cashflow logic as TWR so the audit total
            # exactly matches what the chart sees. `_signed_cashflow` handles
            # the sign-convention inconsistencies across brokers per subtype.
            net_in += _signed_cashflow(tx_type, tx_subtype, Decimal(total_amount or 0))
        groups.append(
            CashflowGroupOut(
                type=str(tx_type),
                subtype=tx_subtype,
                count=int(count),
                sum_amount=Decimal(total_amount or 0),
                classified_as_external_cashflow=is_external,
            )
        )

    notes = [
        "Direction by subtype: `contribution`/`deposit`/`rollover`/`wire`/`ach` "
        "are inflows; `withdrawal` is outflow; `transfer` follows Plaid's signed "
        "amount because brokers use it for both directions.",
        "Trades, dividends, interest, and fees are intentionally excluded — they "
        "affect value but not basis.",
        "`transfer/assignment` and other corporate-action subtypes are treated as INTERNAL.",
    ]

    return CashflowAuditOut(
        start_date=start_date,
        end_date=end_date,
        groups=groups,
        net_external_cashflow_in=net_in,
        notes=notes,
    )


@router.get("/beta", response_model=BetaResult)
def beta_endpoint(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    benchmark: str = Query(
        default="SPY",
        description="Benchmark symbol (SPY/QQQ/etc.) or 'POLICY' for the user's policy mix.",
    ),
    risk_free_annual: float = Query(
        default=0.04,
        description="Annualized risk-free rate used by Sharpe and Sortino (0.04 = 4%/year).",
    ),
    exclude_index_etfs: bool = Query(
        default=False,
        description=(
            "If true, strip broad-market US-equity ETFs (VTI/VOO/SPY/IVV/RSP) "
            "from V before computing returns. Compare σ of the active portion "
            "vs benchmark σ to see whether your stock-picking gave a "
            "diversification / derisking benefit."
        ),
    ),
    reserve_amount: float = Query(
        default=0.0,
        ge=0,
        description=(
            "Dollar amount of cash reserves to subtract from V before "
            "computing daily returns. Mirrors the same param on /performance."
        ),
    ),
) -> BetaResult:
    """Risk + risk-adjusted-return metrics vs a benchmark.

    Returns beta + alpha + R² (regression-based), Sharpe / Sortino
    (absolute), and Information Ratio + tracking error (vs benchmark).
    All annualized. Defaults to a 1-year lookback. Daily returns are
    paired by date; days with reconstructed swings >30% are dropped.
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)
    return beta_service.compute_beta(
        session,
        start_date,
        end_date,
        benchmark,
        risk_free_annual,
        exclude_index_etfs,
        Decimal(str(reserve_amount)),
    )


@router.get("/positioning", response_model=PositioningOut)
def positioning_endpoint(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> PositioningOut:
    """Portfolio positioning — how the latest book splits across asset type,
    sector, region, and account tax-treatment, plus concentration stats and a
    per-ticker correlation/beta table vs SPY, QQQ, and the policy mix.

    Breakdowns are value-weighted over current market value. The correlation
    window defaults to the trailing year (the beta default); sector/region
    come from `security_classifications` (yfinance-enriched, user-overridable).
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)
    return positioning_service.compute_positioning(session, start_date, end_date)


@router.get("/data-quality", response_model=DataQualityReportOut)
def data_quality_report(
    session: Annotated[Session, Depends(get_session)],
) -> DataQualityReportOut:
    """Surface every data-quality issue we know about so you can decide
    which need manual fixes (e.g., entering cost basis for SoFi positions)
    and which are inherent limits (e.g., yfinance can't price options)."""
    return data_quality.build_report(session)


@router.get("/trade-analysis", response_model=TradeAnalysisResult)
def trade_analysis_endpoint(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> TradeAnalysisResult:
    """Per-ticker buy/sell summary + trading-activity stats.

    Surfaced on the dashboard's Trade Analysis card so the user can see
    which names made or lost money and whether trading frequency is
    eating into returns. P&L = today's mark + cumulative sells − cumulative
    buys, which works for closed and open positions alike. Defaults to
    the entire transaction history when dates aren't pinned.
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        # Default to ~24 months — matches Plaid's transaction-retention
        # window so we don't pretend to analyze data we don't have.
        start_date = end_date - timedelta(days=730)
    return trade_analysis_service.analyze_trades(session, start_date, end_date)


@router.get("/trade-timeline", response_model=TradeTimelineResult)
def trade_timeline_endpoint(
    session: Annotated[Session, Depends(get_session)],
    year: int | None = Query(
        default=None,
        description="Tax year filter (applies to 1099 closed-lot rows only). Omit for all years.",
    ),
    include_open: bool = Query(
        default=True,
        description="Include currently-held positions with unrealized P&L.",
    ),
) -> TradeTimelineResult:
    """Chronological trade timeline with SPY counterfactual.

    Combines 1099 realized lots (authoritative historical) with current open
    positions (broker snapshots) and computes per-row alpha vs SPY for the
    same holding window. Designed for a 'which trades earned/cost me alpha'
    timeline on the dashboard.
    """
    return trade_timeline_service.build_timeline(session, year=year, include_open=include_open)
