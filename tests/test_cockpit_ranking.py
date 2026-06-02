"""Tests for the cockpit Opus ranking layer (P3 slice 2).

The LLM is injected, so no real Opus call / subprocess runs.
"""

from __future__ import annotations

from portfolio_tracker.services import cockpit


def _sig(ticker: str, kind: str, severity: str, weight: float, **ev: str) -> cockpit.Signal:
    return cockpit.Signal(
        ticker=ticker,
        name=f"{ticker} Inc",
        kind=kind,
        severity=severity,
        weight_pct=weight,
        headline=f"{kind} {ticker}",
        detail="detail.",
        source="x",
        evidence=dict(ev),
    )


_NU = _sig("NU", "thesis_verdict", "high", 18.0, status="breach")
_META = _sig("META", "valuation", "warning", 82.0, over_under_pct="30%")


def test_rank_orders_act_before_watch_and_grounds_evidence() -> None:
    # LLM returns META first, but NU is 'act' tier and must sort to the top.
    raw = (
        '{"items": ['
        '{"ticker": "META", "tier": "watch", "action": "trim", "headline": "Trim META",'
        ' "rationale": "82% of book, 30% over fair.", "suggested_action": "Trim toward target."},'
        '{"ticker": "NU", "tier": "act", "action": "audit", "headline": "Audit NU",'
        ' "rationale": "Thesis breached.", "suggested_action": "Re-audit the thesis."}]}'
    )
    items = cockpit.rank_signals([_NU, _META], llm=lambda _p: raw)
    assert [i.ticker for i in items] == ["NU", "META"]
    assert items[0].tier == "act" and items[0].rank == 1 and items[0].action == "audit"
    assert items[0].evidence.get("status") == "breach"  # merged from the signal
    assert items[1].ticker == "META" and items[1].llm_ranked is True


def test_hallucinated_ticker_is_dropped() -> None:
    raw = (
        '{"items": ['
        '{"ticker": "ZZZ", "tier": "act", "action": "exit", "headline": "x", "rationale": "y",'
        ' "suggested_action": "z"},'
        '{"ticker": "NU", "tier": "act", "action": "audit", "headline": "a", "rationale": "b",'
        ' "suggested_action": "c"}]}'
    )
    items = cockpit.rank_signals([_NU], llm=lambda _p: raw)
    assert [i.ticker for i in items] == ["NU"]  # ZZZ has no grounding signal


def test_dropped_high_severity_signal_is_reinjected() -> None:
    # LLM omits NU (a breach/high signal). It must come back, deterministically.
    raw = (
        '{"items": [{"ticker": "META", "tier": "watch", "action": "trim", "headline": "x",'
        ' "rationale": "y", "suggested_action": "z"}]}'
    )
    items = cockpit.rank_signals([_NU, _META], llm=lambda _p: raw)
    assert {i.ticker for i in items} == {"NU", "META"}
    nu = next(i for i in items if i.ticker == "NU")
    assert nu.llm_ranked is False and nu.tier == "act"


def test_unparseable_llm_output_falls_back_to_deterministic() -> None:
    items = cockpit.rank_signals([_NU, _META], llm=lambda _p: "Sorry, I can't help with that.")
    assert len(items) == 2
    assert all(i.llm_ranked is False for i in items)
    assert items[0].tier == "act"  # NU (high) sorts first in the fallback


def test_llm_exception_falls_back() -> None:
    def boom(_p: str) -> str:
        raise RuntimeError("llm down")

    items = cockpit.rank_signals([_NU, _META], llm=boom)
    assert len(items) == 2 and all(not i.llm_ranked for i in items)


def test_use_llm_false_skips_llm() -> None:
    called = {"n": 0}

    def spy(_p: str) -> str:
        called["n"] += 1
        return "{}"

    items = cockpit.rank_signals([_NU, _META], llm=spy, use_llm=False)
    assert called["n"] == 0
    assert len(items) == 2


def test_empty_signals() -> None:
    assert cockpit.rank_signals([], llm=lambda _p: "{}") == []


def test_queue_endpoint_empty(client) -> None:
    resp = client.get("/api/cockpit/queue?use_llm=false")
    assert resp.status_code == 200
    assert resp.json() == []
