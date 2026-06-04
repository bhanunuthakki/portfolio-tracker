"""Tests for the accept -> trade execution reconciliation loop.

The pure heuristic (`_reconcile_one`) is exercised with no DB. The DB-level
`reconcile_commitments` and the manual-correction endpoint reuse the in-memory
`session` / `client` fixtures and monkeypatch `_value_by_ticker`, mirroring
`tests/test_cockpit_queue.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from portfolio_tracker.models import ActionQueueItem
from portfolio_tracker.services import cockpit

# ---------------------------------------------------------------------------
# Pure heuristic — `_reconcile_one` (no DB)
# ---------------------------------------------------------------------------


def test_trim_executed() -> None:
    # 12.5% -> 5% is a 60% relative cut, well past the 10% bar.
    res = cockpit._reconcile_one("trim", 12.5, 5.0, held=True, days_since_accept=1)
    assert res.status == "executed"


def test_trim_partial() -> None:
    # 12.5% -> 11.5% is an 8% relative cut: directional but under the bar.
    res = cockpit._reconcile_one("trim", 12.5, 11.5, held=True, days_since_accept=1)
    assert res.status == "partial"


def test_trim_pending_within_window() -> None:
    # No meaningful move yet, but still inside the staleness window.
    res = cockpit._reconcile_one("trim", 12.5, 12.5, held=True, days_since_accept=3)
    assert res.status == "pending"


def test_trim_not_executed_after_staleness() -> None:
    # No move, and past the 14-day window -> the commitment lapsed.
    res = cockpit._reconcile_one("trim", 12.5, 12.5, held=True, days_since_accept=20)
    assert res.status == "not_executed"


def test_trim_noise_below_partial_floor_is_pending() -> None:
    # A 1% relative drift (< 2% floor) is treated as noise, not a partial fill.
    res = cockpit._reconcile_one("trim", 12.5, 12.375, held=True, days_since_accept=2)
    assert res.status == "pending"


def test_add_executed() -> None:
    # 5% -> 6% is a 20% relative add.
    res = cockpit._reconcile_one("add", 5.0, 6.0, held=True, days_since_accept=1)
    assert res.status == "executed"


def test_add_partial() -> None:
    # 5% -> 5.2% is a 4% relative add: directional but under the bar.
    res = cockpit._reconcile_one("add", 5.0, 5.2, held=True, days_since_accept=1)
    assert res.status == "partial"


def test_add_wrong_direction_is_pending() -> None:
    # Accepted "add" but the weight fell — no progress toward the commitment.
    res = cockpit._reconcile_one("add", 5.0, 4.0, held=True, days_since_accept=2)
    assert res.status == "pending"


def test_exit_executed_when_no_longer_held() -> None:
    res = cockpit._reconcile_one("exit", 8.0, 0.0, held=False, days_since_accept=1)
    assert res.status == "executed"


def test_exit_partial_when_trimmed_but_still_held() -> None:
    # 8% -> 4% is a real reduction toward the exit, but still held.
    res = cockpit._reconcile_one("exit", 8.0, 4.0, held=True, days_since_accept=1)
    assert res.status == "partial"


def test_exit_pending_then_not_executed() -> None:
    assert cockpit._reconcile_one("exit", 8.0, 8.0, held=True, days_since_accept=3).status == "pending"
    assert (
        cockpit._reconcile_one("exit", 8.0, 8.0, held=True, days_since_accept=20).status
        == "not_executed"
    )


@pytest.mark.parametrize("action", ["hold", "review", "audit", "research"])
def test_non_trade_actions_are_na(action: str) -> None:
    res = cockpit._reconcile_one(action, 10.0, 10.0, held=True, days_since_accept=99)
    assert res.status == "n/a"


def test_trim_of_vanished_position_counts_as_executed() -> None:
    # Accepted a trim but exited entirely: the reduction goal was met.
    res = cockpit._reconcile_one("trim", 6.0, 0.0, held=False, days_since_accept=1)
    assert res.status == "executed"


# ---------------------------------------------------------------------------
# DB-level — `reconcile_commitments`
# ---------------------------------------------------------------------------


def _seed_accepted(
    session: Session,
    *,
    ticker: str = "NU",
    action: str = "trim",
    weight: float = 12.5,
    accepted_days_ago: int = 1,
) -> ActionQueueItem:
    """Insert an `accepted` queue row with a real commitment payload."""
    accepted_at = datetime.now(UTC) - timedelta(days=accepted_days_ago)
    row = ActionQueueItem(
        signature=f"sig-{ticker}-{action}",
        ticker=ticker,
        name=f"{ticker} Inc",
        tier="act",
        action=action,
        rank=1,
        headline=f"{action} {ticker}",
        rationale="because numbers.",
        suggested_action="do the thing.",
        weight_pct=Decimal(str(weight)),
        llm_ranked=False,
        status="accepted",
        accepted_at=accepted_at,
        commitment_json=json.dumps(
            {"action": action, "ticker": ticker, "weight_pct_at_accept": weight}
        ),
    )
    session.add(row)
    session.commit()
    return row


def _patch_values(monkeypatch: pytest.MonkeyPatch, weights: dict[str, float]) -> None:
    """Stub `_value_by_ticker` so per-ticker book weight = its share of the map."""
    vals = {t.upper(): Decimal(str(w)) for t, w in weights.items()}
    names: dict[str, str | None] = {t: f"{t} Inc" for t in vals}
    monkeypatch.setattr(cockpit, "_value_by_ticker", lambda _s: (vals, names))


def test_reconcile_trim_executed(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    row = _seed_accepted(session, action="trim", weight=12.5)
    _patch_values(monkeypatch, {"NU": 5.0, "REST": 95.0})  # NU now 5% of book
    examined = cockpit.reconcile_commitments(session)
    assert examined == 1
    assert row.execution_status == "executed"
    assert row.executed_at is not None
    assert row.executed_weight_pct is not None and float(row.executed_weight_pct) == pytest.approx(5.0)


def test_reconcile_staleness_marks_not_executed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _seed_accepted(session, action="trim", weight=12.5, accepted_days_ago=20)
    _patch_values(monkeypatch, {"NU": 12.5, "REST": 87.5})  # no move
    cockpit.reconcile_commitments(session)
    assert row.execution_status == "not_executed"
    assert row.executed_at is None


def test_reconcile_exit_executed_when_absent(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _seed_accepted(session, ticker="WIX", action="exit", weight=4.0)
    _patch_values(monkeypatch, {"NU": 100.0})  # WIX no longer in the book
    cockpit.reconcile_commitments(session)
    assert row.execution_status == "executed"


def test_reconcile_hold_is_na(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    row = _seed_accepted(session, action="hold", weight=20.0)
    _patch_values(monkeypatch, {"NU": 20.0, "REST": 80.0})
    cockpit.reconcile_commitments(session)
    assert row.execution_status == "n/a"


def test_reconcile_skips_when_no_holdings(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _seed_accepted(session, action="trim", weight=12.5)
    _patch_values(monkeypatch, {})  # empty book -> can't reconcile
    assert cockpit.reconcile_commitments(session) == 0
    assert row.execution_status == "pending"  # untouched


def test_reconcile_skips_overridden_rows(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _seed_accepted(session, action="trim", weight=12.5)
    cockpit.set_execution_status(session, row.id, "not_executed")  # owner's manual call
    assert row.execution_overridden is True
    _patch_values(monkeypatch, {"NU": 5.0, "REST": 95.0})  # heuristic would say executed
    examined = cockpit.reconcile_commitments(session)
    assert examined == 0  # the overridden row is skipped
    assert row.execution_status == "not_executed"  # not clobbered


def test_set_execution_pending_rearms_auto_match(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _seed_accepted(session, action="trim", weight=12.5)
    cockpit.set_execution_status(session, row.id, "executed")
    assert row.execution_overridden is True
    cockpit.set_execution_status(session, row.id, "pending")  # hand back to the matcher
    assert row.execution_overridden is False
    _patch_values(monkeypatch, {"NU": 5.0, "REST": 95.0})
    cockpit.reconcile_commitments(session)
    assert row.execution_status == "executed"  # re-evaluated automatically


def test_set_execution_unknown_id_raises(session: Session) -> None:
    with pytest.raises(ValueError):
        cockpit.set_execution_status(session, 9999, "executed")


def test_set_execution_invalid_status_raises(session: Session) -> None:
    row = _seed_accepted(session)
    with pytest.raises(ValueError):
        cockpit.set_execution_status(session, row.id, "bogus")


# ---------------------------------------------------------------------------
# Endpoint — POST /api/cockpit/items/{id}/execution
# ---------------------------------------------------------------------------


def test_execution_endpoint_corrects(client, session: Session) -> None:
    row = _seed_accepted(session, action="trim", weight=12.5)
    resp = client.post(f"/api/cockpit/items/{row.id}/execution?status=not_executed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["execution_status"] == "not_executed"
    assert body["execution_overridden"] is True


def test_execution_endpoint_unknown_id_404(client) -> None:
    assert client.post("/api/cockpit/items/999/execution?status=executed").status_code == 404


def test_execution_endpoint_bad_status_400(client, session: Session) -> None:
    row = _seed_accepted(session)
    assert client.post(f"/api/cockpit/items/{row.id}/execution?status=bogus").status_code == 400
