"""Refresh upcoming earnings dates for currently-held positions.

Pulls from yfinance's `Ticker.calendar` and `Ticker.earnings_dates`. Only
runs against tickers that appear in today's `holdings_snapshots` so we
don't waste calls on names you don't care about. Idempotent — UPSERT on
(ticker, earnings_date) so re-runs just refresh the consensus / actual
columns as they update.

Run manually:
    python -m portfolio_tracker.jobs.earnings_calendar

Wired into `jobs.daily_refresh` so it runs automatically each morning.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import yfinance as yf
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import (
    EarningsCalendar,
    HoldingSnapshot,
    Security,
)

logger = logging.getLogger(__name__)


def run() -> int:
    """Refresh earnings dates for held tickers. Returns rows upserted.

    Quiet on per-ticker failures (yfinance is flaky on some symbols);
    logs and continues. Total return value is the count of successful
    upserts so callers can sanity-check.
    """
    written = 0
    with SessionLocal() as session:
        tickers = _held_tickers(session)
        for ticker in tickers:
            try:
                rows = _fetch_for_ticker(ticker)
            except Exception as exc:  # yfinance can throw plenty of variants
                logger.warning("earnings fetch failed for %s: %s", ticker, exc)
                continue
            for row in rows:
                _upsert(session, row)
                written += 1
        session.commit()
    return written


def _held_tickers(session: Session) -> list[str]:
    """Latest-snapshot tickers that aren't cash equivalents."""
    latest = session.execute(
        select(HoldingSnapshot.snapshot_date)
        .order_by(HoldingSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return []
    rows = (
        session.execute(
            select(Security.ticker)
            .join(HoldingSnapshot, HoldingSnapshot.security_id == Security.security_id)
            .where(HoldingSnapshot.snapshot_date == latest)
            .where(Security.is_cash_equivalent.is_(False))
            .where(Security.ticker.is_not(None))
            .where(Security.type != "derivative")
            .distinct()
        )
        .scalars()
        .all()
    )
    return sorted({t for t in rows if t})


def _fetch_for_ticker(ticker: str) -> list[dict[str, object]]:
    """Hit yfinance for past + upcoming earnings rows.

    yfinance's `earnings_dates` returns a pandas DataFrame indexed by
    UTC datetime, with columns `EPS Estimate`, `Reported EPS`,
    `Surprise(%)`. We project to the columns the DB cares about and
    coerce types defensively — yfinance is lossy on edge cases.
    """
    out: list[dict[str, object]] = []
    yt = yf.Ticker(ticker)
    df = None
    try:
        df = yt.get_earnings_dates(limit=12)
    except Exception:
        df = getattr(yt, "earnings_dates", None)
    if df is None or len(df) == 0:
        return out

    # Normalize: pandas gives us a DataFrame; coerce to plain dicts.
    for raw_idx, row in cast(Any, df.iterrows()):
        # raw_idx is a Timestamp in the broker's tz; we only persist the
        # date portion since "earnings on Tuesday" is what matters for
        # the calendar surface.
        try:
            edate = raw_idx.date() if hasattr(raw_idx, "date") else None
        except Exception:
            edate = None
        if edate is None:
            continue
        eps_est = _coerce_decimal(row.get("EPS Estimate"))
        eps_act = _coerce_decimal(row.get("Reported EPS"))
        out.append(
            {
                "ticker": ticker,
                "earnings_date": edate,
                "earnings_time": None,  # yfinance doesn't reliably surface AM/PM
                "eps_estimate": eps_est,
                "eps_actual": eps_act,
                # Revenue isn't on this DataFrame; we leave it null and
                # rely on fundamental dashboards if we ever wire one up.
                "revenue_estimate": None,
                "revenue_actual": None,
            }
        )
    return out


def _coerce_decimal(v: object) -> Decimal | None:
    """Best-effort cast: yfinance gives floats, NaNs, occasional strings."""
    if v is None:
        return None
    try:
        # NaN check via float; Decimal.NaN is its own special case.
        f = float(cast(Any, v))
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return Decimal(str(round(f, 4)))


def _upsert(session: Session, row: dict[str, object]) -> None:
    stmt = sqlite_insert(EarningsCalendar).values(**row, fetched_at=datetime.now(UTC))
    stmt = stmt.on_conflict_do_update(
        index_elements=[EarningsCalendar.ticker, EarningsCalendar.earnings_date],
        set_={
            "earnings_time": stmt.excluded.earnings_time,
            "eps_estimate": stmt.excluded.eps_estimate,
            "eps_actual": stmt.excluded.eps_actual,
            "revenue_estimate": stmt.excluded.revenue_estimate,
            "revenue_actual": stmt.excluded.revenue_actual,
            "fetched_at": stmt.excluded.fetched_at,
        },
    )
    session.execute(stmt)


if __name__ == "__main__":
    n = run()
    print(f"earnings_calendar: refreshed {n} rows")
