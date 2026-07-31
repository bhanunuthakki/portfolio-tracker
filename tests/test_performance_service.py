"""Unit + integration tests for services/performance.py.

Three documented bug-prone areas:
  * `_modified_dietz_series` — the cumulative TWR-ish % with cashflow weighting.
  * `_reverse_transaction_quantity` / `_reverse_transaction_cash_delta` — the
    sign machine for the walk-back (many past sign-flip bugs).
  * `_backfill_values_from_transactions` — reconstructs historical V with a
    cash adjustment so V_start doesn't collapse on net deployment.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Price,
    Security,
)
from portfolio_tracker.services.performance import (
    _backfill_values_from_transactions,
    _modified_dietz_series,
    _reverse_transaction_cash_delta,
    _reverse_transaction_quantity,
)


def _tx(
    tx_type: str,
    *,
    quantity: Decimal = Decimal(0),
    amount: Decimal = Decimal(0),
    subtype: str | None = None,
    security_id: int | None = None,
    name: str | None = None,
) -> InvestmentTransaction:
    return InvestmentTransaction(
        type=tx_type,
        quantity=quantity,
        amount=amount,
        subtype=subtype,
        security_id=security_id,
        name=name,
    )


# ---------------------------------------------------------------------------
# _modified_dietz_series
# ---------------------------------------------------------------------------


def test_modified_dietz_no_cashflow():
    d0, d10 = date(2025, 1, 1), date(2025, 1, 11)
    out = _modified_dietz_series(
        [d0, d10],
        {d0: Decimal(1000), d10: Decimal(1100)},
        {},
        Decimal(1000),
    )
    assert out[d0] == Decimal(0)
    assert out[d10] == Decimal(10)  # (1100 - 1000) / 1000 * 100


def test_modified_dietz_weights_midwindow_cashflow():
    d0, mid, d10 = date(2025, 1, 1), date(2025, 1, 6), date(2025, 1, 11)
    out = _modified_dietz_series(
        [d0, d10],
        {d0: Decimal(1000), d10: Decimal(1600)},
        {mid: Decimal(500)},  # deposit 5 days into a 10-day window
        Decimal(1000),
    )
    # num = 1600 - 1000 - 500 = 100
    # weighted_cf = 500 * 5/10 = 250 ; denom = 1000 + 250 = 1250
    # 100 / 1250 * 100 = 8.0
    assert out[d10] == Decimal(8)


def test_modified_dietz_nonpositive_denominator_is_zero():
    d0, d10 = date(2025, 1, 1), date(2025, 1, 11)
    out = _modified_dietz_series(
        [d0, d10],
        {d0: Decimal(100), d10: Decimal(200)},
        {},
        Decimal(0),  # base 0, no cashflow → denom 0
    )
    assert out[d10] == Decimal(0)


# ---------------------------------------------------------------------------
# _reverse_transaction_quantity
# ---------------------------------------------------------------------------


def test_reverse_quantity_buy_and_sell_use_type_not_sign():
    assert _reverse_transaction_quantity(_tx("buy", quantity=Decimal(30))) == Decimal(-30)
    assert _reverse_transaction_quantity(_tx("sell", quantity=Decimal(30))) == Decimal(30)


def test_reverse_quantity_transfer_uses_signed_quantity():
    # ACATS out (negative units) reverses to a positive add-back.
    assert _reverse_transaction_quantity(_tx("transfer", quantity=Decimal(-10))) == Decimal(10)
    assert _reverse_transaction_quantity(_tx("transfer", quantity=Decimal(10))) == Decimal(-10)


def test_reverse_quantity_share_moving_cash_subtypes():
    assert _reverse_transaction_quantity(
        _tx("cash", quantity=Decimal(-5), subtype="external_asset_transfer_out")
    ) == Decimal(5)
    assert _reverse_transaction_quantity(
        _tx("cash", quantity=Decimal(2), subtype="rei")
    ) == Decimal(-2)


def test_reverse_quantity_no_position_effect_returns_none():
    assert _reverse_transaction_quantity(_tx("buy", quantity=Decimal(0))) is None
    assert _reverse_transaction_quantity(_tx("fee", quantity=Decimal(1))) is None
    # Plain (non share-moving) cash event leaves positions untouched.
    assert (
        _reverse_transaction_quantity(_tx("cash", quantity=Decimal(7), subtype="deposit")) is None
    )


# ---------------------------------------------------------------------------
# _reverse_transaction_cash_delta
# ---------------------------------------------------------------------------


def test_reverse_cash_delta_buy_sell_use_magnitude():
    # BUY: cash was higher before → +m. SELL: cash was lower before → -m.
    assert _reverse_transaction_cash_delta(_tx("buy", amount=Decimal(500)), frozenset()) == Decimal(
        500
    )
    assert _reverse_transaction_cash_delta(
        _tx("sell", amount=Decimal(500)), frozenset()
    ) == Decimal(-500)


def test_reverse_cash_delta_fee_skipped_for_cash_equivalent():
    fee = _tx("fee", amount=Decimal(10), security_id=1)
    assert _reverse_transaction_cash_delta(fee, frozenset()) == Decimal(10)
    # A fee booked against the USD/MMF position is an internal sweep — skip it.
    assert _reverse_transaction_cash_delta(fee, frozenset({1})) == Decimal(0)


def test_reverse_cash_delta_internal_dividend_credit():
    # cash/dividend is income earned inside the portfolio → -m (cash was lower).
    assert _reverse_transaction_cash_delta(
        _tx("cash", amount=Decimal(20), subtype="dividend"), frozenset()
    ) == Decimal(-20)


def test_reverse_cash_delta_share_moving_is_cash_neutral():
    assert _reverse_transaction_cash_delta(
        _tx("cash", amount=Decimal(0), subtype="external_asset_transfer_in"), frozenset()
    ) == Decimal(0)


def test_reverse_cash_delta_external_flows():
    # Deposit: before it, cash was lower → -m.
    assert _reverse_transaction_cash_delta(
        _tx("cash", amount=Decimal(1000), subtype="deposit"), frozenset()
    ) == Decimal(-1000)
    # Withdrawal: before it, cash was higher → +m.
    assert _reverse_transaction_cash_delta(
        _tx("cash", amount=Decimal(200), subtype="withdrawal"), frozenset()
    ) == Decimal(200)


# ---------------------------------------------------------------------------
# _backfill_values_from_transactions — cash-adjustment keeps V_start intact
# ---------------------------------------------------------------------------


def test_backfill_cash_adjustment_prevents_vstart_collapse(session):
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id, plaid_account_id="a-1", name="Taxable", type="investment"
    )
    session.add(account)
    session.flush()
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs", is_cash_equivalent=False)
    session.add(aapl)
    session.flush()

    anchor = date(2025, 1, 10)
    session.add(
        HoldingSnapshot(
            snapshot_date=anchor,
            account_id=account.account_id,
            security_id=aapl.security_id,
            quantity=Decimal(10),
        )
    )
    # The account demonstrably exists from Jan 1 (a zero-dollar marker row —
    # the clamp keys on FIRST RECORDED TRANSACTION, and cash reconstructed for
    # dates before an account's first evidence of existing is definitionally
    # phantom: on the live book the two SoFi accounts "held" $29,856 in
    # September 2024, eight months before they were opened).
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-marker",
            account_id=account.account_id,
            security_id=None,
            date=date(2025, 1, 1),
            type="cash",
            subtype="dividend",
            quantity=Decimal(0),
            amount=Decimal(0),
        )
    )
    # Bought 4 shares for $400 on Jan 5. Walking back, positions drop to 6 but
    # the +$400 cash adjustment compensates: at a flat $100 price, V stays
    # $1000 every day (no fake ramp from the deployment).
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-buy",
            account_id=account.account_id,
            security_id=aapl.security_id,
            date=date(2025, 1, 5),
            type="buy",
            quantity=Decimal(4),
            amount=Decimal(400),
        )
    )
    for day in range(1, 11):
        session.add(
            Price(security_id=aapl.security_id, date=date(2025, 1, day), close=Decimal(100))
        )
    session.commit()

    result = _backfill_values_from_transactions(session, date(2025, 1, 1), date(2025, 1, 9))

    expected_dates = {date(2025, 1, day) for day in range(1, 10)}
    assert set(result.keys()) == expected_dates
    assert all(v == Decimal(1000) for v in result.values())
    # Pre-deployment day still values at 1000 (positions 6 × 100 + 400 cash).
    assert result[date(2025, 1, 4)] == Decimal(1000)
    # Post-deployment day: positions 10 × 100, cash adj 0.
    assert result[date(2025, 1, 9)] == Decimal(1000)


def test_backfill_no_phantom_cash_before_account_first_transaction(session):
    """An account contributes NO reconstructed cash before its first recorded
    transaction. Without the clamp, an account whose recorded lifetime flows
    don't balance projects the residue backward past its own opening as a
    standing idle balance — on the live book, $29,856 "held" by SoFi accounts
    that did not yet exist, which the broker's own all-time export refuted."""
    item = Item(source="plaid", plaid_item_id="itm-2", institution_name="RH", is_data_active=True)
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id, plaid_account_id="a-2", name="Opened Jan 5", type="investment"
    )
    session.add(account)
    session.flush()
    aapl = Security(plaid_security_id="s-aapl2", ticker="AAPL", type="cs", is_cash_equivalent=False)
    session.add(aapl)
    session.flush()

    anchor = date(2025, 1, 10)
    session.add(
        HoldingSnapshot(
            snapshot_date=anchor,
            account_id=account.account_id,
            security_id=aapl.security_id,
            quantity=Decimal(10),
        )
    )
    # First-ever transaction is the Jan 5 buy: before that date the account
    # has no evidence of existing, so its $400 "prior cash" must NOT appear.
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-buy2",
            account_id=account.account_id,
            security_id=aapl.security_id,
            date=date(2025, 1, 5),
            type="buy",
            quantity=Decimal(4),
            amount=Decimal(400),
        )
    )
    for day in range(1, 11):
        session.add(
            Price(security_id=aapl.security_id, date=date(2025, 1, day), close=Decimal(100))
        )
    session.commit()

    result = _backfill_values_from_transactions(session, date(2025, 1, 1), date(2025, 1, 9))

    # Before the account's first transaction: positions only (6 × $100),
    # no phantom $400 of pre-existence cash.
    assert result[date(2025, 1, 4)] == Decimal(600)
    # From the first transaction onward the cash adjustment applies normally.
    assert result[date(2025, 1, 9)] == Decimal(1000)
