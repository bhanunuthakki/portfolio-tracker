"""Rule-grouped cashflow audit (services/cashflow_audit.py).

The point of this module is that a reviewer can check a few rules instead of a
few hundred rows, so the tests that matter are the ones pinning *collapse* —
that unrelated descriptions don't fragment a rule into dozens of groups — and
*ranking*, since sorting by row count instead of dollars buries the single
large mis-tag the audit exists to surface.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
    TransactionOverride,
)
from portfolio_tracker.services.cashflow_audit import build_rule_audit

D = date(2026, 1, 5)


def _seed(session: Session) -> None:
    session.add(
        Item(
            item_id=1,
            source="plaid",
            plaid_item_id="i1",
            institution_name="Broker",
            is_data_active=True,
        )
    )
    session.add(
        Account(
            account_id=10,
            item_id=1,
            plaid_account_id="a10",
            name="Brokerage",
            type="investment",
        )
    )
    session.add(
        Security(security_id=100, plaid_security_id="s100", ticker="AAA", name="AAA")
    )
    # One holdings row so the account counts as "valued" and its flows reach
    # the return — otherwise every group would be marked out of scope.
    session.add(
        HoldingSnapshot(
            snapshot_date=D,
            account_id=10,
            security_id=100,
            quantity=Decimal(1),
            institution_price=Decimal(1),
            institution_value=Decimal(1),
        )
    )
    session.flush()


def _tx(
    session: Session,
    tx_id: str,
    tx_type: str,
    subtype: str,
    amount: str,
    name: str,
    day: int = 5,
) -> None:
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id=tx_id,
            account_id=10,
            security_id=None,
            date=date(2026, 1, day),
            name=name,
            quantity=Decimal(0),
            amount=Decimal(amount),
            type=tx_type,
            subtype=subtype,
        )
    )


@pytest.fixture
def book(session: Session) -> Session:
    _seed(session)
    return session


class TestCollapse:
    def test_same_rule_different_securities_is_one_group(self, book: Session):
        # The failure this pins: keying on the description split "Cash dividend
        # from VTI" and "...from SGOV" into separate rules, turning 158 rows
        # into 119 groups — a summary as long as the list it replaced.
        for i, ticker in enumerate(["VTI", "SGOV", "VOO", "BN", "META"]):
            _tx(
                book,
                f"d{i}",
                "cash",
                "dividend",
                "-10.00",
                f"Cash dividend of $10.00 from {ticker}",
            )
        book.flush()

        groups = build_rule_audit(book).groups
        assert len(groups) == 1
        assert groups[0].count == 5
        assert groups[0].classification == "internal"
        assert groups[0].distinct_patterns == 5

    def test_same_rule_opposite_directions_stay_separate(self, book: Session):
        # Direction is a real distinction even though one rule produced both.
        _tx(book, "in1", "transfer", "transfer", "-500", "transfer - DEPOSIT USD")
        _tx(book, "out1", "transfer", "transfer", "500", "transfer - WITHDRAWAL USD")
        book.flush()

        classifications = {g.classification for g in build_rule_audit(book).groups}
        assert classifications == {"external_in", "external_out"}

    def test_trades_are_absent(self, book: Session):
        _tx(book, "b1", "buy", "buy", "1000", "buy 10 shares of AAA")
        _tx(book, "s1", "sell", "sell", "1000", "sell 10 shares of AAA")
        book.flush()

        assert build_rule_audit(book).groups == []


class TestRanking:
    def test_ranked_by_dollars_not_row_count(self, book: Session):
        # 20 tiny rows vs 1 large one. Count-ordering would bury the large one,
        # which is the row an audit exists to surface.
        for i in range(20):
            _tx(book, f"small{i}", "cash", "contribution", "50", "small deposit")
        _tx(book, "big", "transfer", "transfer", "-40000", "transfer - DEPOSIT USD")
        book.flush()

        groups = build_rule_audit(book).groups
        assert groups[0].count == 1
        assert Decimal(groups[0].net_cashflow) == Decimal(40000)
        assert groups[1].count == 20


class TestDecisionSource:
    def test_override_is_attributed_to_the_user(self, book: Session):
        _tx(book, "t1", "cash", "dividend", "-100", "Cash dividend of $100 from AAA")
        book.add(
            TransactionOverride(
                plaid_investment_transaction_id="t1", classification="external_in"
            )
        )
        book.flush()

        (group,) = build_rule_audit(book).groups
        assert group.decision_source == "override"
        assert group.classification == "external_in"

    def test_name_hint_is_attributed_to_the_description(self, book: Session):
        _tx(book, "t1", "transfer", "transfer", "-500", "transfer - DEPOSIT USD")
        book.flush()

        (group,) = build_rule_audit(book).groups
        assert group.decision_source == "name"

    def test_bare_transfer_is_attributed_to_the_amount_sign(self, book: Session):
        # No override, no direction word — the weakest rule, and the one the UI
        # flags, so it must be identified as such rather than lumped in with
        # subtype mappings.
        _tx(book, "t1", "transfer", "transfer", "-27606.47", "Direct rollover of 27606.47")
        book.flush()

        (group,) = build_rule_audit(book).groups
        assert group.decision_source == "sign"
        assert "sign" in group.reason

    def test_subtype_rule_is_attributed_to_the_subtype(self, book: Session):
        _tx(book, "t1", "cash", "contribution", "2500", "contribution")
        book.flush()

        (group,) = build_rule_audit(book).groups
        assert group.decision_source == "subtype"


class TestReturnScope:
    def test_account_without_holdings_is_marked_and_excluded_from_the_total(
        self, book: Session
    ):
        # A 401(k) that reports contributions but no positions is excluded from
        # the return math. Its rows still appear — hiding them would make this
        # view silently disagree with the transactions table — but they must
        # not move the net, or the audit wouldn't reconcile with the chart.
        book.add(
            Account(
                account_id=11,
                item_id=1,
                plaid_account_id="a11",
                name="401(k)",
                type="investment",
            )
        )
        book.add(
            InvestmentTransaction(
                plaid_investment_transaction_id="k1",
                account_id=11,
                security_id=None,
                date=D,
                name="contribution",
                quantity=Decimal(0),
                amount=Decimal(5000),
                type="cash",
                subtype="contribution",
            )
        )
        _tx(book, "t1", "cash", "contribution", "1000", "contribution")
        book.flush()

        audit = build_rule_audit(book)
        by_scope = {g.counts_toward_return: g for g in audit.groups}
        assert by_scope[False].count == 1
        assert Decimal(by_scope[False].net_cashflow) == Decimal(0)
        assert Decimal(audit.net_external_cashflow_in) == Decimal(1000)

    def test_group_carries_its_transaction_ids_for_bulk_retag(self, book: Session):
        _tx(book, "t1", "cash", "contribution", "100", "contribution", day=5)
        _tx(book, "t2", "cash", "contribution", "200", "contribution", day=6)
        book.flush()

        (group,) = build_rule_audit(book).groups
        assert sorted(group.transaction_ids) == ["t1", "t2"]
