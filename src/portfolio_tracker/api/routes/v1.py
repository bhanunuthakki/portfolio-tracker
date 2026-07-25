"""The versioned `/api/v1` Portfolio Data Service surface.

Consumer-read resources only — operator mutations (link/relink/sync/corrections)
stay on their existing routes and are documented separately per the provider
PRD §7.1. Everything here is additive: no legacy endpoint changes.

Resources:

  * ``GET /api/v1/health``               — process/db/migration/provider health
  * ``GET /api/v1/accounts``             — canonical accounts + detailed Tax treatment
  * ``GET /api/v1/portfolio-snapshot``   — the bulk consumer read model
  * ``GET /api/v1/transactions``         — cursor-paginated normalized transactions
  * ``GET /api/v1/cash-flows``           — TWR-classified external/internal flows
  * ``GET /api/v1/position-snapshots``   — historical observed holdings
  * ``GET /api/v1/securities``           — security master + Classification
  * ``GET /api/v1/data-quality``         — machine-readable data-quality findings
  * ``GET /api/v1/analytics/positioning``— Positioning cuts + equity fraction
  * ``GET /api/v1/analytics/performance``— Modified-Dietz TWR vs benchmarks
  * ``GET /api/v1/analytics/position-performance`` — per-ticker dollar alpha
  * ``GET /api/v1/analytics/risk``       — beta/volatility + drawdown together
  * ``GET /api/v1/analytics/beta``       — the regression half alone
  * ``GET /api/v1/analytics/drawdown``   — the loss-shaped half alone
  * ``GET /api/v1/analytics/exit-quality`` — sell-side quality facts

(`GET /api/v1/portfolio/positions` predates this module and lives in
`routes/positions_v1.py`; it is part of the same contract. `/api/v1/sync-runs`
is deferred until the run-log schema migration ships — see v1-overview.md.)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from portfolio_tracker.config import get_settings
from portfolio_tracker.db import get_session
from portfolio_tracker.models import HoldingSnapshot, Item
from portfolio_tracker.schemas import DataQualityReportOut
from portfolio_tracker.services import data_quality as data_quality_service
from portfolio_tracker.services import performance as performance_service
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.beta import compute_beta
from portfolio_tracker.services.drawdown import compute_drawdown
from portfolio_tracker.services.exit_quality import compute_exit_quality
from portfolio_tracker.services.position_alpha import compute_position_alpha
from portfolio_tracker.services.v1_accounts import AccountsV1Result, build_accounts_result
from portfolio_tracker.services.v1_analytics import (
    BetaV1Result,
    DrawdownV1Result,
    ExitQualityV1Result,
    PerformanceV1Result,
    PositionPerformanceV1Result,
    RiskV1Result,
    exit_quality_meta,
    performance_meta,
    position_performance_meta,
    risk_meta,
)
from portfolio_tracker.services.v1_common import (
    V1_SCHEMA_VERSION,
    V1Meta,
    build_meta,
    is_stale,
)
from portfolio_tracker.services.v1_history import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CashFlowsV1Result,
    PositionSnapshotsV1Result,
    SecuritiesV1Result,
    TransactionsV1Result,
    build_cash_flows_page,
    build_position_snapshots_page,
    build_securities_result,
    build_transactions_page,
)
from portfolio_tracker.services.v1_snapshot import (
    PortfolioSnapshotV1,
    PositioningV1Result,
    build_portfolio_snapshot,
    build_positioning_v1,
)

router = APIRouter(prefix="/api/v1", tags=["v1"])


class ProviderHealthV1(BaseModel):
    """Per-provider link health — counts and times only, never balances."""

    name: str
    configured: bool
    items_linked: int
    items_active: int
    last_successful_sync_at: datetime | None


class HealthV1(BaseModel):
    """``GET /api/v1/health`` — service, database, migration, provider, and
    contract health. Contains no holdings, balances, or account details."""

    status: str  # "ok" | "degraded"
    schema_version: str
    generated_at: datetime
    database_ok: bool
    # Current alembic revision; None when the version table is absent (e.g. a
    # schema created outside alembic) — reported, not guessed.
    migration_version: str | None
    providers: list[ProviderHealthV1]
    active_account_count: int
    latest_snapshot_date: date | None
    is_stale: bool
    links: dict[str, str]


@router.get("/health", response_model=HealthV1)
def health(session: Annotated[Session, Depends(get_session)]) -> HealthV1:
    settings = get_settings()
    database_ok = True
    migration_version: str | None = None
    try:
        migration_version = session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
    except Exception:
        migration_version = None
    try:
        session.execute(select(1))
    except Exception:
        database_ok = False

    items = session.execute(select(Item)).scalars().all()
    providers: list[ProviderHealthV1] = []
    for name, configured in (
        ("plaid", bool(settings.plaid_client_id)),
        ("snaptrade", settings.snaptrade_client_id is not None),
    ):
        source_items = [i for i in items if i.source == name]
        syncs = [i.last_refreshed_at for i in source_items if i.last_refreshed_at is not None]
        providers.append(
            ProviderHealthV1(
                name=name,
                configured=configured,
                items_linked=len(source_items),
                items_active=sum(1 for i in source_items if i.is_data_active),
                last_successful_sync_at=max(syncs) if syncs else None,
            )
        )

    accts = active_account_ids(session)
    latest_snapshot: date | None = None
    if accts:
        latest_snapshot = session.execute(
            select(func.max(HoldingSnapshot.snapshot_date)).where(
                HoldingSnapshot.account_id.in_(accts)
            )
        ).scalar_one_or_none()
    stale = is_stale(latest_snapshot)
    return HealthV1(
        status="ok" if database_ok and not stale else "degraded",
        schema_version=V1_SCHEMA_VERSION,
        generated_at=datetime.now(UTC),
        database_ok=database_ok,
        migration_version=migration_version,
        providers=providers,
        active_account_count=len(accts),
        latest_snapshot_date=latest_snapshot,
        is_stale=stale,
        links={
            "accounts": "/api/v1/accounts",
            "portfolio_snapshot": "/api/v1/portfolio-snapshot",
            "positions": "/api/v1/portfolio/positions",
            "positioning": "/api/v1/analytics/positioning",
            "openapi": "/openapi.json",
        },
    )


@router.get("/accounts", response_model=AccountsV1Result)
def accounts(session: Annotated[Session, Depends(get_session)]) -> AccountsV1Result:
    """Normalized accounts: canonical identity, inclusion state, detailed Tax
    treatment with evidence, per-account value + observation date, freshness."""
    return build_accounts_result(session)


@router.get("/portfolio-snapshot", response_model=PortfolioSnapshotV1)
def portfolio_snapshot(session: Annotated[Session, Depends(get_session)]) -> PortfolioSnapshotV1:
    """The bulk consumer read model — accounts, positions, five-way tax-bucket
    totals, and the versioned equity fraction in one consistent read."""
    return build_portfolio_snapshot(session)


@router.get("/analytics/positioning", response_model=PositioningV1Result)
def analytics_positioning(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> PositioningV1Result:
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)
    return build_positioning_v1(session, start_date, end_date)


@router.get("/transactions", response_model=TransactionsV1Result)
def transactions_v1(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
) -> TransactionsV1Result:
    """Cursor-paginated normalized transactions with override + effective
    cashflow classification. Follow `next_cursor` until null — no hidden cap.
    Default window: trailing 730 days (provider retention)."""
    return build_transactions_page(
        session, start_date=start_date, end_date=end_date, limit=limit, cursor=cursor
    )


@router.get("/cash-flows", response_model=CashFlowsV1Result)
def cash_flows_v1(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    include_internal: bool = Query(
        default=False,
        description="Include internal (zeroed) cashflow events alongside external ones.",
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
) -> CashFlowsV1Result:
    """Deterministically classified cash flows, using the exact TWR-pipeline
    precedence (override → name hint → subtype heuristic)."""
    return build_cash_flows_page(
        session,
        start_date=start_date,
        end_date=end_date,
        include_internal=include_internal,
        limit=limit,
        cursor=cursor,
    )


@router.get("/position-snapshots", response_model=PositionSnapshotsV1Result)
def position_snapshots_v1(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
) -> PositionSnapshotsV1Result:
    """Historical observed holdings rows with explicit date bounds and origin
    markers (broker vs manual gap-fill). Default window: trailing 90 days."""
    return build_position_snapshots_page(
        session, start_date=start_date, end_date=end_date, limit=limit, cursor=cursor
    )


@router.get("/securities", response_model=SecuritiesV1Result)
def securities_v1(session: Annotated[Session, Depends(get_session)]) -> SecuritiesV1Result:
    """The security master: identifiers, cash-equivalent status, asset type,
    and sector/region Classification with source."""
    return build_securities_result(session)


class DataQualityV1Result(BaseModel):
    """``GET /api/v1/data-quality`` — the existing machine-readable findings
    report under the shared envelope."""

    meta: V1Meta
    report: DataQualityReportOut


@router.get("/data-quality", response_model=DataQualityV1Result)
def data_quality_v1(session: Annotated[Session, Depends(get_session)]) -> DataQualityV1Result:
    accounts_meta = build_accounts_result(session).meta
    report = data_quality_service.build_report(session)
    meta = build_meta(
        as_of=accounts_meta.as_of,
        source_providers=accounts_meta.source_providers,
        coverage=accounts_meta.account_coverage,
        last_successful_sync_at=accounts_meta.last_successful_sync_at,
        warnings=[],
        links={"health": "/api/v1/health", "accounts": "/api/v1/accounts"},
        methodology="data_quality.findings",
        methodology_version="1",
    )
    return DataQualityV1Result(meta=meta, report=report)


@router.get("/analytics/performance", response_model=PerformanceV1Result)
def analytics_performance(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    include_backfill: bool = Query(default=False),
    reserve_amount: float = Query(default=0.0, ge=0),
    exclude_index_etfs: bool = Query(default=False),
) -> PerformanceV1Result:
    """Modified-Dietz TWR + benchmark counterfactuals (same calculation as the
    legacy `/api/portfolio/performance`, now enveloped and versioned)."""
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)
    series = performance_service.compute_performance_series(
        session, start_date, end_date, Decimal(str(reserve_amount)), exclude_index_etfs
    )
    return PerformanceV1Result(meta=performance_meta(session), series=series)


@router.get("/analytics/position-performance", response_model=PositionPerformanceV1Result)
def analytics_position_performance(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    exclude_broad_index: bool = Query(default=False),
) -> PositionPerformanceV1Result:
    """Per-ticker dollar alpha vs dollar-matched SPY/QQQ/policy counterfactuals."""
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)
    result = compute_position_alpha(
        session, start_date, end_date, exclude_broad_index=exclude_broad_index
    )
    return PositionPerformanceV1Result(meta=position_performance_meta(session), result=result)


@router.get("/analytics/risk", response_model=RiskV1Result)
def analytics_risk(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    benchmark: str = Query(default="SPY"),
    risk_free_annual: float | None = Query(default=None),
    exclude_index_etfs: bool = Query(default=False),
    reserve_amount: float = Query(default=0.0, ge=0),
) -> RiskV1Result:
    """Beta/alpha/R², Sharpe/Sortino, tracking error, volatility, plus max
    drawdown and recovery — one risk read for consumers."""
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)
    beta = compute_beta(
        session,
        start_date,
        end_date,
        benchmark,
        risk_free_annual,
        exclude_index_etfs,
        Decimal(str(reserve_amount)),
    )
    drawdown = compute_drawdown(
        session, start_date, end_date, Decimal(str(reserve_amount)), exclude_index_etfs
    )
    return RiskV1Result(meta=risk_meta(session), beta=beta, drawdown=drawdown)


@router.get("/analytics/beta", response_model=BetaV1Result)
def analytics_beta(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    benchmark: str = Query(default="SPY"),
    risk_free_annual: float | None = Query(default=None),
    exclude_index_etfs: bool = Query(default=False),
    reserve_amount: float = Query(default=0.0, ge=0),
) -> BetaV1Result:
    """Beta/alpha/R², Sharpe/Sortino, tracking error, volatility only.

    The regression half of `/analytics/risk`, split out so a consumer that
    only needs volatility does not also pay for the drawdown walk (and vice
    versa — see `/analytics/drawdown`). `/analytics/risk` still returns both
    together for consumers that want one call.
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)
    beta = compute_beta(
        session,
        start_date,
        end_date,
        benchmark,
        risk_free_annual,
        exclude_index_etfs,
        Decimal(str(reserve_amount)),
    )
    return BetaV1Result(meta=risk_meta(session), beta=beta)


@router.get("/analytics/drawdown", response_model=DrawdownV1Result)
def analytics_drawdown(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    exclude_index_etfs: bool = Query(default=False),
    reserve_amount: float = Query(default=0.0, ge=0),
) -> DrawdownV1Result:
    """Max drawdown, underwater curve, time-to-recovery, and Calmar only.

    The cheap half of `/analytics/risk`: this walk does not run the beta
    regression, so a consumer needing only loss-shaped risk gets it without
    the regression's cost.
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)
    drawdown = compute_drawdown(
        session, start_date, end_date, Decimal(str(reserve_amount)), exclude_index_etfs
    )
    return DrawdownV1Result(meta=risk_meta(session), drawdown=drawdown)


@router.get("/analytics/exit-quality", response_model=ExitQualityV1Result)
def analytics_exit_quality(
    session: Annotated[Session, Depends(get_session)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> ExitQualityV1Result:
    """Sell-side quality facts: regret vs holding and exit alpha vs SPY."""
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)
    result = compute_exit_quality(session, start_date, end_date)
    return ExitQualityV1Result(meta=exit_quality_meta(session), result=result)
