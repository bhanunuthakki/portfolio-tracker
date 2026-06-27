"""Unit tests for the pure drawdown / Calmar math."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio_tracker.services.drawdown import drawdown_from_index

_START = date(2025, 1, 1)
_END = date(2025, 1, 5)


def _pts(*equities: str) -> list[tuple[date, Decimal]]:
    return [(date(2025, 1, i + 1), Decimal(e)) for i, e in enumerate(equities)]


def test_drawdown_peak_trough_recovery():
    # 1.00 -> 1.20 (peak) -> 0.90 (-25% DD) -> 1.10 (still under) -> 1.30 (recovered).
    res = drawdown_from_index(_START, _END, _pts("1.00", "1.20", "0.90", "1.10", "1.30"))
    assert res.max_drawdown_pct == Decimal("-25.00")
    assert res.peak_date == date(2025, 1, 2)
    assert res.trough_date == date(2025, 1, 3)
    assert res.recovery_date == date(2025, 1, 5)
    assert res.days_to_recovery == 2
    assert res.current_drawdown_pct == Decimal("0.00")  # ends at a new peak
    assert res.annualized_return_pct is not None
    assert res.calmar is not None and res.calmar > 0


def test_drawdown_still_underwater_has_no_recovery():
    res = drawdown_from_index(_START, _END, _pts("1.00", "1.20", "0.90"))
    assert res.max_drawdown_pct == Decimal("-25.00")
    assert res.recovery_date is None
    assert res.days_to_recovery is None
    assert res.current_drawdown_pct == Decimal("-25.00")  # still at the trough


def test_drawdown_monotonic_rise_has_zero_drawdown():
    res = drawdown_from_index(_START, _END, _pts("1.00", "1.05", "1.10"))
    assert res.max_drawdown_pct == Decimal("0.00")
    assert res.calmar is None  # no drawdown -> Calmar undefined


def test_drawdown_too_few_points_is_empty():
    res = drawdown_from_index(_START, _END, _pts("1.00"))
    assert res.max_drawdown_pct is None
    assert res.underwater == []
