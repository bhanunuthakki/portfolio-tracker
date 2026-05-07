"""Fetch historical close prices for every security with a layered source chain.

Source order (try the next one only when the previous returns empty):

  1. **yfinance** — Yahoo Finance via the `yfinance` package. Best coverage
     for US equities and ETFs, but flaky on delisted issues, foreign
     listings, and during Yahoo rate-limit hiccups.
  2. **Stooq** — `stooq.com` public CSV endpoint. Free, no auth, surprisingly
     reliable for US-listed equities/ETFs. Tries both the bare symbol and
     the `.US` suffix some tickers need.

Tickers without coverage in either source are reported via the data-quality
endpoint as `no_historical_prices` so the user can investigate.

Run manually:
    python -m portfolio_tracker.jobs.prices --start 2024-01-01
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import httpx
import typer
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import Price, Security, TickerOverride

_STOOQ_URL = "https://stooq.com/q/d/l/"


def run(start_date: date, end_date: date) -> tuple[int, dict[str, int]]:
    """Returns (rows_written, per_source_counts)."""
    rows_written = 0
    by_source: dict[str, int] = {"yfinance": 0, "stooq": 0, "none": 0}
    with SessionLocal() as session:
        override_map = {
            ov.security_id: ov.ticker
            for ov in session.execute(select(TickerOverride)).scalars().all()
        }
        securities = session.execute(
            select(Security).where(Security.is_cash_equivalent == False)  # noqa: E712
        ).scalars().all()
        for security in securities:
            ticker = override_map.get(security.security_id) or security.ticker
            if ticker is None:
                continue
            history, source = _fetch_history(ticker, start_date, end_date)
            by_source[source] = by_source.get(source, 0) + 1
            if not history:
                continue
            rows_written += _write_history(session, security, history)
        session.commit()
    return rows_written, by_source


def _fetch_history(
    ticker: str, start_date: date, end_date: date
) -> tuple[dict[date, Decimal], str]:
    """Try sources in order. Returns (history, source_name). Empty if all fail."""
    history = _yfinance_history(ticker, start_date, end_date)
    if history:
        return history, "yfinance"
    history = _stooq_history(ticker, start_date, end_date)
    if history:
        return history, "stooq"
    return {}, "none"


def _yfinance_history(
    ticker: str, start_date: date, end_date: date
) -> dict[date, Decimal]:
    try:
        history = yf.Ticker(ticker).history(
            start=start_date.isoformat(),
            end=(end_date + timedelta(days=1)).isoformat(),
            auto_adjust=False,
        )
    except Exception:  # noqa: BLE001 - yfinance raises a variety of exceptions
        return {}
    if history.empty:
        return {}
    out: dict[date, Decimal] = {}
    for ts, row in history.iterrows():
        bar_date = ts.date() if hasattr(ts, "date") else ts
        close = row.get("Close")
        if close is None or _is_nan(close):
            continue
        out[bar_date] = Decimal(str(close))
    return out


def _stooq_history(
    ticker: str, start_date: date, end_date: date
) -> dict[date, Decimal]:
    """Stooq US tickers usually need a `.US` suffix; try both."""
    for candidate in (ticker, f"{ticker}.US"):
        result = _stooq_one(candidate, start_date, end_date)
        if result:
            return result
    return {}


def _stooq_one(
    symbol: str, start_date: date, end_date: date
) -> dict[date, Decimal]:
    params = {
        "s": symbol.lower(),
        "d1": start_date.strftime("%Y%m%d"),
        "d2": end_date.strftime("%Y%m%d"),
        "i": "d",
    }
    try:
        response = httpx.get(_STOOQ_URL, params=params, timeout=15.0)
    except httpx.RequestError:
        return {}
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
    session: Session, security: Security, history: dict[date, Decimal]
) -> int:
    rows_written = 0
    for bar_date, close in history.items():
        existing = session.get(Price, (security.security_id, bar_date))
        if existing is not None:
            existing.close = close
            continue
        session.add(
            Price(security_id=security.security_id, date=bar_date, close=close)
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
        f"no source: {by_source.get('none', 0)}"
    )


if __name__ == "__main__":
    typer.run(main)
