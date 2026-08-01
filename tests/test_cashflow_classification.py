"""Cashflow direction inference (services/performance.py).

These are the rules the TWR / contributions pipeline depends on, classified by
NAME and subtype rather than the (inconsistent across brokers) amount sign.
"""

from __future__ import annotations

from portfolio_tracker.services.performance import (
    _classify_by_name,
    effective_classification,
)


class TestClassifyByName:
    def test_reinvestment_is_internal(self):
        assert _classify_by_name("Dividend reinvestment purchase of 2 shares") == "internal"

    def test_dividend_is_internal(self):
        # SoFi-via-Plaid mis-tags dividends as cash/withdrawal; the name saves us.
        assert _classify_by_name("cash - DIVIDEND USD") == "internal"

    def test_outgoing_is_external_out(self):
        assert _classify_by_name("Completed outgoing margin balance transfer") == "external_out"

    def test_deposit_is_external_in(self):
        assert _classify_by_name("ACH deposit from linked bank") == "external_in"

    def test_no_hint_returns_none(self):
        assert _classify_by_name("Brokerage rebalance") is None

    def test_empty_name_returns_none(self):
        assert _classify_by_name(None) is None
        assert _classify_by_name("") is None


class TestEffectiveClassification:
    def test_override_short_circuits(self):
        # Override is returned verbatim, regardless of subtype.
        assert effective_classification("cash", "withdrawal", "internal") == "internal"
        assert effective_classification("cash", "deposit", "external_out") == "external_out"

    def test_name_hint_applies_to_transfer_rows(self):
        assert (
            effective_classification(
                "transfer", "transfer", None, name="Completed outgoing transfer"
            )
            == "external_out"
        )

    def test_name_hint_ignored_for_non_cashflow_types(self):
        # A SELL of a fund whose name contains "dividend" must NOT be reclassified.
        assert (
            effective_classification("sell", None, None, name="Vanguard Dividend Appreciation ETF")
            is None
        )

    def test_buy_and_sell_are_not_cashflow(self):
        assert effective_classification("buy", None, None) is None
        assert effective_classification("sell", None, None) is None

    def test_internal_transfer_subtype_is_internal(self):
        # Corporate-action subtypes move shares without external cash effect.
        assert effective_classification("transfer", "merger", None) == "internal"
        assert effective_classification("transfer", "split", None) == "internal"


class TestBrokerBonuses:
    """A promotional credit arrived from OUTSIDE and is not investment
    performance. `external_in` subtracts it from the numerator while the
    synthetic benchmark receives the same cashflow — both books on the same
    capital base, so the comparison measures stock-picking rather than
    promotions. Owner ruling 2026-07-31; `internal` was tried first and
    flattered the 2-year return against SPY by 1.5pp."""

    def test_acats_transfer_bonus_is_a_contribution_not_interest(self):
        # Brokers compute these as interest and name them so; the bonus rule is
        # ordered ahead of the interest rule for exactly this row.
        assert _classify_by_name("ACAT In Bonus Interest Payment") == "external_in"

    def test_rollover_bonus_is_a_contribution(self):
        assert _classify_by_name("IRA Rollover Bonus 1%") == "external_in"

    def test_deposit_boost_is_attributed_to_the_bonus_rule(self):
        assert _classify_by_name("Gold Deposit Boost Payment") == "external_in"

    def test_fee_reimbursement_is_a_contribution(self):
        assert _classify_by_name("Incoming Acat Fee Reimbursement") == "external_in"

    def test_a_real_deposit_is_still_external(self):
        # The bonus rules must not swallow genuine contributions.
        assert _classify_by_name("ACH deposit of $2000 into Robinhood") == "external_in"
        assert _classify_by_name("transfer - DEPOSIT Cash in USD") == "external_in"
