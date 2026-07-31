"""Regression tests for the feed-drift defects found on 2026-07-30.

The bug that motivated all of this: `daily_refresh` pulled Plaid HOLDINGS every
morning but never Plaid TRANSACTIONS, so Plaid-sourced accounts kept current
positions while their trade history froze at whatever date `jobs.backfill` was
last run by hand. Nothing looked broken — the item refreshed, V was right,
Modified Dietz reported sensible numbers — but every position-level engine saw
shares appear with no purchase behind them and booked the entire market value
as profit. "Actual P&L" read $56,655 against a real gain near $25k.

Each test here pins one of the four fixes so the failure mode can't return
silently.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
)
from portfolio_tracker.services.active_items import active_account_ids, valued_account_ids
from portfolio_tracker.services.data_quality import build_report
from portfolio_tracker.services.performance import (
    _daily_external_cashflows,  # pyright: ignore[reportPrivateUsage]
    _forward_values_from_snapshots,  # pyright: ignore[reportPrivateUsage]
    _is_start_value_unreliable,  # pyright: ignore[reportPrivateUsage]
    partial_snapshot_dates,
)

D0 = date(2026, 5, 9)


def _item(session: Session, item_id: int, *, active: bool = True) -> Item:
    item = Item(
        item_id=item_id,
        source="plaid",
        plaid_item_id=f"item-{item_id}",
        institution_name=f"Broker {item_id}",
        is_data_active=active,
    )
    session.add(item)
    return item


def _account(session: Session, account_id: int, item_id: int, name: str) -> Account:
    account = Account(
        account_id=account_id,
        item_id=item_id,
        plaid_account_id=f"acct-{account_id}",
        name=name,
        type="investment",
    )
    session.add(account)
    return account


def _security(session: Session, security_id: int, ticker: str, *, cash: bool = False) -> Security:
    security = Security(
        security_id=security_id,
        plaid_security_id=f"sec-{security_id}",
        ticker=ticker,
        name=ticker,
        is_cash_equivalent=cash,
    )
    session.add(security)
    return security


def _snap(
    session: Session,
    on: date,
    account_id: int,
    security_id: int,
    quantity: float,
    price: float = 100.0,
) -> None:
    session.add(
        HoldingSnapshot(
            snapshot_date=on,
            account_id=account_id,
            security_id=security_id,
            quantity=Decimal(str(quantity)),
            institution_price=Decimal(str(price)),
            institution_value=Decimal(str(quantity)) * Decimal(str(price)),
        )
    )


def _tx(
    session: Session,
    tx_id: str,
    account_id: int,
    on: date,
    tx_type: str,
    *,
    security_id: int | None = None,
    quantity: float = 0.0,
    amount: float = 0.0,
    subtype: str | None = None,
    name: str = "",
) -> None:
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id=tx_id,
            account_id=account_id,
            security_id=security_id,
            date=on,
            name=name,
            quantity=Decimal(str(quantity)),
            amount=Decimal(str(amount)),
            type=tx_type,
            subtype=subtype or tx_type,
        )
    )


@pytest.fixture
def book(session: Session) -> Session:
    """A two-account book with a week of complete daily snapshots."""
    _item(session, 1)
    _account(session, 10, 1, "Brokerage")
    _account(session, 11, 1, "IRA")
    _security(session, 100, "AAA")
    _security(session, 101, "BBB")
    for offset in range(7):
        on = D0 + timedelta(days=offset)
        _snap(session, on, 10, 100, 50)
        _snap(session, on, 11, 101, 20)
    session.flush()
    return session


class TestUnexplainedHoldingsChange:
    """The check that would have caught the dead transaction feed."""

    def _findings(self, session: Session) -> list[object]:
        return [
            f for f in build_report(session).findings if f.category == "unexplained_holdings_change"
        ]

    def test_shares_appearing_with_no_purchase_is_flagged(self, book: Session):
        # The UBER case: 200 shares materialise, no buy recorded.
        _snap(book, D0 + timedelta(days=7), 10, 100, 250)
        _snap(book, D0 + timedelta(days=7), 11, 101, 20)
        book.flush()

        findings = self._findings(book)
        assert len(findings) == 1
        assert "appeared without a purchase" in findings[0].title
        assert findings[0].context["unexplained_shares"] == "200.0000"

    def test_shares_vanishing_with_no_sale_is_flagged(self, book: Session):
        # The NVO case: half the position leaves, no sell recorded.
        _snap(book, D0 + timedelta(days=7), 10, 100, 25)
        _snap(book, D0 + timedelta(days=7), 11, 101, 20)
        book.flush()

        findings = self._findings(book)
        assert len(findings) == 1
        assert "left without a sale" in findings[0].title

    def test_change_backed_by_a_transaction_is_silent(self, book: Session):
        # Same 200-share move, but this time the buy is in the feed.
        on = D0 + timedelta(days=7)
        _snap(book, on, 10, 100, 250)
        _snap(book, on, 11, 101, 20)
        _tx(book, "t1", 10, on, "buy", security_id=100, quantity=200, amount=20000)
        book.flush()

        assert self._findings(book) == []

    def test_cash_balance_drift_is_exempt(self, book: Session):
        # A USD balance moves on every trade, dividend and fee; comparing it
        # against share-changing transactions would false-positive forever.
        _security(book, 102, "CUR:USD", cash=True)
        for offset in range(7):
            _snap(book, D0 + timedelta(days=offset), 10, 102, 5000, price=1.0)
        on = D0 + timedelta(days=7)
        _snap(book, on, 10, 100, 50)
        _snap(book, on, 11, 101, 20)
        _snap(book, on, 10, 102, 900, price=1.0)
        book.flush()

        assert self._findings(book) == []

    def test_fractional_drift_below_tolerance_is_silent(self, book: Session):
        # Fractional-share DRIP and broker rounding must not generate noise.
        on = D0 + timedelta(days=7)
        _snap(book, on, 10, 100, 50.2)
        _snap(book, on, 11, 101, 20)
        book.flush()

        assert self._findings(book) == []


class TestCashflowWithoutValue:
    """An account that deposits but never reports holdings distorts the return."""

    def test_orphan_account_is_excluded_from_return_math(self, book: Session):
        _account(book, 12, 1, "401(k)")
        _tx(
            book,
            "c1",
            12,
            D0 + timedelta(days=1),
            "cash",
            amount=2812.02,
            subtype="contribution",
            name="contribution",
        )
        book.flush()

        assert 12 in active_account_ids(book)
        assert 12 not in valued_account_ids(book)
        # Its contribution must not land in the cashflow series, or Modified
        # Dietz subtracts it from the numerator while its assets never join V.
        assert _daily_external_cashflows(book, D0, D0 + timedelta(days=6)) == {}

    def test_exclusion_is_reported_not_silent(self, book: Session):
        _account(book, 12, 1, "401(k)")
        _tx(
            book,
            "c1",
            12,
            D0 + timedelta(days=1),
            "cash",
            amount=2812.02,
            subtype="contribution",
        )
        book.flush()

        findings = [
            f for f in build_report(book).findings if f.category == "cashflow_without_value"
        ]
        assert len(findings) == 1
        assert findings[0].context["account_id"] == "12"

    def test_inert_account_produces_no_finding(self, book: Session):
        # Linked but with neither holdings nor transactions — can't distort
        # anything, so it shouldn't cost the user attention.
        _account(book, 13, 1, "Cash Management")
        book.flush()

        assert [
            f for f in build_report(book).findings if f.category == "cashflow_without_value"
        ] == []


class TestPartialSnapshotDays:
    """A day where only some accounts synced is not a portfolio value."""

    def test_partial_day_is_detected_and_dropped(self, book: Session):
        partial_day = D0 + timedelta(days=7)
        _snap(book, partial_day, 10, 100, 50)  # account 11 missing
        book.flush()

        assert partial_snapshot_dates(book, {partial_day}) == {partial_day: frozenset({11})}
        values = _forward_values_from_snapshots(book, D0, partial_day)
        assert partial_day not in values
        assert len(values) == 7

    def test_complete_days_survive(self, book: Session):
        values = _forward_values_from_snapshots(book, D0, D0 + timedelta(days=6))
        assert len(values) == 7
        assert all(v == Decimal(7000) for v in values.values())

    def test_account_linked_midway_does_not_falsify_earlier_days(self, book: Session):
        # Account 12 starts reporting on day 3. Days 0-2 are complete, not
        # partial — it simply wasn't linked yet.
        _security(book, 103, "CCC")
        for offset in range(3, 7):
            _snap(book, D0 + timedelta(days=offset), 12, 103, 5)
        _account(book, 12, 1, "New account")
        book.flush()

        candidates = {D0 + timedelta(days=o) for o in range(7)}
        assert partial_snapshot_dates(book, candidates) == {}

    def test_partial_day_is_reported(self, book: Session):
        partial_day = D0 + timedelta(days=7)
        _snap(book, partial_day, 10, 100, 50)
        book.flush()

        findings = [f for f in build_report(book).findings if f.category == "partial_snapshot_day"]
        assert len(findings) == 1
        assert findings[0].context["date"] == partial_day.isoformat()


class TestStartValueUnreliable:
    """The guard that read as verified but pinned nothing."""

    def test_modeled_start_is_unreliable_even_at_a_healthy_ratio(self):
        # The live miss: V_start $315,781 against V_end $698,112 is a ratio of
        # 0.45 — "fine" by the old rule — but the start predated every real
        # snapshot, so the whole series was reconstruction.
        assert _is_start_value_unreliable(
            Decimal(315781),
            Decimal(698112),
            date(2024, 1, 1),
            date(2026, 5, 9),
        )

    def test_observed_start_at_a_healthy_ratio_is_reliable(self):
        assert not _is_start_value_unreliable(
            Decimal(646629),
            Decimal(698112),
            D0,
            D0,
        )

    def test_no_observation_at_all_is_unreliable(self):
        assert _is_start_value_unreliable(Decimal(100), Decimal(120), D0, None)

    def test_collapsed_start_still_caught_without_dates(self):
        # Legacy two-argument callers keep the original ratio behaviour.
        assert _is_start_value_unreliable(Decimal(10), Decimal(100))
        assert not _is_start_value_unreliable(Decimal(90), Decimal(100))
