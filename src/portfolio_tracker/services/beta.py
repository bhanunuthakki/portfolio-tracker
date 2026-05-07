"""Portfolio beta, alpha, and R^2 vs a benchmark.

Beta is the slope of the linear regression of portfolio daily returns
against benchmark daily returns:

    R_p(d) = α + β · R_m(d) + ε

  * β > 1   → portfolio swings more than the benchmark
  * β = 1   → moves in lockstep
  * β < 1   → less volatile than the benchmark
  * β < 0   → moves opposite to the benchmark

Alpha is the intercept — the average daily excess return not explained by
beta (annualized for display). R² indicates how much of the portfolio's
day-to-day variation is explained by the benchmark; low R² (say <0.3) means
beta is a bad summary of the relationship.

Daily returns are computed:
  * Portfolio: r_p(d) = (V(d) − V(d−1) − cf(d)) / V(d−1)
  * Benchmark: r_m(d) = (close(d) − close(d−1)) / close(d−1)

Days are paired by date — anything missing from either side is dropped.
Days with reconstructed values that swung > 30% are also dropped as
likely backfill artifacts.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from portfolio_tracker.services.performance import (
    _benchmark_series,
    _daily_external_cashflows,
    _daily_portfolio_value,
)

# Daily moves in absolute value above this are dropped — they're almost
# certainly reconstruction noise rather than real market events.
_MAX_PLAUSIBLE_DAILY_RETURN = Decimal("0.30")
_TRADING_DAYS_PER_YEAR = 252


class BetaResult(BaseModel):
    benchmark: str
    start_date: date
    end_date: date
    sample_size: int
    beta: float | None
    alpha_annualized_pct: float | None
    r_squared: float | None
    correlation: float | None
    portfolio_volatility_annualized: float | None
    benchmark_volatility_annualized: float | None
    notes: list[str]


def compute_beta(
    session: Session,
    start_date: date,
    end_date: date,
    benchmark_symbol: str = "SPY",
) -> BetaResult:
    daily_value = _daily_portfolio_value(session, start_date, end_date)
    daily_cashflow = _daily_external_cashflows(session, start_date, end_date)
    benchmark_series = _benchmark_series(session, start_date, end_date)
    benchmark_closes = benchmark_series.get(benchmark_symbol, {})

    portfolio_returns = _daily_returns(daily_value, daily_cashflow)
    benchmark_returns = _benchmark_daily_returns(benchmark_closes)

    paired_p, paired_m, dropped = _pair_returns(portfolio_returns, benchmark_returns)

    notes: list[str] = []
    if dropped > 0:
        notes.append(
            f"Dropped {dropped} day(s) with implausible (>30%) reconstructed "
            f"portfolio moves — likely backfill artifacts."
        )
    if not paired_p:
        notes.append(
            "No overlapping return days. Beta requires at least 2 paired "
            "(portfolio, benchmark) observations."
        )
        return BetaResult(
            benchmark=benchmark_symbol,
            start_date=start_date,
            end_date=end_date,
            sample_size=0,
            beta=None,
            alpha_annualized_pct=None,
            r_squared=None,
            correlation=None,
            portfolio_volatility_annualized=None,
            benchmark_volatility_annualized=None,
            notes=notes,
        )

    beta, alpha, r_squared, correlation = _ols(paired_p, paired_m)
    p_vol = _annualized_volatility(paired_p)
    m_vol = _annualized_volatility(paired_m)
    alpha_annual = (
        alpha * _TRADING_DAYS_PER_YEAR * 100 if alpha is not None else None
    )

    if len(paired_p) < 30:
        notes.append(
            f"Sample size is small ({len(paired_p)} days). Beta confidence "
            f"increases with more daily observations — accumulate forward "
            f"snapshots for a more reliable estimate."
        )
    if r_squared is not None and r_squared < 0.3:
        notes.append(
            f"R² is low ({r_squared:.2f}) — beta poorly summarizes this "
            f"portfolio's relationship to {benchmark_symbol}. Consider "
            f"a multi-factor model or a different benchmark."
        )

    return BetaResult(
        benchmark=benchmark_symbol,
        start_date=start_date,
        end_date=end_date,
        sample_size=len(paired_p),
        beta=beta,
        alpha_annualized_pct=alpha_annual,
        r_squared=r_squared,
        correlation=correlation,
        portfolio_volatility_annualized=p_vol,
        benchmark_volatility_annualized=m_vol,
        notes=notes,
    )


def _daily_returns(
    daily_value: dict[date, Decimal],
    daily_cashflow: dict[date, Decimal],
) -> dict[date, Decimal]:
    """Per-day return excluding external cashflow effects."""
    sorted_dates = sorted(daily_value.keys())
    out: dict[date, Decimal] = {}
    for i in range(1, len(sorted_dates)):
        d_prev, d_curr = sorted_dates[i - 1], sorted_dates[i]
        v_prev = daily_value[d_prev]
        v_curr = daily_value[d_curr]
        cf = daily_cashflow.get(d_curr, Decimal(0))
        if v_prev <= 0:
            continue
        r = (v_curr - v_prev - cf) / v_prev
        out[d_curr] = r
    return out


def _benchmark_daily_returns(
    closes: dict[date, Decimal],
) -> dict[date, Decimal]:
    sorted_dates = sorted(closes.keys())
    out: dict[date, Decimal] = {}
    for i in range(1, len(sorted_dates)):
        d_prev, d_curr = sorted_dates[i - 1], sorted_dates[i]
        c_prev, c_curr = closes[d_prev], closes[d_curr]
        if c_prev <= 0:
            continue
        out[d_curr] = (c_curr - c_prev) / c_prev
    return out


def _pair_returns(
    portfolio_returns: dict[date, Decimal],
    benchmark_returns: dict[date, Decimal],
) -> tuple[list[float], list[float], int]:
    """Align by date, dropping anything missing from either side or
    portfolio returns deemed implausible. Returns (p, m, dropped_count)."""
    p: list[float] = []
    m: list[float] = []
    dropped = 0
    common_dates = sorted(set(portfolio_returns) & set(benchmark_returns))
    for d in common_dates:
        r_p = portfolio_returns[d]
        if abs(r_p) > _MAX_PLAUSIBLE_DAILY_RETURN:
            dropped += 1
            continue
        p.append(float(r_p))
        m.append(float(benchmark_returns[d]))
    return p, m, dropped


def _ols(
    p: list[float], m: list[float]
) -> tuple[float | None, float | None, float | None, float | None]:
    """Ordinary-least-squares slope, intercept, R², and correlation.

    Returns (beta, alpha, r_squared, correlation). All None when the sample
    has zero variance in the benchmark (degenerate).
    """
    n = len(p)
    if n < 2:
        return (None, None, None, None)
    mean_p = sum(p) / n
    mean_m = sum(m) / n
    cov = sum((pi - mean_p) * (mi - mean_m) for pi, mi in zip(p, m)) / n
    var_m = sum((mi - mean_m) ** 2 for mi in m) / n
    var_p = sum((pi - mean_p) ** 2 for pi in p) / n
    if var_m == 0:
        return (None, None, None, None)
    beta = cov / var_m
    alpha = mean_p - beta * mean_m
    if var_p == 0:
        correlation = 0.0
        r_squared = 0.0
    else:
        correlation = cov / ((var_p * var_m) ** 0.5)
        r_squared = correlation * correlation
    return (beta, alpha, r_squared, correlation)


def _annualized_volatility(returns: list[float]) -> float | None:
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return (variance ** 0.5) * (_TRADING_DAYS_PER_YEAR ** 0.5)
