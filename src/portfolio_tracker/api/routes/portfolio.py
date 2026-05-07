"""Holdings, transactions, and performance endpoints."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated

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
    HoldingByAccountOut,
    HoldingOut,
    InvestmentTransactionOut,
    PerformanceSeries,
)
from portfolio_tracker.services import data_quality, performance
from portfolio_tracker.services.performance import (
    _is_external_cashflow,
    _signed_cashflow,
)

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
    """
    rows = _latest_holding_rows(session)
    if not rows:
        return []
    snapshot_date = rows[0][0].snapshot_date
    overrides = _load_cost_basis_overrides(session)
    return _consolidate_holdings(snapshot_date, rows, overrides)


def _load_cost_basis_overrides(session: Session) -> dict[tuple[int, int], Decimal]:
    """Map (account_id, security_id) → user-supplied total cost basis."""
    rows = session.execute(select(CostBasisOverride)).scalars().all()
    return {(o.account_id, o.security_id): o.total_cost_basis for o in rows}


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
    latest_date = session.execute(
        select(HoldingSnapshot.snapshot_date)
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
        .order_by(Security.ticker, Account.name)
    ).all()
    return [(h, a, s) for h, a, s in rows]


def _consolidate_holdings(
    snapshot_date: date,
    rows: list[tuple[HoldingSnapshot, Account, Security]],
    cost_basis_overrides: dict[tuple[int, int], Decimal],
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
        total_value = Decimal(0)
        total_cost = Decimal(0)
        currency = "USD"
        for h, a, _ in group:
            override = cost_basis_overrides.get((a.account_id, security_id))
            effective_cost = h.cost_basis if h.cost_basis is not None else override
            per_account.append(
                HoldingByAccountOut(
                    account_id=a.account_id,
                    account_name=a.name,
                    quantity=h.quantity,
                    institution_value=h.institution_value,
                    cost_basis=effective_cost,
                )
            )
            currency = h.currency
            if h.institution_value is not None:
                any_value = True
                total_value += h.institution_value
            if effective_cost is not None:
                any_cost = True
                total_cost += effective_cost
        weighted_avg = (
            (total_cost / total_quantity)
            if any_cost and total_quantity > 0
            else None
        )
        unrealized = (
            (total_value - total_cost) if any_value and any_cost else None
        )
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
        .order_by(InvestmentTransaction.date.desc())
        .limit(limit)
    ).all()

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
        )
        for t, a, s in rows
    ]


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
) -> PerformanceSeries:
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = _default_start_date(session, end_date, include_backfill)
    return performance.compute_performance_series(session, start_date, end_date)


# Forward snapshots needed before the default chart drops the 365-day
# backfill fallback. Below this, the chart isn't useful on its own — we
# blend in a year of transaction-walk reconstruction to give context.
_MIN_FORWARD_SNAPSHOTS_FOR_OBSERVED_DEFAULT = 7


def _default_start_date(
    session: Session, end_date: date, include_backfill: bool
) -> date:
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
    earliest_snap = session.execute(
        select(func.min(HoldingSnapshot.snapshot_date))
    ).scalar_one_or_none()
    earliest_tx = session.execute(
        select(func.min(InvestmentTransaction.date))
    ).scalar_one_or_none()

    if include_backfill and earliest_tx is not None:
        candidates = [d for d in (earliest_snap, earliest_tx) if d is not None]
        return max(min(candidates), end_date - timedelta(days=730))

    snap_count = session.execute(
        select(func.count(func.distinct(HoldingSnapshot.snapshot_date)))
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
    if start_date is None:
        # For diagnostics, always go as far back as the data goes (24 months
        # by Plaid's retention) so the user sees every external cashflow.
        earliest_tx = session.execute(
            select(func.min(InvestmentTransaction.date))
        ).scalar_one_or_none()
        start_date = earliest_tx or (end_date - timedelta(days=730))

    rows = session.execute(
        select(
            InvestmentTransaction.type,
            InvestmentTransaction.subtype,
            func.count(InvestmentTransaction.plaid_investment_transaction_id),
            func.sum(InvestmentTransaction.amount),
        )
        .where(InvestmentTransaction.date >= start_date)
        .where(InvestmentTransaction.date <= end_date)
        .group_by(InvestmentTransaction.type, InvestmentTransaction.subtype)
        .order_by(InvestmentTransaction.type, InvestmentTransaction.subtype)
    ).all()

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
        "`transfer/assignment` and other corporate-action subtypes are treated "
        "as INTERNAL.",
    ]

    return CashflowAuditOut(
        start_date=start_date,
        end_date=end_date,
        groups=groups,
        net_external_cashflow_in=net_in,
        notes=notes,
    )


@router.get("/data-quality", response_model=DataQualityReportOut)
def data_quality_report(
    session: Annotated[Session, Depends(get_session)],
) -> DataQualityReportOut:
    """Surface every data-quality issue we know about so you can decide
    which need manual fixes (e.g., entering cost basis for SoFi positions)
    and which are inherent limits (e.g., yfinance can't price options)."""
    return data_quality.build_report(session)
