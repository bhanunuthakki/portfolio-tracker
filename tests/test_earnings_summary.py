"""Tests for the deepened earnings-summary connector (verdict / valuation / alerts).

Builds a throwaway SQLite DB shaped like the companion earnings-summary project
and points the connector at it via monkeypatch — no real companion project,
network, or LLM required.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from portfolio_tracker.services import earnings_summary as es

# NU: an older 'ok' eval and a newer 'breach' eval — the latest must win.
_NU_RULES = (
    '[{"rule_id":"r1","kpi_name":"Revenue YoY","status":"breach","tier":"universal",'
    '"narrative":"Revenue decelerated."},'
    '{"rule_id":"r2","kpi_name":"ROE","status":"ok","tier":"universal","narrative":"Fine."}]'
)


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT);
        CREATE TABLE thesis_state (ticker TEXT, thesis TEXT, breach_status TEXT);
        CREATE TABLE thesis_evaluations (
            ticker TEXT, evaluated_at TEXT, overall_status TEXT, rule_evaluations_json TEXT
        );
        CREATE TABLE dcf_runs (
            ticker TEXT, valuation_date TEXT, npv_per_share REAL, live_price REAL,
            over_under_pct REAL, mos_bar_used REAL, currency TEXT
        );
        CREATE TABLE alerts (
            ticker TEXT, trigger_kind TEXT, fired_at TEXT, status TEXT, signature_sha TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO tracked_companies (ticker, list_type, archived_at) VALUES (?,?,?)",
        [
            ("NU", "portfolio", None),
            ("MELI", "portfolio", None),
            ("OLD", "portfolio", "2025-01-01"),
        ],
    )
    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, breach_status) VALUES (?,?,?)",
        ("NU", "Consolidated growth + ROE durability.", "breach"),
    )
    conn.executemany(
        "INSERT INTO thesis_evaluations "
        "(ticker, evaluated_at, overall_status, rule_evaluations_json) VALUES (?,?,?,?)",
        [
            ("NU", "2026-05-01 00:00:00", "ok", "[]"),
            ("NU", "2026-06-01 09:40:38.154915", "breach", _NU_RULES),
            ("MELI", "2026-05-20 00:00:00", "ok", "[]"),
        ],
    )
    # NU newer DCF: live 26 vs fair 20 => +30% over fair, MOS 20% => 'rich'.
    conn.executemany(
        "INSERT INTO dcf_runs "
        "(ticker, valuation_date, npv_per_share, live_price, over_under_pct, mos_bar_used, currency) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            ("NU", "2026-04-01", 18.0, 15.0, -0.1667, 0.2, "USD"),
            ("NU", "2026-05-26", 20.0, 26.0, 0.30, 0.2, "USD"),
        ],
    )
    conn.executemany(
        "INSERT INTO alerts (ticker, trigger_kind, fired_at, status, signature_sha) VALUES (?,?,?,?,?)",
        [
            ("NU", "earnings_tone", "2026-06-01T17:14:02", "pending", "sha-aaa"),
            ("NU", "thesis_drift", "2026-05-15T10:00:00", "dismissed", "sha-bbb"),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def es_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "es.db"
    _make_db(db)
    monkeypatch.setattr(es, "_db_path", lambda: db)
    return db


def _make_db_with_thesis_status_view(path: Path) -> None:
    """A companion DB that also carries the v_thesis_status view (alembic 0143).
    Modeled as a plain table with the view's columns — the reader issues the
    same SELECT regardless of whether it's a view or a table."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE v_thesis_status (
            ticker TEXT, has_written_thesis INTEGER, breach_status TEXT,
            thesis_updated_at TEXT, ledger_entry_count INTEGER, last_ledger_at TEXT,
            open_notes_count INTEGER, open_questions_count INTEGER
        )
        """
    )
    conn.executemany(
        "INSERT INTO v_thesis_status (ticker, has_written_thesis, breach_status, "
        "thesis_updated_at, ledger_entry_count, last_ledger_at, open_notes_count, "
        "open_questions_count) VALUES (?,?,?,?,?,?,?,?)",
        [
            # NU: thesis + deep ledger + open threads (the "decision log NOT empty" case).
            ("NU", 1, "ok", "2026-06-01", 17, "2026-06-01T18:18:24", 3, 1),
            # WIX: thesis exists but ZERO ledger (research done, no formal decision).
            ("WIX", 1, "warn", "2026-05-20", 0, None, 0, 0),
            # FLKR: present in substrate but NO written thesis — the genuine gap.
            ("FLKR", 0, None, None, 0, None, 0, 0),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def es_db_with_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "es_view.db"
    _make_db_with_thesis_status_view(db)
    monkeypatch.setattr(es, "_db_path", lambda: db)
    return db


def test_latest_verdict_uses_most_recent_eval(es_db: Path) -> None:
    verdicts = es.latest_verdicts(["NU", "MELI"])
    assert verdicts["NU"].status == "breach"
    assert verdicts["MELI"].status == "ok"
    flagged = verdicts["NU"].flagged_rules
    assert [r.rule_id for r in flagged] == ["r1"]  # only the breach rule, ok dropped
    assert flagged[0].kpi_name == "Revenue YoY"


def test_latest_valuation_and_signal(es_db: Path) -> None:
    v = es.latest_valuations(["NU"])["NU"]
    assert v.valuation_date == "2026-05-26"
    assert v.fair_value == 20.0
    assert v.live_price == 26.0
    assert v.over_under_pct == pytest.approx(0.30)
    assert v.signal == "rich"


def test_valuation_signal_branches() -> None:
    cheap = es.Valuation("X", "2026-01-01", 100.0, 70.0, -0.30, 0.2, "USD")
    fair = es.Valuation("Y", "2026-01-01", 100.0, 105.0, 0.05, 0.2, "USD")
    unknown = es.Valuation("Z", None, None, None, None, None, None)
    assert cheap.signal == "cheap"
    assert fair.signal == "fair"
    assert unknown.signal == "unknown"


def test_pending_alerts_excludes_dismissed(es_db: Path) -> None:
    nu = es.pending_alerts(["NU"])["NU"]
    assert len(nu) == 1
    assert nu[0].trigger_kind == "earnings_tone"
    assert nu[0].signature_sha == "sha-aaa"


def test_untracked_holdings_flags_blind_spots(es_db: Path) -> None:
    # NU + MELI are tracked portfolio; OLD is archived; ZZZ is absent entirely.
    assert es.untracked_holdings(["NU", "ZZZ", "OLD"]) == ["ZZZ", "OLD"]


def test_thesis_detail_assembles_everything(es_db: Path) -> None:
    detail = es.thesis_detail("NU")
    assert detail is not None
    assert detail.tracked is True
    assert detail.list_type == "portfolio"
    assert detail.verdict is not None and detail.verdict.status == "breach"
    assert detail.valuation is not None and detail.valuation.signal == "rich"
    assert len(detail.alerts) == 1
    assert detail.thesis_summary is not None and "ROE" in detail.thesis_summary


def test_thesis_status_reads_view(es_db_with_view: Path) -> None:
    out = es.thesis_status_by_ticker(["NU", "WIX", "FLKR", "ZZZ"])

    nu = out["NU"]
    assert nu.has_written_thesis is True
    assert nu.breach_status == "ok"
    assert nu.ledger_entry_count == 17
    assert nu.last_ledger_at == "2026-06-01T18:18:24"
    assert nu.open_notes_count == 3
    assert nu.open_questions_count == 1

    # Thesis exists but zero ledger — must still read has_written_thesis True.
    assert out["WIX"].has_written_thesis is True
    assert out["WIX"].ledger_entry_count == 0

    # Present in the substrate but no thesis — the genuine documentation gap.
    assert out["FLKR"].has_written_thesis is False

    assert "ZZZ" not in out  # no footprint → absent


def test_thesis_status_degrades_when_view_missing(es_db: Path) -> None:
    # The base es_db fixture has no v_thesis_status view (older companion build).
    assert es.thesis_status_by_ticker(["NU"]) == {}


def test_graceful_when_db_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(es, "_db_path", lambda: tmp_path / "nope.db")
    assert es.latest_verdicts(["NU"]) == {}
    assert es.latest_valuations(["NU"]) == {}
    assert es.pending_alerts(["NU"]) == {}
    assert es.untracked_holdings(["NU"]) == []
    assert es.thesis_detail("NU") is None
    assert es.thesis_status_by_ticker(["NU"]) == {}
