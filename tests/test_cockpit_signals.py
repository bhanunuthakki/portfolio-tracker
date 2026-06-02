"""Tests for the cockpit signal aggregator (P3 slice 1)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from portfolio_tracker.services import cockpit
from portfolio_tracker.services import earnings_summary as es
from portfolio_tracker.services.coaching import CoachingTip


def _tip(category: str, severity: str, ticker: str, **ctx: str) -> CoachingTip:
    return CoachingTip(
        category=category,
        severity=severity,
        ticker=ticker,
        name=f"{ticker} Inc",
        headline=f"{ticker} {category}",
        detail="detail.",
        suggested_action="do something.",
        context=dict(ctx),
    )


def test_build_signals_merges_and_weights() -> None:
    signals = cockpit.build_signals(
        value_by_ticker={"NU": Decimal(18000), "META": Decimal(82000)},
        total_value=Decimal(100000),
        names_by_ticker={"NU": "Nu Holdings", "META": "Meta"},
        coaching_tips=[_tip("drawdown_audit", "high", "NU", drawdown_pct="30")],
        verdicts={
            "NU": es.ThesisVerdict(
                ticker="NU",
                status="breach",
                evaluated_at="2026-06-01",
                rules=(es.RuleEval("r1", "Revenue YoY", "breach", "universal", "n"),),
            ),
            "META": es.ThesisVerdict(ticker="META", status="ok", evaluated_at="x", rules=()),
        },
        valuations={"META": es.Valuation("META", "2026-05-26", 600.0, 780.0, 0.30, 0.2, "USD")},
        alerts={"NU": (es.ThesisAlert("NU", "earnings_tone", "2026-06-01", "pending", "sha"),)},
        untracked=["XYZ"],
    )
    by_kind = {s.kind: s for s in signals}
    # 'ok' verdict (META) yields no thesis signal; breach (NU) does, weighted 18%.
    assert by_kind["thesis_verdict"].ticker == "NU"
    assert by_kind["thesis_verdict"].severity == "high"
    assert round(by_kind["thesis_verdict"].weight_pct) == 18
    # Valuation 'rich' for META, weighted by its 82% exposure.
    assert by_kind["valuation"].severity == "warning"
    assert round(by_kind["valuation"].weight_pct) == 82
    assert "above" in by_kind["valuation"].detail
    # Alert, coaching, and coverage-gap signals all present.
    assert "es_alert:earnings_tone" in by_kind
    assert "coaching:drawdown_audit" in by_kind
    assert by_kind["coverage_gap"].ticker == "XYZ"
    # High severity sorts to the top.
    assert signals[0].severity == "high"


def test_build_signals_skips_ok_and_fair() -> None:
    signals = cockpit.build_signals(
        value_by_ticker={"NU": Decimal(100)},
        total_value=Decimal(100),
        names_by_ticker={"NU": "Nu"},
        coaching_tips=[],
        verdicts={"NU": es.ThesisVerdict("NU", "ok", "x", ())},
        valuations={"NU": es.Valuation("NU", "x", 100.0, 101.0, 0.01, 0.2, "USD")},
        alerts={},
        untracked=[],
    )
    assert signals == []


def test_weight_zero_when_no_total() -> None:
    signals = cockpit.build_signals(
        value_by_ticker={},
        total_value=Decimal(0),
        names_by_ticker={},
        coaching_tips=[_tip("irr_below_bar", "warning", "AAA")],
        verdicts={},
        valuations={},
        alerts={},
        untracked=[],
    )
    assert len(signals) == 1
    assert signals[0].weight_pct == 0.0


def test_gather_signals_wires_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cockpit,
        "_value_by_ticker",
        lambda _s: ({"NU": Decimal(50), "META": Decimal(50)}, {"NU": "Nu", "META": "Meta"}),
    )
    monkeypatch.setattr(cockpit, "generate_coaching_tips", lambda _s: SimpleNamespace(tips=[]))
    monkeypatch.setattr(es, "latest_verdicts", lambda _t: {"NU": es.ThesisVerdict("NU", "breach", "x", ())})
    monkeypatch.setattr(es, "latest_valuations", lambda _t: {})
    monkeypatch.setattr(es, "pending_alerts", lambda _t: {})
    monkeypatch.setattr(es, "untracked_holdings", lambda _t: [])
    signals = cockpit.gather_signals(session=None)
    assert any(s.kind == "thesis_verdict" and s.ticker == "NU" for s in signals)


def test_signals_endpoint_empty(client) -> None:
    resp = client.get("/api/cockpit/signals")
    assert resp.status_code == 200
    assert resp.json() == []
