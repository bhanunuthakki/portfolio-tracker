"""SoFi option direction is recovered from broker fills or reported, never assumed."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from portfolio_tracker.jobs import snapshot
from portfolio_tracker.models import (
    Account,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
)
from portfolio_tracker.plaid_client import (
    HoldingsResponse,
    PlaidAccount,
    PlaidHolding,
    PlaidSecurity,
)
from portfolio_tracker.services import data_quality
from portfolio_tracker.services.option_direction import (
    OptionTrade,
    Verdict,
    apply_inferred_short_signs,
    reconcile_direction,
)

CALL = "TEST270319C00055000"
DAY = date(2026, 11, 10)
PRICE = Decimal("2.00")


def trade(day, kind, contracts, price=PRICE, subtype=None, units_per=100):
    sign = -1 if kind == "sell" else 1
    qty = Decimal(contracts * units_per) * sign
    return OptionTrade(
        trade_date=date(2026, 8, day),
        type=kind,
        subtype=subtype or kind,
        quantity=qty,
        amount=-qty * price if price else Decimal(1),
        price=price,
    )


@pytest.mark.parametrize(
    ("held", "trades", "verdict"),
    [
        (200, [trade(1, "sell", 2)], Verdict.SHORT_CONFIRMED),
        (200, [trade(1, "buy", 2)], Verdict.LONG_CONFIRMED),
        # Partial close: sold 3, bought back 1.
        (200, [trade(1, "sell", 3), trade(2, "buy", 1)], Verdict.SHORT_CONFIRMED),
        # Close, then reopen as a long in the same contract.
        (
            100,
            [trade(1, "sell", 2), trade(2, "buy", 2), trade(3, "buy", 1)],
            Verdict.LONG_CONFIRMED,
        ),
        # A flat history is not a short.
        (200, [trade(1, "sell", 2), trade(2, "buy", 2)], Verdict.NET_MISMATCH),
        (200, [], Verdict.NO_HISTORY),
        # Holding larger than visible history: pre-history position.
        (300, [trade(1, "sell", 2)], Verdict.NET_MISMATCH),
        (200, [trade(1, "sell", 2, price=None)], Verdict.UNUSABLE_HISTORY),
        (
            200,
            [OptionTrade(date(2026, 8, 1), "sell", "sell", Decimal(-2), Decimal(-999), PRICE)],
            Verdict.UNUSABLE_HISTORY,
        ),
        (200, [trade(1, "sell", 2), trade(2, "cancel", 1)], Verdict.UNUSABLE_HISTORY),
    ],
)
def test_reconcile_direction(held, trades, verdict):
    assert reconcile_direction(Decimal(held), trades).verdict is verdict


def test_contradictory_sign_is_unusable():
    wrong = trade(1, "sell", 2)
    flipped = OptionTrade(
        wrong.trade_date, "sell", "sell", -wrong.quantity, wrong.amount, wrong.price
    )
    assert reconcile_direction(Decimal(200), [flipped]).verdict is Verdict.UNUSABLE_HISTORY


def test_expiry_and_reingested_unit_duplicates():
    # The same fill stored once in contracts and once in underlying units.
    legacy = OptionTrade(date(2026, 8, 1), "sell", "sell", Decimal(-2), Decimal(400), PRICE)
    assert (
        reconcile_direction(Decimal(200), [trade(1, "sell", 2), legacy]).verdict
        is Verdict.SHORT_CONFIRMED
    )
    # Two genuine identical fills are not collapsed.
    assert (
        reconcile_direction(Decimal(400), [trade(1, "sell", 2), trade(1, "sell", 2)]).verdict
        is Verdict.SHORT_CONFIRMED
    )
    assigned = trade(5, "transfer", 2, subtype="assignment")
    assert reconcile_direction(Decimal(200), [trade(1, "sell", 4), assigned]).verdict in (
        Verdict.SHORT_CONFIRMED,
    )


def seed(session, monkeypatch, institution="SoFi", positive=(200, 200)):
    item = Item(
        source="plaid",
        plaid_item_id="synthetic",
        plaid_access_token_encrypted="synthetic",
        institution_name=institution,
    )
    session.add(item)
    session.flush()
    monkeypatch.setattr(snapshot, "decrypt_token", lambda _: "synthetic")
    monkeypatch.setattr(snapshot, "load_short_call_overrides", lambda: ())
    response = HoldingsResponse(
        accounts=[
            PlaidAccount(plaid_account_id=key, name=key, type="investment", mask=mask)
            for key, mask in (("acct-a", "0001"), ("acct-b", "0002"))
        ],
        securities=[PlaidSecurity(plaid_security_id="opt", ticker=CALL)],
        holdings=[
            PlaidHolding(
                plaid_account_id=account,
                plaid_security_id="opt",
                quantity=Decimal(quantity),
                quantity_unit="underlying_units",
                institution_price=Decimal("0.41"),
                institution_value=Decimal(quantity) * Decimal("0.41"),
                cost_basis=Decimal(120),
            )
            for account, quantity in zip(("acct-a", "acct-b"), positive, strict=True)
        ],
        item_id="synthetic",
    )
    monkeypatch.setattr(snapshot.plaid_client, "get_holdings", lambda _: response)
    return item


def stored(session):
    return {
        account: (row.quantity, row.institution_value, row.cost_basis)
        for row, account in session.execute(
            select(HoldingSnapshot, Account.plaid_account_id)
            .join(Account, Account.account_id == HoldingSnapshot.account_id)
            .where(HoldingSnapshot.snapshot_date == DAY)
        )
    }


def add_fills(session, plaid_account, *fills):
    account = session.scalar(select(Account).where(Account.plaid_account_id == plaid_account))
    security = session.scalar(select(Security).where(Security.ticker == CALL))
    for index, fill in enumerate(fills):
        session.add(
            InvestmentTransaction(
                plaid_investment_transaction_id=f"{plaid_account}-{index}",
                account_id=account.account_id,
                security_id=security.security_id,
                date=fill.trade_date,
                quantity=fill.quantity,
                amount=fill.amount,
                price=fill.price,
                type=fill.type,
                subtype=fill.subtype,
            )
        )
    session.flush()


def test_snapshot_corrects_only_the_account_with_short_history(session, monkeypatch):
    item = seed(session, monkeypatch)
    assert snapshot._snapshot_item(session, item, DAY) == 2
    # No fills yet: both rows stay as delivered and are flagged.
    assert stored(session)["acct-a"][0] == 200
    findings = data_quality._find_unconfirmed_option_direction(session)
    assert {f.severity for f in findings} == {"warning"}
    assert len(findings) == 2

    add_fills(session, "acct-a", trade(1, "sell", 2))
    for _ in range(2):  # a rerun reaches the same state
        snapshot._snapshot_item(session, item, DAY)
        session.flush()
        rows = stored(session)
        assert rows["acct-a"] == (-200, Decimal("-82.00"), -120)
        assert rows["acct-b"] == (200, Decimal("82.00"), 120)
    assert apply_inferred_short_signs(session, DAY) == 0

    kinds = {
        (f.context["account_mask"], f.severity)
        for f in data_quality._find_unconfirmed_option_direction(session)
    }
    assert kinds == {("0001", "info"), ("0002", "warning")}


def test_long_history_keeps_sign_and_raises_no_finding(session, monkeypatch):
    item = seed(session, monkeypatch, positive=(200, 200))
    snapshot._snapshot_item(session, item, DAY)
    add_fills(session, "acct-a", trade(1, "buy", 2))
    add_fills(session, "acct-b", trade(1, "sell", 3), trade(2, "buy", 1), trade(3, "sell", 2))
    snapshot._snapshot_item(session, item, DAY)
    rows = stored(session)
    assert rows["acct-a"][0] == 200
    assert rows["acct-b"][0] == 200  # net short 4 contracts != 2 held: not corrected
    severities = {
        f.context["account_mask"]: f.severity
        for f in data_quality._find_unconfirmed_option_direction(session)
    }
    assert severities == {"0002": "warning"}


def test_signed_feeds_are_authoritative(session, monkeypatch):
    item = seed(session, monkeypatch, institution="Robinhood")
    snapshot._snapshot_item(session, item, DAY)
    add_fills(session, "acct-a", trade(1, "sell", 2))
    snapshot._snapshot_item(session, item, DAY)
    assert stored(session)["acct-a"][0] == 200
    assert data_quality._find_unconfirmed_option_direction(session) == []


def test_negative_feed_value_contradicting_fills_is_an_error(session, monkeypatch):
    item = seed(session, monkeypatch, positive=(-200, -200))
    snapshot._snapshot_item(session, item, DAY)
    add_fills(session, "acct-a", trade(1, "buy", 2))
    errors = [
        f for f in data_quality._find_unconfirmed_option_direction(session) if f.severity == "error"
    ]
    assert [f.context["account_mask"] for f in errors] == ["0001"]
