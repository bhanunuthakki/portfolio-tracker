"""Unit tests for the pure regression / ratio math in services/beta.py.

These functions are deterministic over small float/Decimal lists, so the
expected values are hand-computed (annualization factor = sqrt(252)).
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from portfolio_tracker.models import Benchmark
from portfolio_tracker.services import beta as beta_service
from portfolio_tracker.services.beta import (
    _alpha_significance,
    _benchmark_price_daily_returns_for,
    _daily_returns,
    _information_ratio,
    _ols,
    _pair_returns,
    _sharpe,
    _sortino,
    _window_risk_free,
)

_SQRT_252 = math.sqrt(252)


# ---- _ols ----------------------------------------------------------------


def test_ols_perfectly_linear():
    # p = 2*m exactly → beta 2, alpha 0, R^2 / correlation 1.
    m = [0.01, 0.02, -0.01, 0.03]
    p = [2 * x for x in m]
    beta, alpha, r_squared, correlation = _ols(p, m)
    assert beta == pytest.approx(2.0)
    assert alpha == pytest.approx(0.0, abs=1e-12)
    assert r_squared == pytest.approx(1.0)
    assert correlation == pytest.approx(1.0)


def test_ols_too_few_points_returns_nones():
    assert _ols([0.01], [0.02]) == (None, None, None, None)


# ---- _alpha_significance -------------------------------------------------


def test_alpha_significance_known_value():
    # m=[-1,0,1], p=[0,1,3] -> beta 1.5, alpha 4/3, residuals [1/6,-1/3,1/6],
    # SSE 1/6, s^2 1/6, SE(alpha)^2 = (1/6)(1/3) = 1/18, t = alpha/SE.
    m = [-1.0, 0.0, 1.0]
    p = [0.0, 1.0, 3.0]
    beta, alpha, _r2, _corr = _ols(p, m)
    se, t = _alpha_significance(p, m, beta, alpha)
    assert se == pytest.approx(1 / math.sqrt(18))
    assert t == pytest.approx(5.656854, rel=1e-5)


def test_alpha_significance_too_few_points():
    assert _alpha_significance([0.0, 1.0], [0.0, 1.0], 1.0, 0.0) == (None, None)


# ---- _window_risk_free ---------------------------------------------------


def test_window_risk_free_averages_tbill_yield(session):
    session.add_all(
        [
            Benchmark(symbol="^IRX", date=date(2025, 1, 2), close=Decimal("4.00")),
            Benchmark(symbol="^IRX", date=date(2025, 1, 3), close=Decimal("5.00")),
            # Outside the window — must be ignored.
            Benchmark(symbol="^IRX", date=date(2024, 6, 1), close=Decimal("0.50")),
        ]
    )
    session.commit()
    rf = _window_risk_free(session, date(2025, 1, 1), date(2025, 1, 31))
    assert rf == pytest.approx(0.045)  # avg(4, 5) / 100


def test_window_risk_free_none_when_no_data(session):
    assert _window_risk_free(session, date(2025, 1, 1), date(2025, 1, 31)) is None


def test_benchmark_returns_use_price_close_not_total_return_close(session):
    d0, d1 = date(2025, 1, 2), date(2025, 1, 3)
    session.add_all(
        [
            Benchmark(
                symbol="SPY",
                date=d0,
                close=Decimal(100),
                total_return_close=Decimal(100),
            ),
            Benchmark(
                symbol="SPY",
                date=d1,
                close=Decimal(105),
                total_return_close=Decimal(110),
            ),
        ]
    )
    session.commit()

    returns = _benchmark_price_daily_returns_for(session, "SPY", d0, d1)
    assert returns[d1] == Decimal("0.05")


def test_beta_propagates_position_calculation_unavailable(session, monkeypatch):
    start, end = date(2025, 1, 2), date(2025, 1, 31)
    unavailable = SimpleNamespace(
        calculation_status="unavailable",
        calculation_reason_codes=["share_movement_unmatched"],
        series=[],
    )
    monkeypatch.setattr(
        beta_service, "compute_position_alpha", lambda *_args, **_kwargs: unavailable
    )

    result = beta_service.compute_beta(session, start, end)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["share_movement_unmatched"]
    assert result.beta is None
    assert result.alpha_annualized_pct is None


def test_beta_fails_closed_with_only_one_paired_observation(session, monkeypatch):
    d0, d1 = date(2025, 1, 2), date(2025, 1, 3)
    available = SimpleNamespace(
        calculation_status="available",
        calculation_reason_codes=[],
        series=[
            SimpleNamespace(date=d0, portfolio_value=Decimal(100), position_cashflow=Decimal(0)),
            SimpleNamespace(date=d1, portfolio_value=Decimal(101), position_cashflow=Decimal(0)),
        ],
    )
    monkeypatch.setattr(beta_service, "compute_position_alpha", lambda *_args, **_kwargs: available)
    monkeypatch.setattr(
        beta_service,
        "_benchmark_price_daily_returns_for",
        lambda *_args, **_kwargs: {d1: Decimal("0.01")},
    )

    result = beta_service.compute_beta(session, d0, d1, risk_free_annual=0.0)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["insufficient_return_observations"]
    assert result.beta is None
    assert result.r_squared is None
    assert result.correlation is None


def test_beta_fails_closed_when_benchmark_variance_is_zero(session, monkeypatch):
    d0, d1, d2 = date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 4)
    available = SimpleNamespace(
        calculation_status="available",
        calculation_reason_codes=[],
        series=[
            SimpleNamespace(date=d0, portfolio_value=Decimal(100), position_cashflow=Decimal(0)),
            SimpleNamespace(date=d1, portfolio_value=Decimal(101), position_cashflow=Decimal(0)),
            SimpleNamespace(date=d2, portfolio_value=Decimal(103), position_cashflow=Decimal(0)),
        ],
    )
    monkeypatch.setattr(beta_service, "compute_position_alpha", lambda *_args, **_kwargs: available)
    monkeypatch.setattr(
        beta_service,
        "_benchmark_price_daily_returns_for",
        lambda *_args, **_kwargs: {d1: Decimal("0.01"), d2: Decimal("0.01")},
    )

    result = beta_service.compute_beta(session, d0, d2, risk_free_annual=0.0)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["insufficient_return_observations"]
    assert result.sample_size == 2
    assert result.beta is None
    assert result.r_squared is None
    assert result.correlation is None


def test_alpha_significance_perfect_fit_is_undefined():
    # p = m + 2 exactly: zero residuals -> SE 0 -> t undefined (None).
    m = [-1.0, 0.0, 1.0]
    p = [1.0, 2.0, 3.0]
    beta, alpha, _r2, _corr = _ols(p, m)
    assert _alpha_significance(p, m, beta, alpha) == (None, None)


def test_ols_zero_benchmark_variance_returns_nones():
    # Benchmark constant → var_m == 0 → undefined slope.
    assert _ols([0.02, 0.03], [0.01, 0.01]) == (None, None, None, None)


# ---- _sharpe -------------------------------------------------------------


def test_sharpe_known_value():
    # returns [0,0.04], rf 0 → mean 0.02, sample std 0.0282842712.
    expected = (0.02 / math.sqrt(0.0008)) * _SQRT_252
    assert _sharpe([0.0, 0.04], 0.0) == pytest.approx(expected)
    assert _sharpe([0.0, 0.04], 0.0) == pytest.approx(11.224972, rel=1e-5)


def test_sharpe_too_few_points_is_none():
    assert _sharpe([0.02], 0.0) is None


def test_sharpe_zero_variance_is_none():
    assert _sharpe([0.02, 0.02], 0.0) is None


# ---- _sortino ------------------------------------------------------------


def test_sortino_known_value():
    # excess [-0.02, 0.04], mean 0.01; downside [-0.02, 0]; sample (n-1)
    # downside_var = 0.0004 / 1 = 0.0004 (matches Sharpe's n-1 convention).
    expected = (0.01 / math.sqrt(0.0004)) * _SQRT_252
    assert _sortino([-0.02, 0.04], 0.0) == pytest.approx(expected)
    assert _sortino([-0.02, 0.04], 0.0) == pytest.approx(7.937254, rel=1e-5)


def test_sortino_no_downside_is_none():
    # No return below rf → downside deviation 0 → undefined.
    assert _sortino([0.02, 0.03], 0.0) is None


# ---- _information_ratio --------------------------------------------------


def test_information_ratio_known_value():
    # diff [0.02, 0.01], mean 0.015, sample std sqrt(0.00005).
    std = math.sqrt(0.00005)
    ir, te = _information_ratio([0.03, 0.01], [0.01, 0.0])
    assert ir == pytest.approx((0.015 / std) * _SQRT_252)
    assert te == pytest.approx(std * _SQRT_252)
    assert ir == pytest.approx(33.674916, rel=1e-5)


def test_information_ratio_zero_tracking_error():
    # Identical series → tracking error 0, IR undefined.
    ir, te = _information_ratio([0.01, 0.02], [0.01, 0.02])
    assert ir is None
    assert te == 0.0


def test_information_ratio_too_few_points():
    assert _information_ratio([0.01], [0.02]) == (None, None)


# ---- _daily_returns ------------------------------------------------------


def test_daily_returns_subtracts_cashflow():
    d0, d1, d2 = date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)
    values = {d0: Decimal(1000), d1: Decimal(1100), d2: Decimal(1045)}
    cashflow = {d1: Decimal(50)}
    out = _daily_returns(values, cashflow)
    # (1100 - 1000 - 50) / 1000
    assert out[d1] == Decimal("0.05")
    # (1045 - 1100 - 0) / 1100
    assert out[d2] == Decimal("-0.05")
    assert d0 not in out  # no prior day


def test_daily_returns_skips_nonpositive_prior_value():
    d0, d1 = date(2025, 1, 1), date(2025, 1, 2)
    out = _daily_returns({d0: Decimal(0), d1: Decimal(100)}, {})
    assert out == {}


# ---- _pair_returns -------------------------------------------------------


def test_pair_returns_excludes_only_data_errors_and_keeps_real_tails():
    d1, d2, d3, d4, d5 = (
        date(2025, 1, 2),
        date(2025, 1, 3),
        date(2025, 1, 4),
        date(2025, 1, 5),
        date(2025, 1, 6),
    )
    portfolio = {
        d1: Decimal("0.05"),
        d2: Decimal("0.6"),  # >50% aggregate move → suspected data error
        d3: Decimal("-0.1"),
        d5: Decimal("0.35"),  # real earnings-day gap → MUST be retained
    }
    benchmark = {
        d1: Decimal("0.04"),
        d2: Decimal("0.4"),
        d3: Decimal("-0.08"),
        d4: Decimal("0.01"),  # no portfolio counterpart → intersected out
        d5: Decimal("0.02"),
    }
    p, m, excluded = _pair_returns(portfolio, benchmark)
    # Only d2 excluded (and reported with its magnitude); the 35% real move at
    # d5 survives so volatility/Sharpe capture genuine tail risk; d4 absent.
    assert excluded == [(d2, Decimal("0.6"))]
    assert p == pytest.approx([0.05, -0.1, 0.35])
    assert m == pytest.approx([0.04, -0.08, 0.02])


def test_pair_returns_threshold_is_tunable():
    d1, d2 = date(2025, 1, 2), date(2025, 1, 3)
    portfolio = {d1: Decimal("0.05"), d2: Decimal("0.35")}
    benchmark = {d1: Decimal("0.04"), d2: Decimal("0.30")}
    # A stricter 0.20 bound now treats the 35% move as out-of-bounds.
    p, _m, excluded = _pair_returns(portfolio, benchmark, Decimal("0.20"))
    assert excluded == [(d2, Decimal("0.35"))]
    assert p == pytest.approx([0.05])
