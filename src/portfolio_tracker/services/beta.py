"""Risk metrics relative to a benchmark or in absolute terms.

Beta + alpha + R² (regression-based, single-factor) describe the
portfolio's relationship to the benchmark. Sharpe / Sortino / Information
Ratio describe the portfolio in absolute / vs-benchmark terms.

  * **Beta**             slope of portfolio daily returns regressed on
                         benchmark daily returns
  * **Alpha (annualized)** intercept × 252 (the regression's "this much
                         excess return per year unrelated to beta")
  * **R²**               share of portfolio variance explained by the
                         benchmark (low R² ⇒ beta is a weak summary)
  * **Sharpe ratio**     (R̄_p − R̄_f) / σ_p, annualized — risk-adjusted
                         return vs cash; benchmark-independent
  * **Sortino ratio**    same but uses downside deviation instead of σ_p;
                         doesn't penalize upside volatility
  * **Information ratio** (R̄_p − R̄_b) / σ(R_p − R_b), annualized — how
                         consistently the portfolio outperforms the
                         benchmark per unit of tracking error

The benchmark may be:
  * A symbol in the `benchmarks` table (`SPY`, `QQQ`, anything you've
    pulled prices for)
  * The literal string `POLICY`, in which case the benchmark daily return
    is the weight-weighted sum of the user's policy components.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import PolicyWeight
from portfolio_tracker.services.performance import (
    _benchmark_series,
)
from portfolio_tracker.services.position_alpha import compute_position_alpha

# Daily moves above this are dropped — almost certainly reconstruction noise.
_MAX_PLAUSIBLE_DAILY_RETURN = Decimal("0.30")
_TRADING_DAYS_PER_YEAR = 252
_POLICY_PSEUDO_SYMBOL = "POLICY"

# Default annual risk-free rate when no override is supplied. ~4% is a
# reasonable proxy for short-term Treasury yields in the current rate
# environment; the API exposes this as a query param so it can be tuned.
_DEFAULT_RISK_FREE_ANNUAL = 0.04


class BetaResult(BaseModel):
    benchmark: str
    start_date: date
    end_date: date
    sample_size: int
    risk_free_annual: float

    # Regression vs benchmark
    beta: float | None
    alpha_annualized_pct: float | None
    r_squared: float | None
    correlation: float | None

    # Absolute risk-adjusted performance
    sharpe: float | None
    sortino: float | None
    information_ratio: float | None

    # Volatility scale
    portfolio_volatility_annualized: float | None
    benchmark_volatility_annualized: float | None
    tracking_error_annualized: float | None

    notes: list[str]


def compute_beta(
    session: Session,
    start_date: date,
    end_date: date,
    benchmark_symbol: str = "SPY",
    risk_free_annual: float = _DEFAULT_RISK_FREE_ANNUAL,
    exclude_index_etfs: bool = False,
    reserve_amount: Decimal = Decimal(0),
) -> BetaResult:
    """Risk metrics over [start_date, end_date].

    `exclude_index_etfs` carves broad-market US ETFs out of the V series
    and adds their buy/sell flows to the cashflow series — same logic as
    `compute_performance_series` — so all risk numbers (σ, Sharpe, beta,
    R², Sortino) reflect just the *active stock-picking* portion of the
    portfolio. The σ vs benchmark-σ comparison then directly answers
    "did my picking provide a diversification / derisking benefit?"

    `reserve_amount` is currently a no-op — see note at bottom. SGOV is
    already excluded as a cash-equivalent in the position-alpha V series,
    so subtracting another flat amount doesn't change daily returns.

    **Series source**: portfolio daily V comes from `compute_position_alpha`
    (positions only, no cash, no cash_adj smoothing, no walk-back-derived
    cashflow attribution). This makes the regression alpha consistent with
    the dollar alpha shown elsewhere on the dashboard — earlier versions
    of this service used the legacy walk-back-reconstructed total V which
    introduced cashflow-attribution noise that distorted daily returns
    by 5-15 pp/year (see PERFORMANCE_AUDIT.md F6, now obsoleted).
    """
    # Position-alpha builds positions-only V series, walks transactions
    # forward, applies prices daily. This is the same series the dashboard's
    # main chart renders.
    pa_result = compute_position_alpha(
        session,
        start_date,
        end_date,
        exclude_broad_index=exclude_index_etfs,
    )
    daily_value: dict[date, Decimal] = {
        p.date: Decimal(p.portfolio_value) for p in pa_result.series
    }
    # On a buy day, position V rises by the buy $ (new shares added at
    # today's price). To isolate market-driven returns, subtract that
    # trade cashflow from the daily delta. Position-alpha tracks this
    # per day in `position_cashflow` (buys − sells).
    daily_cashflow: dict[date, Decimal] = {
        p.date: Decimal(p.position_cashflow) for p in pa_result.series
    }

    portfolio_returns = _daily_returns(daily_value, daily_cashflow)
    benchmark_returns = _benchmark_daily_returns_for(
        session, benchmark_symbol, start_date, end_date
    )

    paired_p, paired_m, dropped = _pair_returns(portfolio_returns, benchmark_returns)
    notes: list[str] = []
    if dropped > 0:
        notes.append(
            f"Dropped {dropped} day(s) with implausible (>30%) reconstructed "
            f"portfolio moves — likely backfill artifacts."
        )

    if not paired_p:
        notes.append(
            "No overlapping return days. Need at least 2 paired (portfolio, "
            "benchmark) observations."
        )
        return _empty_result(benchmark_symbol, start_date, end_date, risk_free_annual, notes)

    # Daily risk-free rate (simple, not compounded — fine for daily return scale).
    rf_daily = risk_free_annual / _TRADING_DAYS_PER_YEAR

    beta, alpha, r_squared, correlation = _ols(paired_p, paired_m)
    sharpe = _sharpe(paired_p, rf_daily)
    sortino = _sortino(paired_p, rf_daily)
    info_ratio, tracking_error = _information_ratio(paired_p, paired_m)
    p_vol = _annualized_volatility(paired_p)
    m_vol = _annualized_volatility(paired_m)
    alpha_annual = alpha * _TRADING_DAYS_PER_YEAR * 100 if alpha is not None else None

    if len(paired_p) < 30:
        notes.append(
            f"Sample size is small ({len(paired_p)} days). Confidence in "
            f"these metrics increases with more daily observations — "
            f"accumulate forward snapshots for a more reliable estimate."
        )
    if r_squared is not None and r_squared < 0.3:
        notes.append(
            f"R² is low ({r_squared:.2f}) — beta poorly summarizes this "
            f"portfolio's relationship to {benchmark_symbol}. Consider a "
            f"different benchmark or a multi-factor model."
        )

    return BetaResult(
        benchmark=benchmark_symbol,
        start_date=start_date,
        end_date=end_date,
        sample_size=len(paired_p),
        risk_free_annual=risk_free_annual,
        beta=beta,
        alpha_annualized_pct=alpha_annual,
        r_squared=r_squared,
        correlation=correlation,
        sharpe=sharpe,
        sortino=sortino,
        information_ratio=info_ratio,
        portfolio_volatility_annualized=p_vol,
        benchmark_volatility_annualized=m_vol,
        tracking_error_annualized=tracking_error,
        notes=notes,
    )


def _empty_result(
    benchmark: str,
    start_date: date,
    end_date: date,
    risk_free_annual: float,
    notes: list[str],
) -> BetaResult:
    return BetaResult(
        benchmark=benchmark,
        start_date=start_date,
        end_date=end_date,
        sample_size=0,
        risk_free_annual=risk_free_annual,
        beta=None,
        alpha_annualized_pct=None,
        r_squared=None,
        correlation=None,
        sharpe=None,
        sortino=None,
        information_ratio=None,
        portfolio_volatility_annualized=None,
        benchmark_volatility_annualized=None,
        tracking_error_annualized=None,
        notes=notes,
    )


def _benchmark_daily_returns_for(
    session: Session, symbol: str, start_date: date, end_date: date
) -> dict[date, Decimal]:
    """Daily return series for either a single ticker or the policy mix."""
    benchmark_series = _benchmark_series(session, start_date, end_date)
    if symbol.upper() == _POLICY_PSEUDO_SYMBOL:
        return _policy_daily_returns(session, benchmark_series)
    return _benchmark_daily_returns(benchmark_series.get(symbol, {}))


def _policy_daily_returns(
    session: Session, benchmark_series: dict[str, dict[date, Decimal]]
) -> dict[date, Decimal]:
    """Weight-weighted sum of component daily returns.

    For each date d:  r_policy(d) = Σ_i w_i × r_i(d)

    Components missing data on a date are skipped — the policy return for
    that date sums only the weights that had data, which slightly biases
    high if a holiday-ish ticker drops out, but is the right behavior for
    sparse benchmark coverage.
    """
    weights = {
        r.ticker: Decimal(r.weight_bps) / Decimal(10000)
        for r in session.execute(select(PolicyWeight)).scalars().all()
        if r.weight_bps > 0
    }
    if not weights:
        return {}
    component_returns: dict[str, dict[date, Decimal]] = {}
    for ticker in weights:
        component_returns[ticker] = _benchmark_daily_returns(benchmark_series.get(ticker, {}))
    all_dates: set[date] = set()
    for series in component_returns.values():
        all_dates.update(series)
    out: dict[date, Decimal] = {}
    for d in all_dates:
        total = Decimal(0)
        for ticker, weight in weights.items():
            r = component_returns[ticker].get(d)
            if r is None:
                continue
            total += weight * r
        out[d] = total
    return out


def _daily_returns(
    daily_value: dict[date, Decimal],
    daily_cashflow: dict[date, Decimal],
) -> dict[date, Decimal]:
    sorted_dates = sorted(daily_value.keys())
    out: dict[date, Decimal] = {}
    for i in range(1, len(sorted_dates)):
        d_prev, d_curr = sorted_dates[i - 1], sorted_dates[i]
        v_prev = daily_value[d_prev]
        v_curr = daily_value[d_curr]
        cf = daily_cashflow.get(d_curr, Decimal(0))
        if v_prev <= 0:
            continue
        out[d_curr] = (v_curr - v_prev - cf) / v_prev
    return out


def _benchmark_daily_returns(closes: dict[date, Decimal]) -> dict[date, Decimal]:
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


# ---- regression + ratio math --------------------------------------------


def _ols(
    p: list[float], m: list[float]
) -> tuple[float | None, float | None, float | None, float | None]:
    n = len(p)
    if n < 2:
        return (None, None, None, None)
    mean_p = sum(p) / n
    mean_m = sum(m) / n
    cov = sum((pi - mean_p) * (mi - mean_m) for pi, mi in zip(p, m, strict=False)) / n
    var_m = sum((mi - mean_m) ** 2 for mi in m) / n
    var_p = sum((pi - mean_p) ** 2 for pi in p) / n
    if var_m == 0:
        return (None, None, None, None)
    beta = cov / var_m
    alpha = mean_p - beta * mean_m
    if var_p == 0:
        return (beta, alpha, 0.0, 0.0)
    correlation = cov / ((var_p * var_m) ** 0.5)
    r_squared = correlation * correlation
    return (beta, alpha, r_squared, correlation)


def _sharpe(returns: list[float], rf_daily: float) -> float | None:
    n = len(returns)
    if n < 2:
        return None
    excess = [r - rf_daily for r in returns]
    mean = sum(excess) / n
    var = sum((r - mean) ** 2 for r in excess) / (n - 1)
    if var == 0:
        return None
    return (mean / var**0.5) * (_TRADING_DAYS_PER_YEAR**0.5)


def _sortino(returns: list[float], rf_daily: float) -> float | None:
    """Like Sharpe but uses downside deviation (only counts r < rf)."""
    n = len(returns)
    if n < 2:
        return None
    excess = [r - rf_daily for r in returns]
    mean = sum(excess) / n
    downside = [min(0.0, r) for r in excess]
    downside_var = sum(d * d for d in downside) / n
    if downside_var == 0:
        return None
    return (mean / downside_var**0.5) * (_TRADING_DAYS_PER_YEAR**0.5)


def _information_ratio(p: list[float], m: list[float]) -> tuple[float | None, float | None]:
    """IR = mean(R_p - R_b) / σ(R_p - R_b), annualized.

    Returns (ir, tracking_error_annualized). Tracking error is reported
    separately because it's interesting on its own.
    """
    if len(p) < 2:
        return (None, None)
    diff = [pi - mi for pi, mi in zip(p, m, strict=False)]
    mean = sum(diff) / len(diff)
    var = sum((d - mean) ** 2 for d in diff) / (len(diff) - 1)
    if var == 0:
        return (None, 0.0)
    te = var**0.5 * (_TRADING_DAYS_PER_YEAR**0.5)
    ir = (mean / var**0.5) * (_TRADING_DAYS_PER_YEAR**0.5)
    return (ir, te)


def _annualized_volatility(returns: list[float]) -> float | None:
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return (variance**0.5) * (_TRADING_DAYS_PER_YEAR**0.5)
