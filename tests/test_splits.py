"""Unit tests for the split-factor lookup used by the walk-back."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio_tracker.models import Security, StockSplit
from portfolio_tracker.services.splits import SplitFactors, load_split_factors


def test_factor_after_identity_when_no_splits():
    f = SplitFactors({})
    assert f.factor_after(1, date(2025, 1, 1)) == Decimal(1)


def test_factor_after_product_of_post_date_splits():
    # A 4:1 then a 2:1 split.
    f = SplitFactors({1: [(date(2024, 6, 1), Decimal(4)), (date(2025, 1, 1), Decimal(2))]})
    assert f.factor_after(1, date(2024, 1, 1)) == Decimal(8)  # both after -> 4*2
    assert f.factor_after(1, date(2024, 7, 1)) == Decimal(2)  # only the 2025 one
    assert f.factor_after(1, date(2025, 2, 1)) == Decimal(1)  # none after


def test_factor_after_boundary_is_strictly_after():
    f = SplitFactors({1: [(date(2025, 1, 1), Decimal(2))]})
    # A split ON the date is NOT after it (a same-day trade is already in
    # post-split units).
    assert f.factor_after(1, date(2025, 1, 1)) == Decimal(1)
    assert f.factor_after(1, date(2024, 12, 31)) == Decimal(2)


def test_factor_after_none_security_id():
    f = SplitFactors({1: [(date(2025, 1, 1), Decimal(2))]})
    assert f.factor_after(None, date(2024, 1, 1)) == Decimal(1)


def test_load_split_factors_from_db(session):
    sec = Security(plaid_security_id="s", ticker="AAPL", type="cs")
    session.add(sec)
    session.flush()
    session.add(
        StockSplit(security_id=sec.security_id, split_date=date(2024, 6, 1), ratio=Decimal(4))
    )
    session.commit()
    f = load_split_factors(session, [sec.security_id])
    assert f.factor_after(sec.security_id, date(2024, 1, 1)) == Decimal(4)
    assert f.factor_after(sec.security_id, date(2024, 7, 1)) == Decimal(1)


def test_load_split_factors_empty_when_no_rows(session):
    sec = Security(plaid_security_id="s", ticker="MELI", type="cs")
    session.add(sec)
    session.commit()
    f = load_split_factors(session, [sec.security_id])
    assert f.is_empty
    assert f.factor_after(sec.security_id, date(2024, 1, 1)) == Decimal(1)
