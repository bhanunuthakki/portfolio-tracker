"""Unit + integration tests for services/performance.py.

Three documented bug-prone areas:
  * `_modified_dietz_series` — the cumulative TWR-ish % with cashflow weighting.
  * `_reverse_transaction_quantity` / `_reverse_transaction_cash_delta` — the
    sign machine for the walk-back (many past sign-flip bugs).
  * `_backfill_values_from_transactions` — reconstructs historical V with a
    cash adjustment so V_start doesn't collapse on net deployment.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    Benchmark,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Price,
    PriceAdjustmentBasis,
    PriceSource,
    Security,
    StockSplit,
)
from portfolio_tracker.services import performance as performance_service
from portfolio_tracker.services.external_flow_ledger import classify_transaction_cashflow
from portfolio_tracker.services.performance import (
    _backfill_values_from_transactions,
    _modified_dietz_series,
    _money_flow_matched_value,
    _policy_matched_value,
    _reverse_transaction_cash_delta,
    _reverse_transaction_quantity,
    _share_transfer_external_cashflows,
    compute_performance_series,
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


def test_money_flow_match_does_not_double_count_start_date_cashflow():
    d0, d10 = date(2025, 1, 1), date(2025, 1, 11)
    out = _money_flow_matched_value(
        [d0, d10],
        Decimal(1000),
        {d0: Decimal(1000)},
        {d0: Decimal(100), d10: Decimal(110)},
    )
    # V_start is an end-of-day opening value. A start-day flow is already in
    # that value and must not create a second benchmark lot.
    assert out[d0] == Decimal(1000)
    assert out[d10] == Decimal(1100)


def test_money_flow_match_rejects_stale_benchmark_endpoint():
    start = date(2025, 1, 1)
    end = date(2025, 6, 1)

    assert (
        _money_flow_matched_value(
            [start, end],
            Decimal(1000),
            {},
            {start: Decimal(100)},
        )
        == {}
    )


def test_policy_match_does_not_double_count_start_date_cashflow():
    d0, d10 = date(2025, 1, 1), date(2025, 1, 11)
    out = _policy_matched_value(
        [d0, d10],
        Decimal(1000),
        {d0: Decimal(1000)},
        {"SPY": {d0: Decimal(100), d10: Decimal(110)}},
        {"SPY": Decimal(1)},
    )
    assert out[d0] == Decimal(1000)
    assert out[d10] == Decimal(1100)


# ---------------------------------------------------------------------------
# Cash-flow-matched benchmark books
# ---------------------------------------------------------------------------


def test_money_flow_matched_value_invests_flow_between_observation_dates():
    start = date(2025, 1, 1)
    flow_date = date(2025, 1, 2)
    end = date(2025, 1, 3)

    out = _money_flow_matched_value(
        [start, end],
        Decimal(1000),
        {flow_date: Decimal(550)},
        {
            start: Decimal(100),
            flow_date: Decimal(110),
            end: Decimal(121),
        },
    )

    # Initial capital buys 10 shares; the intervening flow buys 5 shares.
    assert out[end] == Decimal(1815)


def test_policy_matched_value_invests_flow_between_observation_dates():
    start = date(2025, 1, 1)
    flow_date = date(2025, 1, 2)
    end = date(2025, 1, 3)

    out = _policy_matched_value(
        [start, end],
        Decimal(1000),
        {flow_date: Decimal(550)},
        {
            "SPY": {
                start: Decimal(100),
                flow_date: Decimal(110),
                end: Decimal(121),
            }
        },
        {"SPY": Decimal(1)},
    )

    assert out[end] == Decimal(1815)


def test_money_flow_matched_value_excludes_start_flow_and_includes_end_flow():
    start = date(2025, 1, 1)
    end = date(2025, 1, 3)

    out = _money_flow_matched_value(
        [start, end],
        Decimal(1000),
        {
            start: Decimal(500),
            end: Decimal(-200),
        },
        {
            start: Decimal(100),
            end: Decimal(120),
        },
    )

    # The opening value already contains start-date activity. The end-date
    # withdrawal is part of (start, end] and sells benchmark shares at close.
    assert out[start] == Decimal(1000)
    assert out[end] == Decimal(1000)


def test_policy_matched_value_excludes_start_flow_and_includes_end_flow():
    start = date(2025, 1, 1)
    end = date(2025, 1, 3)

    out = _policy_matched_value(
        [start, end],
        Decimal(1000),
        {
            start: Decimal(500),
            end: Decimal(-200),
        },
        {"SPY": {start: Decimal(100), end: Decimal(120)}},
        {"SPY": Decimal(1)},
    )

    assert out[start] == Decimal(1000)
    assert out[end] == Decimal(1000)


def test_performance_series_uses_one_period_cashflow_set(monkeypatch, session):
    start = date(2025, 1, 1)
    flow_date = date(2025, 1, 2)
    end = date(2025, 1, 3)
    after_end = date(2025, 1, 4)

    monkeypatch.setattr(
        performance_service,
        "_daily_portfolio_value",
        lambda *_args: {start: Decimal(1000), end: Decimal(1300)},
    )
    monkeypatch.setattr(
        performance_service,
        "_daily_external_cashflow_assessment",
        lambda *_args: performance_service._CashflowAssessment(
            cashflows={
                start: Decimal(50),
                flow_date: Decimal(200),
                end: Decimal(-100),
                after_end: Decimal(900),
            },
            calculation_reason_codes=(),
        ),
    )
    closes = {
        start: Decimal(100),
        flow_date: Decimal(100),
        end: Decimal(100),
    }
    monkeypatch.setattr(
        performance_service,
        "_benchmark_series",
        lambda *_args: {"SPY": closes, "QQQ": closes},
    )
    monkeypatch.setattr(performance_service, "load_policy_weights", lambda *_args: {})
    monkeypatch.setattr(
        performance_service,
        "_earliest_observed_date",
        lambda *_args: start,
    )

    series = performance_service.compute_performance_series(session, start, end)
    final = series.points[-1]

    # Opening-date and post-ending-date flows are outside the end-of-day
    # window. The same +$100 net flow feeds the bridge and both flat-price
    # benchmark books, whose investment gain is therefore exactly zero.
    assert series.net_external_cashflow_in == Decimal(100)
    assert final.portfolio_value == Decimal(1300)
    assert final.spy_equivalent_value == Decimal(1100)
    assert final.qqq_equivalent_value == Decimal(1100)
    assert final.spy_return_pct == Decimal(0)
    assert final.qqq_return_pct == Decimal(0)
    assert final.portfolio_return_pct == (Decimal(200) / Decimal(1100)) * Decimal(100)


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


def test_reverse_cash_delta_uses_owner_approved_flow_direction():
    for name in ("ACAT Reimbursement", "Account Promo Reward"):
        tx = _tx("cash", amount=Decimal(100), subtype="withdrawal", name=name)
        decision = classify_transaction_cashflow(
            tx.type,
            tx.subtype,
            Decimal(tx.amount),
            name=tx.name,
        )

        assert decision is not None
        assert decision.classification == "external_in"
        assert decision.signed_external_amount == Decimal(100)
        assert _reverse_transaction_cash_delta(tx, frozenset()) == Decimal(-100)


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


def test_share_transfer_cashflow_nets_provider_ids_after_split_normalization(session):
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    session.add(item)
    session.flush()
    first = Account(item_id=item.item_id, plaid_account_id="a-1", name="Taxable", type="investment")
    second = Account(item_id=item.item_id, plaid_account_id="a-2", name="IRA", type="investment")
    old_sid = Security(plaid_security_id="plaid:aapl", ticker="aapl", type="cs")
    new_sid = Security(plaid_security_id="snaptrade:aapl", ticker="AAPL", type="cs")
    session.add_all([first, second, old_sid, new_sid])
    session.flush()
    movement_date = date(2025, 5, 15)
    session.add_all(
        [
            StockSplit(
                security_id=old_sid.security_id,
                split_date=date(2025, 5, 20),
                ratio=Decimal(2),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-out",
                account_id=first.account_id,
                security_id=old_sid.security_id,
                date=movement_date,
                type="cash",
                subtype="external_asset_transfer_out",
                quantity=Decimal(-10),
                amount=Decimal(0),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-in",
                account_id=second.account_id,
                security_id=new_sid.security_id,
                date=movement_date,
                type="cash",
                subtype="external_asset_transfer_in",
                quantity=Decimal(20),
                amount=Decimal(0),
            ),
        ]
    )
    session.commit()

    assert (
        _share_transfer_external_cashflows(
            session,
            date(2025, 5, 1),
            date(2025, 6, 1),
            frozenset({first.account_id, second.account_id}),
        )
        == {}
    )


def test_share_transfer_cashflow_requires_eligible_split_adjusted_price(session):
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    account = Account(item=item, plaid_account_id="a-1", name="Taxable", type="investment")
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add_all([item, account, security])
    session.flush()
    movement_date = date(2025, 5, 15)
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-in",
                account_id=account.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="cash",
                subtype="external_asset_transfer_in",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
            Price(
                security_id=security.security_id,
                date=movement_date,
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
        ]
    )
    session.commit()

    assert _share_transfer_external_cashflows(
        session,
        date(2025, 5, 1),
        date(2025, 6, 1),
        frozenset({account.account_id}),
    ) == {movement_date: Decimal(1000)}


def test_share_transfer_cashflow_uses_freshest_eligible_ticker_price(session):
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    account = Account(item=item, plaid_account_id="a-1", name="Taxable", type="investment")
    older = Security(plaid_security_id="plaid:nu", ticker="NU", type="cs")
    fresher = Security(plaid_security_id="snaptrade:nu", ticker="NU", type="cs")
    session.add_all([item, account, older, fresher])
    session.flush()
    movement_date = date(2025, 5, 15)
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-in-old-id",
                account_id=account.account_id,
                security_id=older.security_id,
                date=movement_date,
                type="cash",
                subtype="external_asset_transfer_in",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-in-new-id",
                account_id=account.account_id,
                security_id=fresher.security_id,
                date=movement_date,
                type="cash",
                subtype="external_asset_transfer_in",
                quantity=Decimal(1),
                amount=Decimal(0),
            ),
            Price(
                security_id=older.security_id,
                date=movement_date - timedelta(days=8),
                close=Decimal(90),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
            Price(
                security_id=fresher.security_id,
                date=movement_date,
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
        ]
    )
    session.commit()

    assert _share_transfer_external_cashflows(
        session,
        date(2025, 5, 1),
        date(2025, 6, 1),
        frozenset({account.account_id}),
    ) == {movement_date: Decimal(1100)}


def test_plain_zero_amount_share_transfer_is_valued_as_external_cashflow(session):
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    account = Account(item=item, plaid_account_id="a-1", name="Taxable", type="investment")
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add_all([item, account, security])
    session.flush()
    movement_date = date(2025, 5, 15)
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-plain-transfer-in",
                account_id=account.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="transfer",
                subtype="transfer",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
            Price(
                security_id=security.security_id,
                date=movement_date,
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
        ]
    )
    session.commit()

    assert _share_transfer_external_cashflows(
        session,
        date(2025, 5, 1),
        date(2025, 6, 1),
        frozenset({account.account_id}),
    ) == {movement_date: Decimal(1000)}


def test_whole_account_performance_fails_closed_on_unpriceable_share_transfer(client, session):
    start = date(2025, 5, 1)
    movement_date = date(2025, 5, 20)
    end = date(2025, 6, 1)
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    account = Account(item=item, plaid_account_id="a-1", name="Taxable", type="investment")
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add_all([item, account, security])
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=Decimal(100),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(11),
                institution_value=Decimal(1100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-unpriceable-transfer",
                account_id=account.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="transfer",
                subtype="transfer",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=end, close=Decimal(110)),
        ]
    )
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["external_share_movement_price_unavailable"]
    assert result.net_external_cashflow_in is None
    assert all(point.portfolio_return_pct is None for point in result.points)
    assert all(point.spy_return_pct is None for point in result.points)
    assert all(point.spy_equivalent_value is None for point in result.points)

    response = client.get(
        "/api/v1/analytics/performance",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
    )
    assert response.status_code == 200
    wire_series = response.json()["series"]
    assert wire_series["calculation_status"] == "unavailable"
    assert wire_series["calculation_reason_codes"] == ["external_share_movement_price_unavailable"]
    assert wire_series["net_external_cashflow_in"] is None


def test_whole_account_performance_uses_priceable_share_transfer_cashflow(session):
    start = date(2025, 5, 1)
    movement_date = date(2025, 5, 20)
    end = date(2025, 6, 1)
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    account = Account(item=item, plaid_account_id="a-1", name="Taxable", type="investment")
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add_all([item, account, security])
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=Decimal(100),
            ),
            HoldingSnapshot(
                snapshot_date=movement_date,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(11),
                institution_value=Decimal(1100),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(11),
                institution_value=Decimal(1100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-priceable-transfer",
                account_id=account.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="transfer",
                subtype="transfer",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
            Price(
                security_id=security.security_id,
                date=movement_date,
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=movement_date, close=Decimal(105)),
            Benchmark(symbol="SPY", date=end, close=Decimal(110)),
        ]
    )
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.calculation_status == "available"
    assert result.calculation_reason_codes == []
    assert result.net_external_cashflow_in == Decimal(1000)
    assert result.points[-1].portfolio_return_pct == 0
    assert result.points[-1].spy_return_pct is not None


def test_whole_account_performance_rejects_nonpositive_dietz_denominator(session):
    start = date(2025, 5, 1)
    end = date(2025, 5, 11)
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    account = Account(item=item, plaid_account_id="a-1", name="Taxable", type="investment")
    cash = Security(plaid_security_id="cash", ticker="CUR:USD", type="cash")
    session.add_all([item, account, cash])
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=cash.security_id,
                quantity=Decimal(0),
                institution_value=Decimal(0),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=cash.security_id,
                quantity=Decimal(1100),
                institution_value=Decimal(1100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="deposit-at-end",
                account_id=account.account_id,
                security_id=cash.security_id,
                date=end,
                type="cash",
                subtype="deposit",
                quantity=Decimal(1000),
                amount=Decimal(1000),
            ),
        ]
    )
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["nonpositive_dietz_denominator"]
    assert result.net_external_cashflow_in is None
    assert all(point.portfolio_return_pct is None for point in result.points)
