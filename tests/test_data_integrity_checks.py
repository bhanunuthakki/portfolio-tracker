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
    Price,
    Security,
    TransactionExclusion,
    TransactionOverride,
)
from portfolio_tracker.services.active_items import active_account_ids, valued_account_ids
from portfolio_tracker.services.data_quality import build_report
from portfolio_tracker.services.performance import (
    _daily_external_cashflows,  # pyright: ignore[reportPrivateUsage]
    _forward_values_from_snapshots,  # pyright: ignore[reportPrivateUsage]
    _is_start_value_unreliable,  # pyright: ignore[reportPrivateUsage]
    _is_transfer_shaped_fee,  # pyright: ignore[reportPrivateUsage]
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


class TestDuplicateContributionChain:
    """A payroll sweep records the same dollars twice: once into the plan's core
    account, again days later into its brokerage window. Counting both inflates
    contributions, and because Modified Dietz subtracts contributions from gain,
    it understates the return by the full duplicated amount."""

    def _chain(self, session: Session) -> list[object]:
        return [
            f
            for f in build_report(session).findings
            if f.category == "duplicate_contribution_chain"
        ]

    def _sweep(self, session: Session, core: int, legs: list[tuple[int, str]]) -> None:
        """Three biweekly contributions into `core`, each swept to `legs` 3 days later."""
        for week in range(3):
            day = D0 + timedelta(days=week * 14)
            total = sum(Decimal(share) for _acct, share in legs)
            _tx(
                session,
                f"core{week}",
                core,
                day,
                "cash",
                amount=float(total),
                subtype="contribution",
                name="contribution",
            )
            for idx, (acct, share) in enumerate(legs):
                _tx(
                    session,
                    f"leg{acct}-{idx}-{week}",
                    acct,
                    day + timedelta(days=3),
                    "cash",
                    amount=float(Decimal(share)),
                    subtype="contribution",
                    name="TRANSFERRED FROM TO BROKERAGE OPTION (Cash)",
                )

    def test_pass_through_with_unvalued_core_is_reported_as_safe(self, book: Session):
        # The live shape: the core account reports no holdings, so only the
        # brokerage leg reaches the return. Correct — but only by accident, so
        # the finding says so rather than staying silent.
        _account(book, 20, 1, "401(k) core")  # no snapshots -> not valued
        _account(book, 21, 1, "BrokerageLink")
        _security(book, 104, "FXAIX")
        for offset in range(7):
            _snap(book, D0 + timedelta(days=offset), 21, 104, 10)
        self._sweep(book, 20, [(21, "2812.02")])
        book.flush()

        (finding,) = self._chain(book)
        assert finding.severity == "info"
        assert finding.context["status"] == "neutralised"
        assert "No double count" in finding.detail

    def test_double_count_is_an_error_when_both_legs_are_valued(self, book: Session):
        # The latent failure this check exists for: if the core account ever
        # reports holdings it rejoins the cashflow series, both legs count, and
        # the only symptom is a return that quietly drifts low.
        _account(book, 20, 1, "401(k) core")
        _account(book, 21, 1, "BrokerageLink")
        _security(book, 104, "FXAIX")
        for offset in range(7):
            _snap(book, D0 + timedelta(days=offset), 20, 104, 5)  # core now valued
            _snap(book, D0 + timedelta(days=offset), 21, 104, 10)
        self._sweep(book, 20, [(21, "2812.02")])
        book.flush()

        (finding,) = self._chain(book)
        assert finding.severity == "error"
        assert finding.context["status"] == "double_counted"
        assert finding.context["duplicated_amount"] == "8436.06"

    def test_unrelated_deposit_in_the_same_week_does_not_break_matching(self, book: Session):
        # Live regression: a $50 SoFi transfer landed inside the sweep window
        # and overshot the window total, so a sum-everything test silently
        # found nothing at all. The match has to be a SUBSET.
        _account(book, 20, 1, "401(k) core")
        _account(book, 21, 1, "BrokerageLink")
        _security(book, 104, "FXAIX")
        for offset in range(7):
            _snap(book, D0 + timedelta(days=offset), 21, 104, 10)
        self._sweep(book, 20, [(21, "1831.08"), (21, "980.94")])
        for week in range(3):
            _tx(
                book,
                f"noise{week}",
                10,
                D0 + timedelta(days=week * 14 + 3),
                "cash",
                amount=50.0,
                subtype="deposit",
                name="unrelated deposit",
            )
        book.flush()

        (finding,) = self._chain(book)
        assert finding.context["matched_contributions"] == "3"

    def test_a_single_coincidence_is_not_reported(self, book: Session):
        # One cent-exact pair happens by chance. Only a recurring sweep is a
        # pipeline, and only a pipeline is worth the user's attention.
        _account(book, 20, 1, "401(k) core")
        _account(book, 21, 1, "BrokerageLink")
        _security(book, 104, "FXAIX")
        for offset in range(7):
            _snap(book, D0 + timedelta(days=offset), 21, 104, 10)
        _tx(book, "c1", 20, D0, "cash", amount=1000.0, subtype="contribution")
        _tx(
            book,
            "l1",
            21,
            D0 + timedelta(days=3),
            "cash",
            amount=1000.0,
            subtype="contribution",
        )
        book.flush()

        assert self._chain(book) == []


class TestUnlabelledTransferBonus:
    """SoFi paid eight transfer bonuses labelled only 'transfer - DEPOSIT USD'
    — no 'bonus' text anywhere — so both the subtype heuristic and the
    name-based bonus rule read them as owner contributions. $5,778.98 was being
    SUBTRACTED from investment gain. The exact percentage ratio to a recent
    ACATS arrival is the only available signal."""

    def _seed_transfer_in(self, session: Session, *, value_per_share: float, shares: float):
        _security(session, 110, "MELI")
        for offset in range(9):
            session.add(
                Price(
                    security_id=110,
                    date=D0 + timedelta(days=offset),
                    close=Decimal(str(value_per_share)),
                )
            )
        session.add(
            InvestmentTransaction(
                plaid_investment_transaction_id="acats-in",
                account_id=10,
                security_id=110,
                date=D0,
                name="ACATS in from Robinhood (MELI)",
                quantity=Decimal(str(shares)),
                amount=Decimal(0),
                type="cash",
                subtype="external_asset_transfer_in",
            )
        )

    def _findings(self, session: Session) -> list[object]:
        return [
            f for f in build_report(session).findings if f.category == "unlabelled_transfer_bonus"
        ]

    def test_two_percent_of_a_transfer_is_flagged(self, book: Session):
        # $10,000 transferred in; $200.00 lands 4 days later described only as
        # a deposit. Exactly 2.00% is not a coincidence.
        self._seed_transfer_in(book, value_per_share=100.0, shares=100.0)
        _tx(
            book,
            "bonus",
            10,
            D0 + timedelta(days=4),
            "transfer",
            amount=200.0,
            subtype="transfer",
            name="transfer - DEPOSIT USD",
        )
        book.flush()

        (finding,) = self._findings(book)
        assert finding.context["implied_rate"] == "0.0200"
        assert finding.context["matched_transfer_ticker"] == "MELI"

    def test_a_recurring_deposit_is_not_flagged(self, book: Session):
        # $50 biweekly alongside the same transfer — not a clean percentage of
        # anything, so it must be left alone.
        self._seed_transfer_in(book, value_per_share=100.0, shares=100.0)
        _tx(
            book,
            "recurring",
            10,
            D0 + timedelta(days=4),
            "transfer",
            amount=50.0,
            subtype="transfer",
            name="transfer - DEPOSIT USD",
        )
        book.flush()

        assert self._findings(book) == []

    def test_an_already_corrected_bonus_is_silent(self, book: Session):
        # Once re-tagged Internal it is no longer external_in, so it drops out.
        self._seed_transfer_in(book, value_per_share=100.0, shares=100.0)
        _tx(
            book,
            "bonus",
            10,
            D0 + timedelta(days=4),
            "transfer",
            amount=200.0,
            subtype="transfer",
            name="transfer - DEPOSIT USD",
        )
        book.add(
            TransactionOverride(plaid_investment_transaction_id="bonus", classification="internal")
        )
        book.flush()

        assert self._findings(book) == []


class TestExceptionDurability:
    """Corrections have to survive the things that routinely happen to this
    data: a re-ingest, a re-link, a provider changing its ids. A correction
    that quietly stops applying is worse than none — the numbers move and
    nothing says so."""

    def test_transfer_shaped_fee_is_cash_neutral_by_rule(self, book: Session):
        # SoFi maps an incoming ACATS to `fee/miscellaneous fee` carrying the
        # position's full market value. Treated as a fee, reversing it injects
        # that value into historical cash ($288,949 on the live book). The rule
        # covers it whether or not anyone remembered to delete the row — and it
        # covers the NEXT ACATS too.
        assert _is_transfer_shaped_fee("fee - TRANSFER IN VTI") is True
        assert _is_transfer_shaped_fee("fee - TRANSFER OUT BN") is True
        assert _is_transfer_shaped_fee("fee - MARGIN INTEREST USD") is False
        assert _is_transfer_shaped_fee(None) is False

    def test_excluded_transaction_is_not_re_ingested(self, book: Session):
        # The failure this prevents: deleting a bad row is undone by the next
        # backfill, because ingest is insert-if-absent on the provider id and
        # absence reads as "new".
        book.add(
            TransactionExclusion(
                plaid_investment_transaction_id="bad-row",
                reason="duplicate of the authoritative synthetic ACATS pair",
            )
        )
        book.flush()
        assert book.get(TransactionExclusion, "bad-row") is not None
        assert book.get(TransactionExclusion, "some-other-row") is None

    def test_orphaned_override_is_reported_as_an_error(self, book: Session):
        # An override pointing at a transaction that no longer exists is doing
        # nothing. The dangerous cause is a re-link: same trade, new id, ruling
        # orphaned, unclassified duplicate in its place.
        book.add(
            TransactionOverride(
                plaid_investment_transaction_id="vanished", classification="internal"
            )
        )
        book.flush()

        findings = [f for f in build_report(book).findings if f.category == "orphaned_correction"]
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert findings[0].context["orphan_count"] == "1"

    def test_a_live_override_is_not_reported(self, book: Session):
        _tx(book, "live", 10, D0, "cash", amount=100.0, subtype="contribution")
        book.add(
            TransactionOverride(plaid_investment_transaction_id="live", classification="internal")
        )
        book.flush()

        assert [f for f in build_report(book).findings if f.category == "orphaned_correction"] == []
