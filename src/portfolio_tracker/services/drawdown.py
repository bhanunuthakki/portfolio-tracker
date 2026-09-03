"""Loss-shaped risk: max drawdown, underwater curve, recovery, Calmar.

A concentrated single-name book experiences risk as drawdown depth and
time-to-recover, not as annualized sigma — yet the risk panel only had
dispersion metrics. This computes the peak-to-trough story over the same
cashflow-neutral return index `/performance` already produces (so the
companion earnings-summary project can render it instead of re-deriving the
curve locally — the tracker owns the risk math).

The equity index is `1 + portfolio_return_pct/100` per day (Modified-Dietz
cumulative return, which neutralizes contributions/withdrawals). Drawdown on
each day is `(equity − running_peak) / running_peak`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from portfolio_tracker.services.performance import compute_performance_series

_DAYS_PER_YEAR = Decimal("365.25")


class UnderwaterPoint(BaseModel):
    date: date
    drawdown_pct: Decimal  # 0 at/above the running peak, negative below


class DrawdownResult(BaseModel):
    methodology: Literal["risk.beta_drawdown"]
    methodology_version: Literal["2"]
    calculation_status: Literal["available", "unavailable"]
    calculation_reason_codes: list[str]
    start_date: date
    end_date: date
    max_drawdown_pct: Decimal | None  # most negative point on the curve
    peak_date: date | None  # the prior peak the max drawdown fell from
    trough_date: date | None
    recovery_date: date | None  # first day back to the prior peak (None: still underwater)
    days_to_recovery: int | None
    current_drawdown_pct: Decimal | None  # where the book sits now vs its all-time peak
    annualized_return_pct: Decimal | None
    calmar: Decimal | None  # annualized_return / |max_drawdown|
    underwater: list[UnderwaterPoint] = []


def _empty(
    start_date: date,
    end_date: date,
    *,
    reason_codes: list[str],
) -> DrawdownResult:
    return DrawdownResult(
        methodology="risk.beta_drawdown",
        methodology_version="2",
        calculation_status="unavailable",
        calculation_reason_codes=sorted(set(reason_codes)),
        start_date=start_date,
        end_date=end_date,
        max_drawdown_pct=None,
        peak_date=None,
        trough_date=None,
        recovery_date=None,
        days_to_recovery=None,
        current_drawdown_pct=None,
        annualized_return_pct=None,
        calmar=None,
    )


def compute_drawdown(
    session: Session,
    start_date: date,
    end_date: date,
    reserve_amount: Decimal = Decimal(0),
    exclude_index_etfs: bool = False,
) -> DrawdownResult:
    """Drawdown metrics over the /performance cashflow-neutral return index."""
    series = compute_performance_series(
        session, start_date, end_date, reserve_amount, exclude_index_etfs
    )
    if series.calculation_status == "unavailable":
        return _empty(
            start_date,
            end_date,
            reason_codes=series.calculation_reason_codes,
        )
    points = [
        (p.date, Decimal(1) + p.portfolio_return_pct / Decimal(100))
        for p in series.points
        if p.portfolio_return_pct is not None
    ]
    return drawdown_from_index(start_date, end_date, points)


def drawdown_from_index(
    start_date: date, end_date: date, points: list[tuple[date, Decimal]]
) -> DrawdownResult:
    """Pure max-drawdown / underwater / recovery / Calmar over an equity index.

    `points` is `(date, equity_multiple)` ascending, equity rebased to 1.0 at
    the window start. Split out from the DB layer so the loss-shaped math is
    directly testable on a hand-built curve.
    """
    if len(points) < 2:
        return _empty(
            start_date,
            end_date,
            reason_codes=["insufficient_return_observations"],
        )

    underwater: list[UnderwaterPoint] = []
    peak_value = points[0][1]
    peak_date = points[0][0]
    max_dd = Decimal(0)
    max_dd_peak_date = peak_date
    max_dd_trough_date = points[0][0]
    # Track the peak that PRECEDED the eventual max-drawdown trough so we can
    # find when (if ever) the book recovered to it.
    running_peak_date_for_trough = peak_date

    for d, eq in points:
        if eq > peak_value:
            peak_value = eq
            peak_date = d
        dd = ((eq - peak_value) / peak_value) if peak_value > 0 else Decimal(0)
        underwater.append(
            UnderwaterPoint(date=d, drawdown_pct=(dd * Decimal(100)).quantize(Decimal("0.01")))
        )
        if dd < max_dd:
            max_dd = dd
            max_dd_trough_date = d
            running_peak_date_for_trough = peak_date
            max_dd_peak_date = peak_date

    # Recovery: the peak equity at the max-drawdown trough, and the first day
    # after the trough that climbs back to it.
    trough_peak_value: Decimal | None = None
    for d, eq in points:
        if d == running_peak_date_for_trough:
            trough_peak_value = eq
            break
    recovery_date: date | None = None
    if trough_peak_value is not None:
        for d, eq in points:
            if d > max_dd_trough_date and eq >= trough_peak_value:
                recovery_date = d
                break
    days_to_recovery = (recovery_date - max_dd_trough_date).days if recovery_date else None

    current_dd = underwater[-1].drawdown_pct if underwater else None

    # Annualized return + Calmar.
    total_return = points[-1][1] - Decimal(1)
    span_days = (points[-1][0] - points[0][0]).days
    annualized: Decimal | None = None
    if span_days > 0:
        years = Decimal(span_days) / _DAYS_PER_YEAR
        base = Decimal(1) + total_return
        if base > 0 and years > 0:
            annualized = (base ** (Decimal(1) / years) - Decimal(1)) * Decimal(100)
    calmar: Decimal | None = None
    if annualized is not None and max_dd < 0:
        calmar = (annualized / (abs(max_dd) * Decimal(100))).quantize(Decimal("0.0001"))

    return DrawdownResult(
        methodology="risk.beta_drawdown",
        methodology_version="2",
        calculation_status="available",
        calculation_reason_codes=[],
        start_date=start_date,
        end_date=end_date,
        max_drawdown_pct=(max_dd * Decimal(100)).quantize(Decimal("0.01")),
        peak_date=max_dd_peak_date,
        trough_date=max_dd_trough_date,
        recovery_date=recovery_date,
        days_to_recovery=days_to_recovery,
        current_drawdown_pct=current_dd,
        annualized_return_pct=(
            annualized.quantize(Decimal("0.01")) if annualized is not None else None
        ),
        calmar=calmar,
        underwater=underwater,
    )
