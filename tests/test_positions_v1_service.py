"""Unit tests for the pure positions-v1 math (no DB).

Covers the 4-way `tax_treatment` mapping (which must mirror the
earnings-summary client exactly) and the `build_positions_result` assembler:
percent-of-portfolio, lot-level tax bucketing, and edge cases.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portfolio_tracker.schemas import ConsolidatedHoldingOut, HoldingByAccountOut
from portfolio_tracker.services.positions_v1 import (
    build_positions_result,
    tax_treatment,
)

_D = date(2025, 6, 2)


@pytest.mark.parametrize(
    ("account_type", "subtype", "expected"),
    [
        # tax_free: roth (any) + hsa, roth check wins over the 401k/ira tokens
        ("investment", "roth ira", "tax_free"),
        ("investment", "Roth 401k", "tax_free"),
        ("depository", "hsa", "tax_free"),
        ("investment", "ROTH", "tax_free"),
        # tax_deferred: traditional retirement vehicles
        ("investment", "401k", "tax_deferred"),
        ("investment", "traditional ira", "tax_deferred"),
        ("investment", "sep", "tax_deferred"),
        ("investment", "457b", "tax_deferred"),
        ("investment", "pension", "tax_deferred"),
        ("investment", "simple", "tax_deferred"),
        # taxable: brokerage token in subtype OR brokerage account type
        ("brokerage", None, "taxable"),
        ("investment", "brokerage", "taxable"),
        # unknown: a bare individual/joint is NOT taxable in the 4-way contract
        ("investment", "individual", "unknown"),
        ("investment", "joint", "unknown"),
        (None, None, "unknown"),
        ("", "", "unknown"),
    ],
)
def test_tax_treatment_mapping(account_type: str | None, subtype: str | None, expected: str):
    assert tax_treatment(account_type, subtype) == expected


def _lot(account_id: int, name: str, value: str) -> HoldingByAccountOut:
    return HoldingByAccountOut(
        account_id=account_id,
        account_name=name,
        quantity=Decimal(1),
        institution_value=Decimal(value),
        cost_basis=Decimal(value),
        cost_basis_source=None,
        cost_basis_unreliable=False,
    )


def _holding(
    security_id: int,
    ticker: str,
    total_value: Decimal | None,
    accounts: list[HoldingByAccountOut],
) -> ConsolidatedHoldingOut:
    return ConsolidatedHoldingOut(
        snapshot_date=_D,
        security_id=security_id,
        ticker=ticker,
        name=ticker,
        total_quantity=Decimal(len(accounts)),
        total_value=total_value,
        total_cost_basis=total_value,
        weighted_avg_cost_per_share=None,
        unrealized_pnl=Decimal(0),
        accounts=accounts,
        currency="USD",
        has_unreliable_cost_basis=False,
    )


def test_build_percent_and_buckets():
    # AAPL ($10k) spans a taxable brokerage lot ($6k) and a tax_free roth lot
    # ($4k); MSFT ($5k) in a tax_deferred 401k; TSLA ($5k) in an unknown acct.
    aapl = _holding(
        1,
        "AAPL",
        Decimal(10000),
        [_lot(10, "Brokerage", "6000"), _lot(11, "Roth IRA", "4000")],
    )
    msft = _holding(2, "MSFT", Decimal(5000), [_lot(12, "401k", "5000")])
    tsla = _holding(3, "TSLA", Decimal(5000), [_lot(13, "Cash Mgmt", "5000")])
    account_tax = {10: "taxable", 11: "tax_free", 12: "tax_deferred", 13: "unknown"}

    res = build_positions_result(_D, [aapl, msft, tsla], account_tax)

    assert res.total_market_value == Decimal(20000)
    by_ticker = {p.ticker: p for p in res.positions}
    assert by_ticker["AAPL"].percent_of_portfolio == Decimal("50.0000")
    assert by_ticker["MSFT"].percent_of_portfolio == Decimal("25.0000")
    assert by_ticker["TSLA"].percent_of_portfolio == Decimal("25.0000")

    # Lot-level tax tagging — AAPL's two lots land in different buckets.
    aapl_lots = {lot.account_id: lot.tax_treatment for lot in by_ticker["AAPL"].accounts}
    assert aapl_lots == {10: "taxable", 11: "tax_free"}

    assert res.by_tax_treatment == {
        "taxable": Decimal(6000),
        "tax_deferred": Decimal(5000),
        "tax_free": Decimal(4000),
        "unknown": Decimal(5000),
    }


def test_build_missing_market_value_yields_null_percent():
    priced = _holding(1, "AAPL", Decimal(1000), [_lot(10, "Brokerage", "1000")])
    unpriced = _holding(2, "OPT", None, [_lot(11, "Brokerage", "0")])
    # The unpriced lot carries no institution_value, so it adds nothing to a bucket.
    unpriced.accounts[0].institution_value = None
    res = build_positions_result(_D, [priced, unpriced], {10: "taxable", 11: "taxable"})

    assert res.total_market_value == Decimal(1000)
    by_ticker = {p.ticker: p for p in res.positions}
    assert by_ticker["AAPL"].percent_of_portfolio == Decimal("100.0000")
    assert by_ticker["OPT"].percent_of_portfolio is None


def test_build_unknown_account_defaults_to_unknown_bucket():
    h = _holding(1, "AAPL", Decimal(1000), [_lot(99, "Mystery", "1000")])
    # account 99 not present in the tax map -> defaults to "unknown"
    res = build_positions_result(_D, [h], {})
    assert res.positions[0].accounts[0].tax_treatment == "unknown"
    assert res.by_tax_treatment["unknown"] == Decimal(1000)


def test_build_empty_book():
    res = build_positions_result(None, [], {})
    assert res.total_market_value == Decimal(0)
    assert res.positions == []
    assert res.by_tax_treatment == {
        "taxable": Decimal(0),
        "tax_deferred": Decimal(0),
        "tax_free": Decimal(0),
        "unknown": Decimal(0),
    }
    assert res.snapshot_date is None
