"""Unit tests for the pure regression / ratio math in services/beta.py.

These functions are deterministic over small float/Decimal lists, so the
expected values are hand-computed (annualization factor = sqrt(252)).
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest

from portfolio_tracker.services.beta import (
    _daily_returns,
    _information_ratio,
    _ols,
    _pair_returns,
    _sharpe,
    _sortino,
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
    # excess [-0.02, 0.04], mean 0.01; downside_var = 0.0004/2 = 0.0002.
    expected = (0.01 / math.sqrt(0.0002)) * _SQRT_252
    assert _sortino([-0.02, 0.04], 0.0) == pytest.approx(expected)
    assert _sortino([-0.02, 0.04], 0.0) == pytest.approx(11.224972, rel=1e-5)


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
