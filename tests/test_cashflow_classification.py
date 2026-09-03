"""Cashflow direction inference (services/performance.py).

These are the rules the TWR / contributions pipeline depends on, classified by
NAME and subtype rather than the (inconsistent across brokers) amount sign.
"""

from __future__ import annotations

from portfolio_tracker.services.external_flow_ledger import (
    classify_by_name,
    effective_classification,
)


class TestClassifyByName:
    def test_reinvestment_is_internal(self):
        assert classify_by_name("Dividend reinvestment purchase of 2 shares") == "internal"

    def test_dividend_is_internal(self):
        # SoFi-via-Plaid mis-tags dividends as cash/withdrawal; the name saves us.
        assert classify_by_name("cash - DIVIDEND USD") == "internal"

    def test_outgoing_is_external_out(self):
        assert classify_by_name("Completed outgoing margin balance transfer") == "external_out"

    def test_deposit_is_external_in(self):
        assert classify_by_name("ACH deposit from linked bank") == "external_in"

    def test_no_hint_returns_none(self):
        assert classify_by_name("Brokerage rebalance") is None

    def test_empty_name_returns_none(self):
        assert classify_by_name(None) is None
        assert classify_by_name("") is None

    def test_owner_approved_broker_bonus_is_external_in(self):
        # Owner ruling: promotional consideration is contributed capital, not
        # investment income—even when the broker calls it an interest payment.
        assert classify_by_name("ACAT In Bonus Interest Payment") == "external_in"


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
