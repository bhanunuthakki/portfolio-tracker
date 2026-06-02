"""Read-only connector to the companion `earnings-summary` project.

`earnings-summary` is a separate quarterly-research pipeline that lives at
`../earnings-summary/`. It tracks ~12 portfolio tickers + a watchlist,
hydrates them with FMP earnings dates, thesis state, and per-quarter brief
HTML files. Both projects use SQLite, so we open its DB read-only and
enrich the portfolio-tracker Holdings / Trade Analysis surfaces with:

  * Whether the ticker is on the user's research watchlist (`tracked`)
  * Next expected earnings date (FMP, more reliable than yfinance for
    forward dates)
  * Thesis status (`ok` / `breach` / ...) from `thesis_state.breach_status`
  * Path to the most recent brief HTML, exposed via passthrough route

When the companion project isn't installed (paths don't exist), every
lookup returns an empty dict and the dashboard silently degrades — the
portfolio-tracker continues to work standalone.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from portfolio_tracker.config import get_settings


@dataclass(frozen=True)
class TickerSummary:
    """One row's worth of enrichment from earnings-summary."""

    ticker: str
    tracked: bool  # is it in tracked_companies?
    list_type: str | None  # "portfolio" | "watchlist" | None
    next_earnings_date: date | None  # from expected_earnings
    thesis_status: str | None  # "ok" | "breach" | etc.
    thesis_summary: str | None  # short thesis text
    latest_brief_iso_date: str | None  # "2026-05-13" — filename component
    has_brief: bool  # True if latest_brief_iso_date is set


def is_available() -> bool:
    """True iff the companion DB file exists. Avoids exception-driven
    detection in hot paths."""
    return _db_path().is_file()


def summary_by_ticker(tickers: list[str]) -> dict[str, TickerSummary]:
    """Return enrichment for each requested ticker. Missing tickers
    (not tracked, no earnings date, etc.) are returned with sensible
    None fields rather than omitted — caller can render uniformly.

    Empty dict when the companion DB isn't available OR when the schema
    has drifted (some queries can fail individually if earnings-summary
    is mid-migration or has renamed tables). Each sub-query is wrapped
    independently so a missing `expected_earnings` table doesn't kill
    the `tracked_companies` lookup, and vice versa.
    """
    if not tickers:
        return {}
    if not is_available():
        return {}
    try:
        conn = _connect_readonly()
    except sqlite3.OperationalError:
        return {}
    try:
        tracked = _safe(_tracked_companies, conn, default={})
        next_earnings = _safe(_next_earnings, conn, tickers, default={})
        thesis = _safe(_thesis_state, conn, tickers, default={})
    finally:
        conn.close()

    output_dir = _output_dir()
    out: dict[str, TickerSummary] = {}
    for t in tickers:
        t_norm = t.upper().strip()
        tinfo = tracked.get(t_norm)
        out[t_norm] = TickerSummary(
            ticker=t_norm,
            tracked=tinfo is not None,
            list_type=tinfo,
            next_earnings_date=next_earnings.get(t_norm),
            thesis_status=thesis.get(t_norm, (None, None))[0],
            thesis_summary=thesis.get(t_norm, (None, None))[1],
            latest_brief_iso_date=_latest_brief_iso_date(output_dir, t_norm),
            has_brief=bool(_latest_brief_iso_date(output_dir, t_norm)),
        )
    return out


def all_brief_iso_dates(ticker: str) -> list[str]:
    """Every {date} stem found under output/research/{TICKER}/, sorted
    newest-first. Empty list when nothing exists or ticker is unsafe."""
    ticker_norm = ticker.upper().strip()
    if not _safe_ticker(ticker_norm):
        return []
    output_dir = _output_dir()
    tdir = output_dir / "research" / ticker_norm
    if not tdir.is_dir():
        return []
    iso_dates: list[str] = []
    for f in tdir.iterdir():
        name = f.name
        if name.endswith("_report.html"):
            iso = name[: -len("_report.html")]
            if _is_iso_date(iso):
                iso_dates.append(iso)
    return sorted(iso_dates, reverse=True)


def latest_brief_path(ticker: str) -> Path | None:
    """Absolute path to the most recent {date}_report.html for a ticker.

    Returns None when no brief exists. Caller is responsible for confirming
    the path is under the configured output dir before serving (defense
    against ../ traversal via the ticker string)."""
    ticker_norm = ticker.upper().strip()
    if not _safe_ticker(ticker_norm):
        return None
    output_dir = _output_dir()
    iso = _latest_brief_iso_date(output_dir, ticker_norm)
    if not iso:
        return None
    candidate = output_dir / "research" / ticker_norm / f"{iso}_report.html"
    if not candidate.is_file():
        return None
    # Defense in depth: ensure resolved path is under output_dir
    try:
        candidate.resolve().relative_to(output_dir.resolve())
    except ValueError:
        return None
    return candidate


# ---- internals ---------------------------------------------------------


def _db_path() -> Path:
    return get_settings().resolved_earnings_summary_db_path


def _output_dir() -> Path:
    return get_settings().resolved_earnings_summary_output_dir


def _connect_readonly() -> sqlite3.Connection:
    # URI mode lets us pass `?mode=ro` which prevents accidental writes
    # to the companion DB. Using an absolute path with file: prefix.
    uri = f"file:{_db_path().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _tracked_companies(conn: sqlite3.Connection) -> dict[str, str]:
    """ticker -> list_type for every non-archived tracked company."""
    rows = conn.execute(
        "SELECT ticker, list_type FROM tracked_companies WHERE archived_at IS NULL"
    ).fetchall()
    return {t.upper(): lt for t, lt in rows}


def _next_earnings(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, date]:
    """Earliest upcoming expected_date for each ticker (today or later)."""
    if not tickers:
        return {}
    today = date.today().isoformat()
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT ticker, MIN(expected_date)
        FROM expected_earnings
        WHERE expected_date >= ?
          AND ticker IN ({placeholders})
        GROUP BY ticker
        """,
        [today, *[t.upper() for t in tickers]],
    ).fetchall()
    out: dict[str, date] = {}
    for ticker, iso in rows:
        if iso:
            try:
                out[ticker.upper()] = date.fromisoformat(iso)
            except ValueError:
                continue
    return out


def _thesis_state(
    conn: sqlite3.Connection, tickers: list[str]
) -> dict[str, tuple[str | None, str | None]]:
    """ticker -> (breach_status, short thesis text)."""
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT ticker, breach_status, thesis
        FROM thesis_state
        WHERE ticker IN ({placeholders})
        """,
        [t.upper() for t in tickers],
    ).fetchall()
    return {t.upper(): (status, _short_thesis(thesis)) for t, status, thesis in rows}


def _short_thesis(text: str | None, max_chars: int = 280) -> str | None:
    if not text:
        return None
    t = text.strip()
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1].rstrip() + "…"


def _latest_brief_iso_date(output_dir: Path, ticker: str) -> str | None:
    """Filename date stem of the most recent {date}_report.html under
    output/research/{ticker}/, or None if directory or files missing."""
    if not _safe_ticker(ticker):
        return None
    tdir = output_dir / "research" / ticker
    if not tdir.is_dir():
        return None
    iso_dates: list[str] = []
    for f in tdir.iterdir():
        name = f.name
        if name.endswith("_report.html"):
            iso = name[: -len("_report.html")]
            if _is_iso_date(iso):
                iso_dates.append(iso)
    if not iso_dates:
        return None
    return max(iso_dates)


def _safe_ticker(s: str) -> bool:
    # Strict ASCII ticker — prevents path-traversal via crafted ticker.
    return bool(s) and all(c.isalnum() or c in "._-" for c in s) and ".." not in s


def _safe(fn, *args, default):
    """Run an earnings-summary sub-query and return `default` if the
    companion DB throws an OperationalError (typically a missing or
    renamed table). Lets callers degrade per-query instead of all-or-
    nothing — e.g., thesis lookups still work even when the
    `expected_earnings` table is missing."""
    try:
        return fn(*args)
    except sqlite3.OperationalError:
        return default


def _is_iso_date(s: str) -> bool:
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False
