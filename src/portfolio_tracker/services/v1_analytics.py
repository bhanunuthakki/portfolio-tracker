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
from portfolio_tracker.services.beta import BetaResult
from portfolio_tracker.services.drawdown import DrawdownResult
from portfolio_tracker.services.exit_quality import ExitQualityResult
from portfolio_tracker.services.position_alpha import PositionAlphaResult
from portfolio_tracker.services.v1_accounts import build_accounts_result
from portfolio_tracker.services.v1_common import V1Meta, build_meta


def _analytics_meta(
    session: Session,
    *,
    methodology: str,
    links: dict[str, str],
    today: date | None = None,
    generated_at: datetime | None = None,
) -> V1Meta:
    accounts_meta = build_accounts_result(session, today=today, generated_at=generated_at).meta
    return build_meta(
        as_of=accounts_meta.as_of,
        source_providers=accounts_meta.source_providers,
        coverage=accounts_meta.account_coverage,
        last_successful_sync_at=accounts_meta.last_successful_sync_at,
        warnings=[],
        links=links,
        methodology=methodology,
        methodology_version="1",
        today=today,
        generated_at=generated_at,
    )


class PerformanceV1Result(BaseModel):
    """Modified-Dietz TWR vs cashflow-matched benchmark counterfactuals."""

    meta: V1Meta
    series: PerformanceSeries


class PositionPerformanceV1Result(BaseModel):
    """Per-ticker dollar alpha vs dollar-matched benchmark counterfactuals."""

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
    session: Session, *, today: date | None = None, generated_at: datetime | None = None
) -> V1Meta:
    return _analytics_meta(
        session,
        methodology="performance.modified_dietz",
        links={
            "position_performance": "/api/v1/analytics/position-performance",
            "risk": "/api/v1/analytics/risk",
            "cash_flows": "/api/v1/cash-flows",
        },
        today=today,
        generated_at=generated_at,
    )


def position_performance_meta(
    session: Session, *, today: date | None = None, generated_at: datetime | None = None
) -> V1Meta:
    return _analytics_meta(
        session,
        methodology="position_alpha.dollar_matched_counterfactual",
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
