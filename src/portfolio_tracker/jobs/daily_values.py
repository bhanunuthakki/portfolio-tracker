"""Materialize the daily portfolio-value cache (`portfolio_values_daily`).

The performance service can compute daily values on the fly from
`holdings_snapshots` (forward) + transaction walk-back (historical).
That works but is O(snapshots + transactions) per chart request, and once
snapshots cross the 24-month Plaid retention window the backfill loses
fidelity. This job collapses that work into one row per date so the chart
becomes a flat read.

Layered semantics:
  * Today and forward — `source = "snapshot"`, written when a holdings
    snapshot is taken. Authoritative.
  * Historical dates the engine reconstructs — `source = "backfill"`. Will
    drift over time as transactions age out of Plaid; the bootstrap is a
    one-shot crystallization of the best estimate while the data still
    exists.

Run modes:
    # bootstrap from the very first reconstructable date through today
    python -m portfolio_tracker.jobs.daily_values --bootstrap

    # incremental: append only today's value (cheap, idempotent — for cron)
    python -m portfolio_tracker.jobs.daily_values

    # custom window
    python -m portfolio_tracker.jobs.daily_values --start 2024-01-01 --end 2024-06-30

    # preview modeled-cache invalidation after a transaction/override correction
    python -m portfolio_tracker.jobs.daily_values --rebuild-modeled --start 2024-01-01

    # apply the reviewed invalidation, then reconstruct and persist modeled rows
    python -m portfolio_tracker.jobs.daily_values --rebuild-modeled --apply --start 2024-01-01
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import HoldingSnapshot, PortfolioValueDaily
from portfolio_tracker.services.performance import (
    _daily_portfolio_value,  # pyright: ignore[reportPrivateUsage]
)

# Maximum bootstrap span — match Plaid's investment-transaction retention
# so we don't try to reconstruct dates with no source data anyway.
_BOOTSTRAP_MAX_LOOKBACK_DAYS = 730


def run(
    start_date: date | None = None,
    end_date: date | None = None,
    bootstrap: bool = False,
    rebuild_modeled: bool = False,
    apply_rebuild: bool = False,
) -> int:
    """Compute and upsert daily portfolio values for the given window.

    Returns the number of rows written.

    `rebuild_modeled=True` explicitly invalidates derived cache rows in the
    requested window before recomputing. It is dry-run-only unless
    `apply_rebuild=True`; observed rows are never deleted.

    `bootstrap=True` widens the start to the earliest reconstructable date
    (capped at `_BOOTSTRAP_MAX_LOOKBACK_DAYS`). Otherwise defaults to a
    single-day incremental window ending today.
    """
    today = date.today()
    if end_date is None:
        end_date = today

    with SessionLocal() as session:
        if bootstrap or start_date is None:
            start_date = _resolve_start(session, start_date, bootstrap, today)

        if start_date > end_date:
            return 0

        if rebuild_modeled:
            preview = preview_modeled_cache_rebuild(session, start_date, end_date)
            if not apply_rebuild:
                return 0
            apply_modeled_cache_rebuild(session, preview)
            session.flush()

        # `_daily_portfolio_value` already prefers snapshots and falls back
        # to the (now cash-adjusted) transaction walk-back. Tag each row's
        # `source` based on whether a snapshot exists for that date.
        values = _daily_portfolio_value(session, start_date, end_date)
        snapshot_dates = _dates_with_snapshots(session, start_date, end_date)

        rows_written = _upsert(session, values, snapshot_dates)
        session.commit()
        return rows_written


def _resolve_start(session: Session, start_date: date | None, bootstrap: bool, today: date) -> date:
    """Pick a start date when the caller didn't pin one.

    * Explicit `start_date` wins.
    * `bootstrap` reaches back as far as Plaid retention allows. The
      walk-back uses the earliest snapshot as its anchor and reverses
      transactions from there, so going farther back than the retention
      floor produces no data anyway.
    * Default (incremental) is just today.
    """
    if start_date is not None:
        return start_date
    if not bootstrap:
        return today
    return today - timedelta(days=_BOOTSTRAP_MAX_LOOKBACK_DAYS)


def _dates_with_snapshots(session: Session, start_date: date, end_date: date) -> set[date]:
    from portfolio_tracker.services.performance import (
        _complete_snapshot_dates,  # pyright: ignore[reportPrivateUsage]
    )

    rows = (
        session.execute(
            select(HoldingSnapshot.snapshot_date)
            .where(HoldingSnapshot.snapshot_date >= start_date)
            .where(HoldingSnapshot.snapshot_date <= end_date)
            .distinct()
        )
        .scalars()
        .all()
    )
    return set(_complete_snapshot_dates(session, set(rows)))


@dataclass(frozen=True)
class ModeledCacheRebuildPreview:
    start_date: date
    end_date: date
    modeled_rows_to_replace: int
    observed_rows_preserved: int


def preview_modeled_cache_rebuild(
    session: Session, start_date: date, end_date: date
) -> ModeledCacheRebuildPreview:
    """Describe a modeled-cache rebuild without mutating persistent state."""
    counts: dict[str, int] = {}
    for source, count in session.execute(
        select(PortfolioValueDaily.source, func.count())
        .where(PortfolioValueDaily.date >= start_date)
        .where(PortfolioValueDaily.date <= end_date)
        .group_by(PortfolioValueDaily.source)
    ).all():
        counts[source] = int(count)
    return ModeledCacheRebuildPreview(
        start_date=start_date,
        end_date=end_date,
        modeled_rows_to_replace=int(counts.get("backfill", 0)),
        observed_rows_preserved=int(counts.get("snapshot", 0)),
    )


def apply_modeled_cache_rebuild(session: Session, preview: ModeledCacheRebuildPreview) -> int:
    """Delete only modeled rows named by a previously reviewed preview."""
    session.execute(
        delete(PortfolioValueDaily)
        .where(PortfolioValueDaily.date >= preview.start_date)
        .where(PortfolioValueDaily.date <= preview.end_date)
        .where(PortfolioValueDaily.source == "backfill")
    )
    return preview.modeled_rows_to_replace


def _upsert(
    session: Session,
    values: dict[date, Decimal],
    snapshot_dates: set[date],
) -> int:
    """Idempotent overwrite — every (re)run trumps any prior cached value
    for the same date. Bootstrap and daily runs are both safe to re-execute."""
    if not values:
        return 0
    payload = [
        {
            "date": d,
            "total_value": v,
            "total_cost_basis": None,
            "source": "snapshot" if d in snapshot_dates else "backfill",
        }
        for d, v in values.items()
    ]
    # SQLite's UPSERT keyed on the PK (`date`).
    stmt = sqlite_insert(PortfolioValueDaily).values(payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PortfolioValueDaily.date],
        set_={
            "total_value": stmt.excluded.total_value,
            "total_cost_basis": stmt.excluded.total_cost_basis,
            "source": stmt.excluded.source,
        },
    )
    session.execute(stmt)
    return len(payload)


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Crystallize the full reconstructable history (~24 months).",
    )
    parser.add_argument(
        "--rebuild-modeled",
        action="store_true",
        help=(
            "Preview invalidation of modeled cache rows before reconstructing "
            "them from corrected transactions and overrides."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply --rebuild-modeled after printing its preview.",
    )
    parser.add_argument(
        "--start",
        type=_parse_date,
        help="ISO date for the start of the window (overrides --bootstrap).",
    )
    parser.add_argument(
        "--end",
        type=_parse_date,
        help="ISO date for the end of the window (defaults to today).",
    )
    return parser


if __name__ == "__main__":
    args = _build_argparser().parse_args()
    if args.apply and not args.rebuild_modeled:
        raise SystemExit("--apply requires --rebuild-modeled")
    if args.rebuild_modeled:
        with SessionLocal() as preview_session:
            preview_today = date.today()
            preview_end = args.end or preview_today
            preview_start = _resolve_start(
                preview_session,
                args.start,
                args.bootstrap,
                preview_today,
            )
            preview = preview_modeled_cache_rebuild(preview_session, preview_start, preview_end)
        print(
            "daily_values modeled-cache rebuild preview: "
            f"{preview.modeled_rows_to_replace} modeled rows would be replaced; "
            f"{preview.observed_rows_preserved} observed rows would be preserved; "
            f"window {preview.start_date} -> {preview.end_date}; "
            f"apply={'yes' if args.apply else 'no'}"
        )
        if not args.apply:
            raise SystemExit(0)
    written = run(
        start_date=args.start,
        end_date=args.end,
        bootstrap=args.bootstrap,
        rebuild_modeled=args.rebuild_modeled,
        apply_rebuild=args.apply,
    )
    span = (args.start, args.end or date.today())
    # Plain ASCII so the message survives the Windows cp1252 console.
    print(
        f"daily_values complete: {written} rows written for {span[0] or 'auto-start'} -> {span[1]}"
    )
