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

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TypeVar, cast

from portfolio_tracker.config import get_settings

_T = TypeVar("_T")


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


def _safe(fn: Callable[..., _T], *args: object, default: _T) -> _T:
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


# ---------------------------------------------------------------------------
# Deep ingestion (P2): thesis verdict, valuation, and alerts
#
# `summary_by_ticker` above reads only the coarse `thesis_state.breach_status`.
# These readers expose the authoritative signals the earnings-summary pipeline
# actually computes:
#   * thesis_evaluations.overall_status  -> "ok" | "warn" | "breach" (+ per-rule)
#   * dcf_runs                            -> fair value, live price, over/under
#   * alerts                              -> pending fundamental alerts
# All read-only and defensively wrapped: a missing/renamed table degrades to an
# empty result rather than raising, preserving this module's standalone-friendly
# contract.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleEval:
    """One evaluated thesis rule (from thesis_evaluations.rule_evaluations_json)."""

    rule_id: str
    kpi_name: str
    status: str  # "ok" | "warn" | "breach"
    tier: str | None
    narrative: str | None


@dataclass(frozen=True)
class ThesisVerdict:
    """Latest thesis evaluation for a ticker."""

    ticker: str
    status: str  # "ok" | "warn" | "breach"
    evaluated_at: str | None
    rules: tuple[RuleEval, ...]

    @property
    def flagged_rules(self) -> tuple[RuleEval, ...]:
        """Rules currently in warn/breach, worst (breach) first."""
        rank = {"breach": 0, "warn": 1}
        return tuple(
            sorted((r for r in self.rules if r.status in rank), key=lambda r: rank[r.status])
        )


@dataclass(frozen=True)
class Valuation:
    """Latest DCF valuation snapshot for a ticker."""

    ticker: str
    valuation_date: str | None
    fair_value: float | None  # npv_per_share
    live_price: float | None
    over_under_pct: float | None  # (live - fair) / fair; negative => below fair (cheap)
    mos_bar: float | None
    currency: str | None

    @property
    def signal(self) -> str:
        """`rich` (>= +MOS over fair), `cheap` (<= -MOS under fair), else `fair`."""
        if self.over_under_pct is None:
            return "unknown"
        mos = self.mos_bar or 0.0
        if self.over_under_pct >= mos:
            return "rich"
        if self.over_under_pct <= -mos:
            return "cheap"
        return "fair"


@dataclass(frozen=True)
class ThesisAlert:
    """A pending fundamental alert from earnings-summary's `alerts` feed."""

    ticker: str
    trigger_kind: str  # kpi_inflection|earnings_tone|saydo_due|thesis_drift|material_news
    fired_at: str | None
    status: str
    signature_sha: str | None


@dataclass(frozen=True)
class ThesisDetail:
    """Full per-ticker thesis health: verdict + valuation + alerts + thesis text."""

    ticker: str
    tracked: bool
    list_type: str | None
    thesis_summary: str | None
    verdict: ThesisVerdict | None
    valuation: Valuation | None
    alerts: tuple[ThesisAlert, ...]


def latest_verdicts(tickers: list[str]) -> dict[str, ThesisVerdict]:
    """Latest thesis evaluation per ticker. Empty when the companion DB is
    unavailable or the table is missing."""
    return _query_map(_verdicts, tickers)


def latest_valuations(tickers: list[str]) -> dict[str, Valuation]:
    """Latest DCF valuation snapshot per ticker."""
    return _query_map(_valuations, tickers)


def pending_alerts(tickers: list[str]) -> dict[str, tuple[ThesisAlert, ...]]:
    """Pending (un-actioned) fundamental alerts per ticker, newest first."""
    return _query_map(_alerts, tickers)


def thesis_detail(ticker: str) -> ThesisDetail | None:
    """Assemble the full thesis-health picture for one ticker.

    Returns None when the ticker is unsafe or the companion DB is absent.
    Reuses the public readers (each opens its own short-lived read-only
    connection) — fine for a single-ticker detail / drill-down call.
    """
    t = ticker.upper().strip()
    if not _safe_ticker(t) or not is_available():
        return None
    base = summary_by_ticker([t]).get(t)
    return ThesisDetail(
        ticker=t,
        tracked=base.tracked if base else False,
        list_type=base.list_type if base else None,
        thesis_summary=base.thesis_summary if base else None,
        verdict=latest_verdicts([t]).get(t),
        valuation=latest_valuations([t]).get(t),
        alerts=pending_alerts([t]).get(t, ()),
    )


def untracked_holdings(tickers: list[str]) -> list[str]:
    """Subset of `tickers` with no portfolio/watchlist coverage in
    earnings-summary — i.e. thesis blind spots. Preserves input order."""
    if not tickers or not is_available():
        return []
    try:
        conn = _connect_readonly()
    except sqlite3.OperationalError:
        return []
    try:
        tracked = _tracked_companies(conn)
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    covered = {tk for tk, lt in tracked.items() if lt in ("portfolio", "watchlist")}
    return [tk for tk in tickers if tk.upper().strip() not in covered]


# ---- deep-ingestion internals ---------------------------------------------


def _query_map(
    fn: Callable[[sqlite3.Connection, list[str]], dict[str, _T]], tickers: list[str]
) -> dict[str, _T]:
    """Open a read-only connection, run `fn`, and degrade to `{}` if the
    companion DB is missing or a queried table doesn't exist."""
    if not tickers or not is_available():
        return {}
    try:
        conn = _connect_readonly()
    except sqlite3.OperationalError:
        return {}
    try:
        return fn(conn, tickers)
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def _verdicts(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, ThesisVerdict]:
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT te.ticker, te.evaluated_at, te.overall_status, te.rule_evaluations_json
        FROM thesis_evaluations te
        JOIN (
            SELECT ticker, MAX(evaluated_at) AS m
            FROM thesis_evaluations
            WHERE ticker IN ({placeholders})
            GROUP BY ticker
        ) latest ON latest.ticker = te.ticker AND latest.m = te.evaluated_at
        """,
        [t.upper() for t in tickers],
    ).fetchall()
    out: dict[str, ThesisVerdict] = {}
    for ticker, evaluated_at, status, rules_json in rows:
        key = str(ticker).upper()
        out[key] = ThesisVerdict(
            ticker=key,
            status=str(status),
            evaluated_at=_opt_str(evaluated_at),
            rules=_parse_rules(rules_json),
        )
    return out


def _valuations(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, Valuation]:
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT d.ticker, d.valuation_date, d.npv_per_share, d.live_price,
               d.over_under_pct, d.mos_bar_used, d.currency
        FROM dcf_runs d
        JOIN (
            SELECT ticker, MAX(valuation_date) AS m
            FROM dcf_runs
            WHERE ticker IN ({placeholders})
            GROUP BY ticker
        ) latest ON latest.ticker = d.ticker AND latest.m = d.valuation_date
        """,
        [t.upper() for t in tickers],
    ).fetchall()
    out: dict[str, Valuation] = {}
    for ticker, vdate, fair, live, over_under, mos, currency in rows:
        key = str(ticker).upper()
        out[key] = Valuation(
            ticker=key,
            valuation_date=_opt_str(vdate),
            fair_value=_opt_float(fair),
            live_price=_opt_float(live),
            over_under_pct=_opt_float(over_under),
            mos_bar=_opt_float(mos),
            currency=_opt_str(currency),
        )
    return out


def _alerts(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, tuple[ThesisAlert, ...]]:
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT ticker, trigger_kind, fired_at, status, signature_sha
        FROM alerts
        WHERE status = 'pending' AND ticker IN ({placeholders})
        ORDER BY fired_at DESC
        """,
        [t.upper() for t in tickers],
    ).fetchall()
    grouped: dict[str, list[ThesisAlert]] = {}
    for ticker, kind, fired_at, status, sig in rows:
        key = str(ticker).upper()
        grouped.setdefault(key, []).append(
            ThesisAlert(
                ticker=key,
                trigger_kind=str(kind),
                fired_at=_opt_str(fired_at),
                status=str(status),
                signature_sha=_opt_str(sig),
            )
        )
    return {k: tuple(v) for k, v in grouped.items()}


def _parse_rules(rules_json: object) -> tuple[RuleEval, ...]:
    if not isinstance(rules_json, str) or not rules_json:
        return ()
    try:
        raw: object = json.loads(rules_json)
    except (json.JSONDecodeError, ValueError):
        return ()
    if not isinstance(raw, list):
        return ()
    rules: list[RuleEval] = []
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            continue
        d = cast(dict[str, object], item)
        rules.append(
            RuleEval(
                rule_id=_as_str(d.get("rule_id")),
                kpi_name=_as_str(d.get("kpi_name")),
                status=_as_str(d.get("status")),
                tier=_opt_str(d.get("tier")),
                narrative=_opt_str(d.get("narrative")),
            )
        )
    return tuple(rules)


def _as_str(v: object) -> str:
    return "" if v is None else str(v)


def _opt_str(v: object) -> str | None:
    return None if v is None else str(v)


def _opt_float(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None
