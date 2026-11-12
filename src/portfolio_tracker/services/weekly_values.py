"""Read the retained value history at weekly frequency."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import Account, HoldingSnapshot, Item, PortfolioValueDaily
from portfolio_tracker.services.performance import (
    _complete_account_valuations_by_date,  # pyright: ignore[reportPrivateUsage]
    performance_account_ids,
)

ValueBasis = Literal[
    "whole_account", "marked_positions", "legacy_cached_snapshot", "legacy_cached_model"
]


class WeeklyValuePoint(BaseModel):
    week_start: date
    as_of: date
    basis: ValueBasis
    value: Decimal
    covered_account_ids: list[int] | None
    source_providers: list[str]
    valuation_observation_keys: list[str] = Field(default_factory=list[str])


class WeeklyValueHistory(BaseModel):
    currency: Literal["USD"] = "USD"
    expected_account_ids: list[int]
    points: list[WeeklyValuePoint]


class DailyValuePoint(BaseModel):
    """One retained value on its actual observation date and basis."""

    as_of: date
    basis: ValueBasis
    value: Decimal
    covered_account_ids: list[int] | None
    source_providers: list[str]
    valuation_observation_keys: list[str] = Field(default_factory=list[str])


def build_daily_value_points(
    session: Session, start_date: date, end_date: date
) -> tuple[frozenset[int], list[DailyValuePoint]]:
    """Every retained value point by date and basis, without forward filling."""
    account_ids = performance_account_ids(session, start_date, end_date)
    points: list[DailyValuePoint] = []

    valuations = _complete_account_valuations_by_date(
        session, start_date, end_date, account_ids=account_ids
    )
    for day, totals in valuations.items():
        points.append(
            DailyValuePoint(
                as_of=day,
                basis="whole_account",
                value=sum((row.total_value for row in totals), Decimal(0)),
                covered_account_ids=sorted(account_ids),
                source_providers=sorted({row.source_provider for row in totals}),
                valuation_observation_keys=sorted(row.observation_key for row in totals),
            )
        )

    marks: dict[date, dict[int, Decimal]] = defaultdict(dict)
    providers: dict[int, str] = {}
    unusable: set[tuple[date, int]] = set()
    for holding, provider in session.execute(
        select(HoldingSnapshot, Item.source)
        .join(Account, Account.account_id == HoldingSnapshot.account_id)
        .join(Item, Item.item_id == Account.item_id)
        .where(
            HoldingSnapshot.account_id.in_(account_ids),
            HoldingSnapshot.snapshot_date.between(start_date, end_date),
        )
    ):
        day, account_id = holding.snapshot_date, holding.account_id
        if (
            holding.origin != "broker"
            or holding.currency != "USD"
            or (holding.quantity != 0 and holding.institution_value is None)
        ):
            unusable.add((day, account_id))
            continue
        providers[account_id] = provider
        value = holding.institution_value or Decimal(0)
        marks[day][account_id] = marks[day].get(account_id, Decimal(0)) + value

    for day, daily_marks in marks.items():
        covered = sorted(aid for aid in daily_marks if (day, aid) not in unusable)
        if covered:
            points.append(
                DailyValuePoint(
                    as_of=day,
                    basis="marked_positions",
                    value=sum((daily_marks[aid] for aid in covered), Decimal(0)),
                    covered_account_ids=covered,
                    source_providers=sorted({providers[aid] for aid in covered}),
                )
            )

    for row in session.scalars(
        select(PortfolioValueDaily).where(
            PortfolioValueDaily.date.between(start_date, end_date),
            PortfolioValueDaily.source.in_(("snapshot", "backfill")),
        )
    ):
        points.append(
            DailyValuePoint(
                as_of=row.date,
                basis="legacy_cached_snapshot"
                if row.source == "snapshot"
                else "legacy_cached_model",
                value=row.total_value,
                covered_account_ids=None,
                source_providers=[],
            )
        )
    return account_ids, points


def build_weekly_value_history(
    session: Session, start_date: date, end_date: date
) -> WeeklyValueHistory:
    """Keep the latest actual point per ISO week and basis, without forward filling."""
    account_ids, points = build_daily_value_points(session, start_date, end_date)
    weekly: dict[tuple[date, ValueBasis], WeeklyValuePoint] = {}
    for point in points:
        week_start = point.as_of - timedelta(days=point.as_of.weekday())
        key = (week_start, point.basis)
        if key not in weekly or weekly[key].as_of < point.as_of:
            weekly[key] = WeeklyValuePoint(week_start=week_start, **point.model_dump())
    return WeeklyValueHistory(
        expected_account_ids=sorted(account_ids),
        points=[weekly[key] for key in sorted(weekly)],
    )
