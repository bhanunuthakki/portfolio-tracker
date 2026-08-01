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
    """A promotional credit is money the BROKER gave the account, not money the
    owner contributed. Tagging it `external_in` subtracts it from measured gain
    — the account is credited with cash while its performance is penalised for
    receiving it. A 2% bonus on a ~$150k ACATS is ~$3,000 of error, in the
    direction that understates returns."""

    def test_acats_transfer_bonus_is_internal(self):
        assert _classify_by_name("ACAT In Bonus Interest Payment") == "internal"

    def test_rollover_bonus_is_internal(self):
        assert _classify_by_name("IRA Rollover Bonus 1%") == "internal"

    def test_deposit_boost_is_internal_despite_the_word_deposit(self):
        # Would otherwise hit the "deposit" direction marker and be booked as
        # a contribution — the bonus rules run first for exactly this reason.
        assert _classify_by_name("Gold Deposit Boost Payment") == "internal"

    def test_fee_reimbursement_is_internal_despite_the_word_incoming(self):
        assert _classify_by_name("Incoming Acat Fee Reimbursement") == "internal"

    def test_a_real_deposit_is_still_external(self):
        # The bonus rules must not swallow genuine contributions.
        assert _classify_by_name("ACH deposit of $2000 into Robinhood") == "external_in"
        assert _classify_by_name("transfer - DEPOSIT Cash in USD") == "external_in"
