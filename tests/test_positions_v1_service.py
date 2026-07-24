"""Unit tests for the pure positions-v1 math (no DB).

Covers the detailed five-way `tax_treatment` mapping (the SC-1 ruling in
docs/design/phase0_decision_addendum.md) and the `build_positions_result`
assembler: percent-of-portfolio, lot-level tax bucketing, and edge cases.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portfolio_tracker.schemas import ConsolidatedHoldingOut, HoldingByAccountOut
from portfolio_tracker.services.positions_v1 import (
    build_positions_result,
    tax_treatment,
    tax_treatment_detail,
)

_D = date(2025, 6, 2)


@pytest.mark.parametrize(
    ("account_type", "subtype", "expected"),
    [
        # roth: any roth subtype, roth check wins over the 401k/ira tokens
        ("investment", "roth ira", "roth"),
        ("investment", "Roth 401k", "roth"),
        ("investment", "ROTH", "roth"),
        # hsa: exact subtype
        ("depository", "hsa", "hsa"),
        # pretax: traditional retirement vehicles
        ("investment", "401k", "pretax"),
        ("investment", "traditional ira", "pretax"),
        ("investment", "sep", "pretax"),
        ("investment", "457b", "pretax"),
        ("investment", "pension", "pretax"),
        ("investment", "simple", "pretax"),
        # taxable: brokerage token in subtype OR brokerage account type
        ("brokerage", None, "taxable"),
        ("investment", "brokerage", "taxable"),
        # cash-management sleeves (Robinhood via SnapTrade arrives as CHECKING)
        ("investment", "CHECKING", "taxable"),
        # weakest positive tier: bare individual/joint -> taxable at LOW confidence
        ("investment", "individual", "taxable"),
        ("investment", "joint", "taxable"),
        (None, None, "unknown"),
        ("", "", "unknown"),
    ],
)
def test_tax_treatment_mapping(account_type: str | None, subtype: str | None, expected: str):
    assert tax_treatment(account_type, subtype) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # SnapTrade omits subtype for some institutions; the NAME tier covers
        # the live cases the consumers used to hand-classify.
        ("BrokerageLink Roth", "roth"),
        ("BrokerageLink", "pretax"),  # self-directed 401(k) window, owner-confirmed
        ("Health Savings Account", "hsa"),
        ("META PLATFORMS, INC. 401(K) PLAN", "pretax"),
        ("Robinhood traditional IRA", "pretax"),
        ("SoFi Self-directed", "taxable"),
        ("Admiral Shares Fund", "unknown"),  # 'ira' is word-ish, not substring
    ],
)
def test_tax_treatment_name_tier(name: str, expected: str):
    assert tax_treatment("investment", None, name) == expected


def test_tax_treatment_detail_evidence_and_confidence():
    roth = tax_treatment_detail("investment", "Roth IRA")
    assert roth.treatment == "roth"
    assert roth.evidence == "subtype:roth ira"
    assert roth.confidence == "high"

    type_only = tax_treatment_detail("brokerage", None)
    assert type_only.treatment == "taxable"
    assert type_only.evidence == "type:brokerage"
    assert type_only.confidence == "medium"

    named = tax_treatment_detail("investment", None, "BrokerageLink Roth")
    assert named.treatment == "roth"
    assert named.evidence == "name:brokeragelink roth"
    assert named.confidence == "medium"

    individual = tax_treatment_detail("investment", "individual")
    assert individual.treatment == "taxable"
    assert individual.evidence == "subtype:individual"
    assert individual.confidence == "low"

    # Subtype beats name: an explicit roth subtype wins over a taxable-looking name.
    subtype_wins = tax_treatment_detail("investment", "roth ira", "Self-directed brokerage")
    assert subtype_wins.treatment == "roth"

    blank = tax_treatment_detail(None, None)
    assert blank.treatment == "unknown"
    assert blank.evidence is None
    assert blank.confidence == "low"


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
    # AAPL ($10k) spans a taxable brokerage lot ($6k) and a roth lot ($4k);
    # MSFT ($5k) in a pretax 401k; TSLA ($5k) in an unknown account.
    aapl = _holding(
        1,
        "AAPL",
        Decimal(10000),
        [_lot(10, "Brokerage", "6000"), _lot(11, "Roth IRA", "4000")],
    )
    msft = _holding(2, "MSFT", Decimal(5000), [_lot(12, "401k", "5000")])
    tsla = _holding(3, "TSLA", Decimal(5000), [_lot(13, "Cash Mgmt", "5000")])
    account_tax = {10: "taxable", 11: "roth", 12: "pretax", 13: "unknown"}

    res = build_positions_result(_D, [aapl, msft, tsla], account_tax)

    assert res.total_market_value == Decimal(20000)
    by_ticker = {p.ticker: p for p in res.positions}
    assert by_ticker["AAPL"].percent_of_portfolio == Decimal("50.0000")
    assert by_ticker["MSFT"].percent_of_portfolio == Decimal("25.0000")
    assert by_ticker["TSLA"].percent_of_portfolio == Decimal("25.0000")

    # Lot-level tax tagging — AAPL's two lots land in different buckets.
    aapl_lots = {lot.account_id: lot.tax_treatment for lot in by_ticker["AAPL"].accounts}
    assert aapl_lots == {10: "taxable", 11: "roth"}

    assert res.by_tax_treatment == {
        "taxable": Decimal(6000),
        "pretax": Decimal(5000),
        "roth": Decimal(4000),
        "hsa": Decimal(0),
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
        "pretax": Decimal(0),
        "roth": Decimal(0),
        "hsa": Decimal(0),
        "unknown": Decimal(0),
    }
    assert res.snapshot_date is None
