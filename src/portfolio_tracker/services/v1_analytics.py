"""Envelope wrappers for the deterministic analytics under `/api/v1/analytics/*`.

The calculations themselves are unchanged — Portfolio Tracker's existing
services remain authoritative (PRD §9.2). These wrappers add the shared
envelope so consumers get freshness, coverage, and versioned methodology
metadata alongside every analytics payload.

The envelope ``as_of`` is the underlying holdings observation date (from the
accounts read model), NOT the query window end: a calculation run today over a
week-old book must read as stale (PRD §9.3 — "a current calculation over stale
inputs is still stale").
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel
from sqlalchemy.orm import Session

from portfolio_tracker.schemas import PerformanceSeries
from portfolio_tracker.services.active_items import valued_account_ids
from portfolio_tracker.services.beta import BetaResult
from portfolio_tracker.services.drawdown import DrawdownResult
from portfolio_tracker.services.exit_quality import ExitQualityResult
from portfolio_tracker.services.position_alpha import PositionAlphaResult
from portfolio_tracker.services.v1_accounts import build_accounts_result
from portfolio_tracker.services.v1_common import (
    W_CALCULATION_UNAVAILABLE,
    V1AccountCoverage,
    V1Meta,
    V1Warning,
    build_meta,
)


def _analytics_meta(
    session: Session,
    *,
    methodology: str,
    links: dict[str, str],
    methodology_version: str = "1",
    included_account_ids: frozenset[int] | None = None,
    as_of_override: date | None = None,
    warnings: list[V1Warning] | None = None,
    today: date | None = None,
    generated_at: datetime | None = None,
) -> V1Meta:
    accounts_result = build_accounts_result(session, today=today, generated_at=generated_at)
    accounts_meta = accounts_result.meta
    coverage = accounts_meta.account_coverage
    source_providers = accounts_meta.source_providers
    last_successful_sync_at = accounts_meta.last_successful_sync_at
    if included_account_ids is not None:
        previously_included = set(coverage.included_account_ids)
        coverage = V1AccountCoverage(
            included_account_ids=sorted(included_account_ids),
            excluded_account_ids=sorted(
                set(coverage.excluded_account_ids) | (previously_included - included_account_ids)
            ),
            lagging_account_ids=sorted(set(coverage.lagging_account_ids) & included_account_ids),
        )
        included_accounts = [
            account
            for account in accounts_result.accounts
            if account.account_id in included_account_ids
        ]
        source_providers = sorted({account.provider for account in included_accounts})
        sync_times = [
            account.last_successful_sync_at
            for account in included_accounts
            if account.last_successful_sync_at is not None
        ]
        last_successful_sync_at = max(sync_times) if sync_times else None
    return build_meta(
        as_of=as_of_override or accounts_meta.as_of,
        source_providers=source_providers,
        coverage=coverage,
        last_successful_sync_at=last_successful_sync_at,
        warnings=warnings or [],
        links=links,
        methodology=methodology,
        methodology_version=methodology_version,
        today=today,
        generated_at=generated_at,
    )


class PerformanceV1Result(BaseModel):
    """Modified-Dietz TWR vs cashflow-matched benchmark counterfactuals."""

    meta: V1Meta
    series: PerformanceSeries


class PositionPerformanceV1Result(BaseModel):
    """Fail-closed invested-position price/trade comparison."""

    meta: V1Meta
    result: PositionAlphaResult


class RiskV1Result(BaseModel):
    """Correlation/beta/volatility (regression + risk-adjusted stats) and
    drawdown/recovery in one read."""

    meta: V1Meta
    beta: BetaResult
    drawdown: DrawdownResult


class BetaV1Result(BaseModel):
    """The regression half of `RiskV1Result`, for consumers that only need
    volatility/beta and shouldn't pay for the drawdown walk."""

    meta: V1Meta
    beta: BetaResult


class DrawdownV1Result(BaseModel):
    """The loss-shaped half of `RiskV1Result`, for consumers that only need
    drawdown/recovery and shouldn't pay for the beta regression."""

    meta: V1Meta
    drawdown: DrawdownResult


class ExitQualityV1Result(BaseModel):
    """Sell-side quality facts: regret vs holding and exit alpha vs SPY."""

    meta: V1Meta
    result: ExitQualityResult


def performance_meta(
    session: Session,
    *,
    series: PerformanceSeries | None = None,
    today: date | None = None,
    generated_at: datetime | None = None,
) -> V1Meta:
    warnings = (
        [
            V1Warning(
                code=W_CALCULATION_UNAVAILABLE,
                message=(
                    "Whole-portfolio performance is unavailable: "
                    + ", ".join(series.calculation_reason_codes)
                ),
                scope="performance",
            )
        ]
        if series is not None and series.calculation_status == "unavailable"
        else []
    )
    return _analytics_meta(
        session,
        methodology="performance.modified_dietz",
        methodology_version="2",
        links={
            "position_performance": "/api/v1/analytics/position-performance",
            "risk": "/api/v1/analytics/risk",
            "cash_flows": "/api/v1/cash-flows",
        },
        included_account_ids=(
            frozenset(series.valuation_account_ids)
            if series is not None
            else valued_account_ids(session)
        ),
        as_of_override=(
            series.end_date
            if series is not None
            and series.ending_value_provenance
            in {"observed_complete_snapshot", "observed_account_valuation"}
            else None
        ),
        warnings=warnings,
        today=today,
        generated_at=generated_at,
    )


def position_performance_meta(
    session: Session, *, today: date | None = None, generated_at: datetime | None = None
) -> V1Meta:
    return _analytics_meta(
        session,
        methodology="position_alpha.split_normalized_price_trade_modified_dietz",
        methodology_version="3",
        links={"performance": "/api/v1/analytics/performance"},
        today=today,
        generated_at=generated_at,
    )


def risk_meta(
    session: Session, *, today: date | None = None, generated_at: datetime | None = None
) -> V1Meta:
    return _analytics_meta(
        session,
        methodology="risk.beta_drawdown",
        methodology_version="2",
        links={
            "performance": "/api/v1/analytics/performance",
            "positioning": "/api/v1/analytics/positioning",
        },
        today=today,
        generated_at=generated_at,
    )


def exit_quality_meta(
    session: Session, *, today: date | None = None, generated_at: datetime | None = None
) -> V1Meta:
    return _analytics_meta(
        session,
        methodology="exit_quality.repricing",
        links={"transactions": "/api/v1/transactions"},
        today=today,
        generated_at=generated_at,
    )
