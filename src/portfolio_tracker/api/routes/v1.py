"""The versioned `/api/v1` Portfolio Data Service surface (Slice 1).

Consumer-read resources only — operator mutations (link/relink/sync/corrections)
stay on their existing routes and are documented separately per the provider
PRD §7.1. Everything here is additive: no legacy endpoint changes.

Slice 1 resources:

  * ``GET /api/v1/health``               — process/db/migration/provider health
  * ``GET /api/v1/accounts``             — canonical accounts + detailed Tax treatment
  * ``GET /api/v1/portfolio-snapshot``   — the bulk consumer read model
  * ``GET /api/v1/analytics/positioning``— Positioning cuts + equity fraction

(`GET /api/v1/portfolio/positions` predates this module and lives in
`routes/positions_v1.py`; it is part of the same contract.)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from portfolio_tracker.config import get_settings
from portfolio_tracker.db import get_session
from portfolio_tracker.models import HoldingSnapshot, Item
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.v1_accounts import AccountsV1Result, build_accounts_result
from portfolio_tracker.services.v1_common import V1_SCHEMA_VERSION, is_stale
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
