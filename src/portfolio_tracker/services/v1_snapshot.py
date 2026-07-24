"""`GET /api/v1/portfolio-snapshot` — the consumer-optimized bulk read model.

One consistent read carrying what both consumers need for a starting position
(`docs/design/portfolio_data_service_prd.md` §7.2): normalized accounts with
canonical identity + detailed Tax treatment, consolidated positions with
per-lot treatment, five-way tax-bucket totals, and the SC-3 equity-fraction
Positioning fact — all under the shared envelope.

The equity fraction is deterministic and versioned. Numerator: market value of
every included holding whose asset type is not Cash (``classify_asset_type``,
which treats ``Security.is_cash_equivalent`` money-market funds as cash).
Denominator: total market value of included holdings at the same snapshot.
Both consumers read this fact instead of maintaining ticker allowlists.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from portfolio_tracker.api.routes.portfolio import (
    _consolidate_holdings,  # pyright: ignore[reportPrivateUsage]
    _latest_holding_rows,  # pyright: ignore[reportPrivateUsage]
    _load_cost_basis_overrides,  # pyright: ignore[reportPrivateUsage]
)
from portfolio_tracker.schemas import PositioningOut
from portfolio_tracker.services import positioning as positioning_service
from portfolio_tracker.services.positioning import AssetType, classify_asset_type
from portfolio_tracker.services.positions_v1 import (
    PositionV1,
    build_positions_result,
    tax_treatment,
)
from portfolio_tracker.services.v1_accounts import AccountV1, build_accounts_result
from portfolio_tracker.services.v1_common import (
    W_CALCULATION_UNAVAILABLE,
    V1Meta,
    V1Warning,
    build_meta,
)

EQUITY_FRACTION_METHODOLOGY = "equity_fraction.cash_equivalent"
EQUITY_FRACTION_VERSION = "1"

_CASH_POLICY = (
    "Cash = securities classified as Cash by broker type code or the "
    "is_cash_equivalent flag (money-market funds such as FDRXX/SPAXX count as "
    "cash). Everything else — stocks, ETFs, mutual funds, crypto, other — "
    "counts as equity exposure in the numerator. No ticker allowlist."
)

_FRACTION_QUANT = Decimal("0.000001")


class EquityFractionV1(BaseModel):
    """The SC-3 versioned Positioning fact (PRD §7.2).

    ``equity_fraction`` is a FRACTION in [0, 1] (``unit`` says so explicitly);
    None when the denominator is zero or there is no data — never silently 0.
    """

    equity_value: Decimal
    denominator_value: Decimal
    equity_fraction: Decimal | None
    unit: str
    cash_equivalent_policy: str
    included_account_ids: list[int]
    excluded_account_ids: list[int]
    holdings_as_of: date | None
    is_partial: bool
    is_stale: bool
    warnings: list[V1Warning]
    methodology: str
    methodology_version: str


class PortfolioSnapshotV1(BaseModel):
    """The bulk read model — one consistent snapshot for both consumers."""

    meta: V1Meta
    accounts: list[AccountV1]
    total_market_value: Decimal
    # Five-way detailed treatment → market value, bucketed at the lot level.
    by_tax_treatment: dict[str, Decimal]
    positions: list[PositionV1]
    equity_fraction: EquityFractionV1


def build_equity_fraction(
    session: Session,
    *,
    coverage_included: list[int],
    coverage_excluded: list[int],
    is_partial: bool,
    is_stale: bool,
) -> EquityFractionV1:
    """Compute the deterministic equity fraction over the latest snapshot."""
    rows = _latest_holding_rows(session)
    snapshot_date = rows[0][0].snapshot_date if rows else None
    equity = Decimal(0)
    total = Decimal(0)
    for holding, _account, security in rows:
        value = holding.institution_value or Decimal(0)
        total += value
        if classify_asset_type(security.type, security.is_cash_equivalent) is not AssetType.CASH:
            equity += value
    warnings: list[V1Warning] = []
    fraction: Decimal | None
    if total > 0:
        fraction = (equity / total).quantize(_FRACTION_QUANT)
    else:
        fraction = None
        warnings.append(
            V1Warning(
                code=W_CALCULATION_UNAVAILABLE,
                message="Equity fraction is unavailable: the included book has no market value.",
                scope="equity_fraction",
            )
        )
    return EquityFractionV1(
        equity_value=equity,
        denominator_value=total,
        equity_fraction=fraction,
        unit="fraction",
        cash_equivalent_policy=_CASH_POLICY,
        included_account_ids=coverage_included,
        excluded_account_ids=coverage_excluded,
        holdings_as_of=snapshot_date,
        is_partial=is_partial,
        is_stale=is_stale,
        warnings=warnings,
        methodology=EQUITY_FRACTION_METHODOLOGY,
        methodology_version=EQUITY_FRACTION_VERSION,
    )


def build_portfolio_snapshot(
    session: Session,
    *,
    today: date | None = None,
    generated_at: datetime | None = None,
) -> PortfolioSnapshotV1:
    """Assemble the bulk snapshot from the same primitives the focused
    resources use, so the two can never disagree."""
    accounts_result = build_accounts_result(session, today=today, generated_at=generated_at)
    coverage = accounts_result.meta.account_coverage

    rows = _latest_holding_rows(session)
    overrides = _load_cost_basis_overrides(session)
    snapshot_date: date | None
    if rows:
        snapshot_date = rows[0][0].snapshot_date
        consolidated = _consolidate_holdings(snapshot_date, rows, overrides)
    else:
        snapshot_date = None
        consolidated = []
    account_tax = {
        (account.account_id): tax_treatment(account.type, account.subtype)
        for _h, account, _s in rows
    }
    positions_result = build_positions_result(snapshot_date, consolidated, account_tax)

    meta = build_meta(
        as_of=snapshot_date,
        source_providers=accounts_result.meta.source_providers,
        coverage=coverage,
        last_successful_sync_at=accounts_result.meta.last_successful_sync_at,
        warnings=[w for w in accounts_result.meta.warnings if w.code not in ("NO_DATA",)],
        links={
            "accounts": "/api/v1/accounts",
            "positions": "/api/v1/portfolio/positions",
            "positioning": "/api/v1/analytics/positioning",
            "health": "/api/v1/health",
        },
        methodology="portfolio_snapshot.bulk",
        methodology_version="1",
        today=today,
        generated_at=generated_at,
    )
    equity = build_equity_fraction(
        session,
        coverage_included=coverage.included_account_ids,
        coverage_excluded=coverage.excluded_account_ids,
        is_partial=meta.is_partial,
        is_stale=meta.is_stale,
    )
    return PortfolioSnapshotV1(
        meta=meta,
        accounts=accounts_result.accounts,
        total_market_value=positions_result.total_market_value,
        by_tax_treatment=positions_result.by_tax_treatment,
        positions=positions_result.positions,
        equity_fraction=equity,
    )


class PositioningV1Result(BaseModel):
    """``GET /api/v1/analytics/positioning`` — the existing deterministic
    Positioning cuts wrapped in the shared envelope, plus the SC-3 equity
    fraction in full detail."""

    meta: V1Meta
    positioning: PositioningOut
    equity_fraction: EquityFractionV1


def build_positioning_v1(
    session: Session,
    start_date: date,
    end_date: date,
    *,
    today: date | None = None,
    generated_at: datetime | None = None,
) -> PositioningV1Result:
    positioning = positioning_service.compute_positioning(session, start_date, end_date)
    accounts_result = build_accounts_result(session, today=today, generated_at=generated_at)
    coverage = accounts_result.meta.account_coverage
    equity = build_equity_fraction(
        session,
        coverage_included=coverage.included_account_ids,
        coverage_excluded=coverage.excluded_account_ids,
        is_partial=accounts_result.meta.is_partial,
        is_stale=accounts_result.meta.is_stale,
    )
    meta = build_meta(
        as_of=accounts_result.meta.as_of,
        source_providers=accounts_result.meta.source_providers,
        coverage=coverage,
        last_successful_sync_at=accounts_result.meta.last_successful_sync_at,
        warnings=[],
        links={
            "accounts": "/api/v1/accounts",
            "portfolio_snapshot": "/api/v1/portfolio-snapshot",
            "positions": "/api/v1/portfolio/positions",
        },
        methodology="positioning.value_weighted_cuts",
        methodology_version="1",
        today=today,
        generated_at=generated_at,
    )
    return PositioningV1Result(meta=meta, positioning=positioning, equity_fraction=equity)
