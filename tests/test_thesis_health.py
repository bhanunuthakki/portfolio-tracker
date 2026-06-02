"""Tests for the Thesis Health board (P4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from portfolio_tracker.services import cockpit
from portfolio_tracker.services import earnings_summary as es


def _summary(
    ticker: str,
    *,
    tracked: bool,
    list_type: str | None = None,
    has_brief: bool = False,
    brief: str | None = None,
) -> es.TickerSummary:
    return es.TickerSummary(
        ticker=ticker,
        tracked=tracked,
        list_type=list_type,
        next_earnings_date=None,
        thesis_status=None,
        thesis_summary=None,
        latest_brief_iso_date=brief,
        has_brief=has_brief,
    )


def test_thesis_health_assembles_weights_and_sorts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cockpit,
        "_value_by_ticker",
        lambda _s: (
            {"NU": Decimal(18000), "META": Decimal(82000), "ZZZ": Decimal(5000)},
            {"NU": "Nu", "META": "Meta", "ZZZ": "ZZ Corp"},
        ),
    )
    monkeypatch.setattr(
        es,
        "summary_by_ticker",
        lambda _t: {
            "NU": _summary("NU", tracked=True, list_type="portfolio", has_brief=True, brief="2026-05-13"),
            "META": _summary("META", tracked=True, list_type="portfolio"),
            # ZZZ absent -> blind spot
        },
    )
    monkeypatch.setattr(
        es,
        "latest_verdicts",
        lambda _t: {
            "NU": es.ThesisVerdict(
                "NU", "breach", "2026-06-01", (es.RuleEval("r1", "Revenue YoY", "breach", "u", "n"),)
            ),
            "META": es.ThesisVerdict("META", "ok", "x", ()),
        },
    )
    monkeypatch.setattr(
        es,
        "latest_valuations",
        lambda _t: {"META": es.Valuation("META", "2026-05-26", 600.0, 780.0, 0.30, 0.2, "USD")},
    )
    monkeypatch.setattr(es, "pending_alerts", lambda _t: {})

    rows = cockpit.thesis_health(session=None)  # session only flows to the patched _value_by_ticker
    by = {r.ticker: r for r in rows}
    assert by["NU"].verdict_status == "breach"
    assert "Revenue YoY" in by["NU"].flagged_rules
    assert by["NU"].has_brief and by["NU"].brief_iso_date == "2026-05-13"
    assert round(by["NU"].weight_pct) == 17  # 18000 / 105000
    assert by["META"].valuation_signal == "rich" and by["META"].verdict_status == "ok"
    assert by["ZZZ"].tracked is False  # held but no coverage -> blind spot
    # NU (breach) is the most urgent -> sorts first, ahead of the 82%-weight META.
    assert rows[0].ticker == "NU"


def test_thesis_health_endpoint_empty(client) -> None:
    resp = client.get("/api/cockpit/thesis-health")
    assert resp.status_code == 200
    assert resp.json() == []
