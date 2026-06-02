"""Tests for the cockpit action-queue persistence + lifecycle (P3 slice 3)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from portfolio_tracker.services import cockpit


def _ai(ticker: str, tier: str, kinds: list[str], rank: int = 1) -> cockpit.ActionItem:
    return cockpit.ActionItem(
        rank=rank,
        ticker=ticker,
        name=f"{ticker} Inc",
        tier=tier,
        action="audit",
        headline=f"{ticker} action",
        rationale="because numbers.",
        suggested_action="do the thing.",
        weight_pct=12.5,
        evidence={"k": "v"},
        signal_kinds=kinds,
    )


def _patch_ranked(monkeypatch: pytest.MonkeyPatch, items: list[cockpit.ActionItem]) -> None:
    monkeypatch.setattr(cockpit, "gather_signals", lambda _s: [])
    monkeypatch.setattr(cockpit, "rank_signals", lambda _sigs, **_k: items)


def test_refresh_inserts_and_lists(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ranked(
        monkeypatch, [_ai("NU", "act", ["thesis_verdict"]), _ai("META", "watch", ["valuation"])]
    )
    out = cockpit.refresh_queue(session, use_llm=False)
    assert {i.ticker for i in out} == {"NU", "META"}
    assert out[0].ticker == "NU"  # act sorts before watch
    assert out[0].status == "open"


def test_dismiss_persists_across_refresh(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ranked(
        monkeypatch, [_ai("NU", "act", ["thesis_verdict"]), _ai("META", "watch", ["valuation"])]
    )
    cockpit.refresh_queue(session, use_llm=False)
    nu = next(i for i in cockpit.list_queue(session) if i.ticker == "NU")
    cockpit.dismiss_item(session, nu.id)
    cockpit.refresh_queue(session, use_llm=False)  # same items re-ranked
    active = {i.ticker for i in cockpit.list_queue(session)}
    assert "NU" not in active  # dismissal sticks
    assert "META" in active


def test_accept_records_commitment(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ranked(monkeypatch, [_ai("NU", "act", ["thesis_verdict"])])
    cockpit.refresh_queue(session, use_llm=False)
    nu = cockpit.list_queue(session)[0]
    updated = cockpit.accept_item(session, nu.id)
    assert updated.status == "accepted" and updated.accepted_at is not None
    assert "NU" in {i.ticker for i in cockpit.list_queue(session)}  # accepted stays visible


def test_snooze_hides_until_due(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ranked(monkeypatch, [_ai("NU", "act", ["thesis_verdict"])])
    cockpit.refresh_queue(session, use_llm=False)
    nu = cockpit.list_queue(session)[0]
    cockpit.snooze_item(session, nu.id, days=7)
    assert cockpit.list_queue(session) == []  # hidden while snoozed
    row = cockpit._require_item(session, nu.id)
    row.snooze_until = date.today() - timedelta(days=1)
    session.commit()
    cockpit.refresh_queue(session, use_llm=False)  # snooze elapsed -> reopens
    reopened = cockpit.list_queue(session)
    assert [i.ticker for i in reopened] == ["NU"] and reopened[0].status == "open"


def test_vanished_signal_resolves(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ranked(
        monkeypatch, [_ai("NU", "act", ["thesis_verdict"]), _ai("META", "watch", ["valuation"])]
    )
    cockpit.refresh_queue(session, use_llm=False)
    _patch_ranked(monkeypatch, [_ai("NU", "act", ["thesis_verdict"])])  # META's signal gone
    out = cockpit.refresh_queue(session, use_llm=False)
    assert {i.ticker for i in out} == {"NU"}  # META resolved -> hidden


def test_stable_signature_ignores_order_and_case() -> None:
    a = _ai("nu", "act", ["valuation", "thesis_verdict"])
    b = _ai("NU", "watch", ["thesis_verdict", "valuation"])
    assert cockpit._signature(a) == cockpit._signature(b)


def test_queue_endpoints(client) -> None:
    assert client.post("/api/cockpit/queue/refresh?use_llm=false").json() == []
    assert client.get("/api/cockpit/queue").json() == []
    assert client.post("/api/cockpit/items/999/accept").status_code == 404
