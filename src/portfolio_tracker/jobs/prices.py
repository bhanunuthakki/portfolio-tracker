"""Fetch historical close prices for every security with a layered source chain.

Source order (try the next one only when the previous returns empty):

  1. **yfinance** — Yahoo Finance via the `yfinance` package. Best coverage
     for US equities and ETFs, but flaky on delisted issues, foreign
     listings, and during Yahoo rate-limit hiccups. The job requests
     `auto_adjust=False` to preserve Close separately from Adj Close. Yahoo's
     Close remains split-adjusted but does not include dividend reinvestment,
     so it is persisted as `split_adjusted`.
  2. **Stooq** — `stooq.com` public CSV endpoint. Free, no auth, surprisingly
     reliable for US-listed equities/ETFs. Tries both the bare symbol and
     the `.US` suffix some tickers need. Its adjustment basis is persisted as
     `unknown` until that contract is independently established.

Tickers without coverage in either source are reported via the data-quality
endpoint as `no_historical_prices` so the user can investigate.

Each run prices only securities held or traded during the requested window.
Closed positions, expired options and delisted tickers can never return new
data, so they are skipped. Each security commits on its own, so a stopped run
keeps its completed rows. The first Stooq connection failure turns Stooq off
for the rest of the run, and an optional time budget defers remaining work.

Run manually:
    python -m portfolio_tracker.jobs.prices --start 2024-01-01
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, cast

import httpx
import typer
import yfinance as yf
from sqlalchemy import and_, func, select, union_all
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import (
    HoldingSnapshot,
    InvestmentTransaction,
    Price,
    PriceAdjustmentBasis,
    PriceSource,
    Security,
    TickerOverride,
)

_STOOQ_URL = "https://stooq.com/q/d/l/"
_STOOQ_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
# daily_refresh runs under a 30-minute task limit. This budget leaves time for
# the steps after prices.
DAILY_TIME_BUDGET_SECONDS = 600.0


class _StooqUnreachableError(Exception):
    """Stooq did not answer at the transport level."""


def run(
    start_date: date, end_date: date, time_budget_seconds: float | None = None
) -> tuple[int, dict[str, int]]:
    """Returns (rows_written, counts per source plus `deferred`).

    With a budget, no new lookup starts after it ends. Open positions go first.
    """
    rows_written = 0
    by_source: dict[str, int] = {"yfinance": 0, "stooq": 0, "none": 0, "deferred": 0}
    deadline = None if time_budget_seconds is None else monotonic() + time_budget_seconds
    use_stooq = True
    with SessionLocal() as session:
        targets = _securities_to_price(session, start_date, end_date)
        for position, (security, ticker) in enumerate(targets):
            if deadline is not None and monotonic() >= deadline:
                by_source["deferred"] = len(targets) - position
                break
            try:
                history, source, adjustment_basis = _fetch_history(
                    ticker, start_date, end_date, use_stooq=use_stooq
                )
            except _StooqUnreachableError:
                use_stooq = False
                history, source = {}, "none"
                adjustment_basis = PriceAdjustmentBasis.UNKNOWN.value
            by_source[source] = by_source.get(source, 0) + 1
            if not history:
                continue
            rows_written += _write_history(
                session,
                security,
                history,
                source=source,
                adjustment_basis=adjustment_basis,
            )
            session.commit()
    return rows_written, by_source


def _securities_to_price(
    session: Session, start_date: date, end_date: date
) -> list[tuple[Security, str]]:
    """Return (security, ticker) pairs whose holding period overlaps the window.

    The period runs from the first to the last snapshot or transaction. A
    position in an account's latest snapshot stays open, even if that account
    stopped syncing, until its option contract expires. Open positions go first.
    """
    latest = (
        select(
            HoldingSnapshot.account_id,
            func.max(HoldingSnapshot.snapshot_date).label("latest_date"),
        )
        .where(HoldingSnapshot.snapshot_date <= end_date)
        .group_by(HoldingSnapshot.account_id)
        .subquery()
    )
    open_ids = set(
        session.scalars(
            select(HoldingSnapshot.security_id)
            .join(
                latest,
                and_(
                    HoldingSnapshot.account_id == latest.c.account_id,
                    HoldingSnapshot.snapshot_date == latest.c.latest_date,
                ),
            )
            .where(HoldingSnapshot.quantity != 0)
        )
    )
    activity = union_all(
        select(
            HoldingSnapshot.security_id.label("security_id"),
            HoldingSnapshot.snapshot_date.label("activity_date"),
        ).where(HoldingSnapshot.quantity != 0),
        select(InvestmentTransaction.security_id, InvestmentTransaction.date).where(
            InvestmentTransaction.security_id.is_not(None)
        ),
    ).subquery()
    active_ids = set(
        session.scalars(
            select(activity.c.security_id)
            .group_by(activity.c.security_id)
            .having(
                func.min(activity.c.activity_date) <= end_date,
                func.max(activity.c.activity_date) >= start_date,
            )
        )
    )
    overrides = {
        ov.security_id: ov.ticker for ov in session.execute(select(TickerOverride)).scalars().all()
    }
    targets: list[tuple[bool, int, Security, str]] = []
    securities = session.scalars(select(Security).where(Security.is_cash_equivalent.is_(False)))
    for security in securities:
        sid = security.security_id
        ticker = overrides.get(sid) or security.ticker
        if ticker is None or sid not in open_ids | active_ids:
            continue
        if _option_expired_before(security, start_date):
            continue
        targets.append((sid not in open_ids, sid, security, ticker))
    targets.sort(key=lambda target: target[:2])
    return [(security, ticker) for _, _, security, ticker in targets]


def _option_expired_before(security: Security, start_date: date) -> bool:
    if security.option_contract_json is None:
        return False
    expiration = cast(dict[str, Any], json.loads(security.option_contract_json)).get(
        "expiration_date"
    )
    return isinstance(expiration, str) and date.fromisoformat(expiration) < start_date


def _fetch_history(
    ticker: str, start_date: date, end_date: date, *, use_stooq: bool = True
) -> tuple[dict[date, Decimal], str, str]:
    """Return history plus explicit provider and adjustment-basis provenance."""
    history = _yfinance_history(ticker, start_date, end_date)
    if history:
        return (
            history,
            PriceSource.YFINANCE.value,
            PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
        )
    if use_stooq:
        history = _stooq_history(ticker, start_date, end_date)
        if history:
            return history, PriceSource.STOOQ.value, PriceAdjustmentBasis.UNKNOWN.value
    return {}, "none", PriceAdjustmentBasis.UNKNOWN.value


def _yfinance_history(ticker: str, start_date: date, end_date: date) -> dict[date, Decimal]:
    try:
        history = yf.Ticker(ticker).history(
            start=start_date.isoformat(),
            end=(end_date + timedelta(days=1)).isoformat(),
            auto_adjust=False,
        )
    except Exception:  # yfinance raises a variety of exceptions
        return {}
    if history.empty:
        return {}
    out: dict[date, Decimal] = {}
    for ts, row in cast(Any, history.iterrows()):
        bar_date = ts.date() if hasattr(ts, "date") else ts
        close = row.get("Close")
        if close is None or _is_nan(close):
            continue
        out[bar_date] = Decimal(str(close))
    return out


def _stooq_history(ticker: str, start_date: date, end_date: date) -> dict[date, Decimal]:
    """Stooq US tickers usually need a `.US` suffix; try both."""
    for candidate in (ticker, f"{ticker}.US"):
        result = _stooq_one(candidate, start_date, end_date)
        if result:
            return result
    return {}


def _stooq_one(symbol: str, start_date: date, end_date: date) -> dict[date, Decimal]:
    params = {
        "s": symbol.lower(),
        "d1": start_date.strftime("%Y%m%d"),
        "d2": end_date.strftime("%Y%m%d"),
        "i": "d",
    }
    try:
        response = httpx.get(_STOOQ_URL, params=params, timeout=_STOOQ_TIMEOUT)
    except httpx.RequestError as exc:
        raise _StooqUnreachableError from exc
    if response.status_code != 200:
        return {}
    text = response.text.strip()
    if not text or text.lower().startswith("no data"):
        return {}
    lines = text.splitlines()
    if len(lines) < 2:
        return {}
    # Header: Date,Open,High,Low,Close,Volume
    out: dict[date, Decimal] = {}
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            bar_date = date.fromisoformat(parts[0])
            close = Decimal(parts[4])
        except (ValueError, InvalidOperation):
            continue
        out[bar_date] = close
    return out


def _write_history(
    session: Session,
    security: Security,
    history: dict[date, Decimal],
    *,
    source: str,
    adjustment_basis: str,
) -> int:
    """Upsert price values and provenance; return the count of newly added rows."""
    rows_written = 0
    for bar_date, close in history.items():
        existing = session.get(Price, (security.security_id, bar_date))
        if existing is not None:
            existing.close = close
            existing.source = source
            existing.adjustment_basis = adjustment_basis
            continue
        session.add(
            Price(
                security_id=security.security_id,
                date=bar_date,
                close=close,
                source=source,
                adjustment_basis=adjustment_basis,
            )
        )
        rows_written += 1
    return rows_written


def _is_nan(value: object) -> bool:
    try:
        return value != value  # type: ignore[comparison-overlap]
    except TypeError:
        return False


def main(
    start: str = typer.Option(..., help="ISO start date, e.g. 2024-01-01"),
    end: str | None = typer.Option(None, help="ISO end date; defaults to today"),
) -> None:
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end is not None else date.today()
    written, by_source = run(start_date, end_date)
    print(f"prices complete: {written} new price rows written")
    print(
        f"  yfinance: {by_source.get('yfinance', 0)} | "
        f"stooq: {by_source.get('stooq', 0)} | "
        f"no source: {by_source.get('none', 0)} | "
        f"deferred: {by_source.get('deferred', 0)}"
    )


if __name__ == "__main__":
    typer.run(main)
