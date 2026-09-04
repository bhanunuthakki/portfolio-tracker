"""Unit + integration tests for the position-alpha service.

Covers the counterfactual P&L math, the `_qty_walk_back` reconstruction
(including the ACATS `acquired_at` zeroing), the M5 fix to
`_price_per_ticker_at_date` (prefer a positive close across a ticker's
security_ids), and a small end-to-end `compute_position_alpha`.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_tracker.models import (
    Account,
    Benchmark,
    CostBasisOverride,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Price,
    PriceAdjustmentBasis,
    PriceSource,
    Security,
    StockSplit,
)
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.position_alpha import (
    _basket_value_at,
    _BasketIndex,
    _counterfactual_pl,
    _last_known_price,
    _load_position_event_ledger,
    _matched_return_summary,
    _policy_benchmark_available,
    _price_per_ticker_at_date,
    _qty_walk_back,
    compute_position_alpha,
)

# ---------------------------------------------------------------------------
# _BasketIndex / _basket_value_at — the policy-basket counterfactual
#
# This path had no coverage while it was the endpoint's hot loop (it drove
# ~500k linear price scans per run). These tests pin both the as-of price
# semantics and the weight renormalization the index has to preserve.
# ---------------------------------------------------------------------------

_W = {"AAA": Decimal("0.6"), "BBB": Decimal("0.4")}
_CLOSES = {
    "AAA": {date(2025, 1, 2): Decimal(100), date(2025, 1, 6): Decimal(110)},
    "BBB": {date(2025, 1, 2): Decimal(50), date(2025, 1, 6): Decimal(45)},
}


def _split_adjusted_price(security_id: int, price_date: date, close: Decimal | int | str) -> Price:
    return Price(
        security_id=security_id,
        date=price_date,
        close=Decimal(close),
        source=PriceSource.YFINANCE.value,
        adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
    )


def _benchmark_bridge(
    symbol: str, start: date, end: date, close: Decimal | int | str
) -> list[Benchmark]:
    """Synthetic weekly closes between endpoint rows used by unit fixtures."""
    rows: list[Benchmark] = []
    current = start + timedelta(days=7)
    while current < end:
        rows.append(Benchmark(symbol=symbol, date=current, close=Decimal(close)))
        current += timedelta(days=7)
    return rows


def _position_price_bridge(
    security_id: int, start: date, end: date, close: Decimal | int | str
) -> list[Price]:
    rows: list[Price] = []
    current = start + timedelta(days=7)
    while current < end:
        rows.append(_split_adjusted_price(security_id, current, close))
        current += timedelta(days=7)
    return rows


def test_basket_index_uses_as_of_prices():
    """A date between closes resolves to the last close at or before it —
    identical to the `_last_known_price` scan it replaced."""
    index = _BasketIndex(_W, _CLOSES)
    prices = index.prices_on(date(2025, 1, 5))
    assert prices == {"AAA": Decimal(100), "BBB": Decimal(50)}
    # And it agrees with the original helper on every ticker.
    for ticker, series in _CLOSES.items():
        assert prices[ticker] == _last_known_price(series, date(2025, 1, 5))


def test_basket_index_omits_ticker_before_its_first_close():
    index = _BasketIndex(_W, _CLOSES)
    assert index.prices_on(date(2024, 12, 31)) == {}


def test_basket_value_at_grows_with_the_basket():
    # $1000 on 1/2 split 60/40: 6 shares AAA @100, 8 shares BBB @50.
    # On 1/6: 6*110 + 8*45 = 660 + 360 = 1020.
    index = _BasketIndex(_W, _CLOSES)
    value = _basket_value_at(Decimal(1000), index, date(2025, 1, 2), date(2025, 1, 6))
    assert value == Decimal(1020)


def test_basket_value_at_renormalizes_when_a_component_is_unpriceable():
    """An unpriceable component drops out and the remaining weights absorb
    the full capital — never silently under-invest."""
    closes = {"AAA": _CLOSES["AAA"]}  # BBB has no series at all
    index = _BasketIndex(_W, closes)
    value = _basket_value_at(Decimal(1000), index, date(2025, 1, 2), date(2025, 1, 6))
    # All $1000 into AAA: 10 shares @100 -> 10*110 = 1100.
    assert value == Decimal(1100)


def test_policy_benchmark_requires_a_priceable_component_at_both_endpoints():
    start = date(2025, 1, 2)
    end = date(2025, 1, 6)
    assert _policy_benchmark_available(_W, _CLOSES, start, end) is True
    assert _policy_benchmark_available(_W, {}, start, end) is False
    assert (
        _policy_benchmark_available(
            _W,
            {"AAA": {end: Decimal(110)}},
            start,
            end,
        )
        is False
    )


def test_matched_return_summary_weights_mixed_flows_and_rounds():
    start = date(2025, 1, 1)
    end = date(2025, 1, 11)
    result = _matched_return_summary(
        start_date=start,
        end_date=end,
        v_start=Decimal(1000),
        daily_cashflow={
            date(2025, 1, 3): Decimal(500),
            date(2025, 1, 8): Decimal(-200),
        },
        actual_pl=Decimal("123.45"),
        spy_pl=Decimal("100.00"),
        qqq_pl=None,
        policy_pl=None,
    )

    # 1000 + 500*(8/10) - 200*(3/10) = 1340.
    assert result.dietz_denominator == Decimal("1340.00")
    assert result.portfolio_return_pct == Decimal("9.2127")
    assert result.spy_return_pct == Decimal("7.4627")
    assert result.alpha_vs_spy_pct == Decimal("1.7500")
    assert result.qqq_return_pct is None


def test_basket_value_at_zero_capital_and_repeat_dates():
    index = _BasketIndex(_W, _CLOSES)
    assert _basket_value_at(Decimal(0), index, date(2025, 1, 2), date(2025, 1, 6)) == Decimal(0)
    # Repeated dates hit the memo; results must be identical, not merely close.
    first = _basket_value_at(Decimal(500), index, date(2025, 1, 2), date(2025, 1, 6))
    second = _basket_value_at(Decimal(500), index, date(2025, 1, 2), date(2025, 1, 6))
    assert first == second


def _active_account(session) -> Account:
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id, plaid_account_id="a-1", name="Taxable", type="investment"
    )
    session.add(account)
    session.flush()
    return account


# ---------------------------------------------------------------------------
# _counterfactual_pl — dollar-matched cashflow counterfactual
# ---------------------------------------------------------------------------


def test_counterfactual_pl_hold_only():
    # $1000 at price 100 = 10 shares; price rises to 110 → +$100.
    pl = _counterfactual_pl(
        v_start=Decimal(1000),
        buys=[],
        sells=[],
        closes={},
        start_price=Decimal(100),
        end_price=Decimal(110),
        bought_sum=Decimal(0),
        sold_sum=Decimal(0),
    )
    assert pl == Decimal(100)


def test_counterfactual_pl_with_midwindow_buy():
    buy_date = date(2025, 5, 15)
    # 10 shares from V_start; a $500 buy at price 125 adds 4 shares → 14.
    # End at 110 → 1540 value; net invested 1500 → P&L = 40.
    pl = _counterfactual_pl(
        v_start=Decimal(1000),
        buys=[(buy_date, Decimal(500))],
        sells=[],
        closes={buy_date: Decimal(125)},
        start_price=Decimal(100),
        end_price=Decimal(110),
        bought_sum=Decimal(500),
        sold_sum=Decimal(0),
    )
    assert pl == Decimal(40)


def test_counterfactual_pl_zero_start_price_is_safe():
    assert _counterfactual_pl(
        v_start=Decimal(1000),
        buys=[],
        sells=[],
        closes={},
        start_price=Decimal(0),
        end_price=Decimal(110),
        bought_sum=Decimal(0),
        sold_sum=Decimal(0),
    ) == Decimal(0)


# ---------------------------------------------------------------------------
# _qty_walk_back — reverse transactions from the anchor snapshot
# ---------------------------------------------------------------------------


def test_qty_walk_back_reverses_buy(session):
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    anchor = date(2025, 1, 10)
    target = date(2025, 1, 1)
    session.add(
        HoldingSnapshot(
            snapshot_date=anchor,
            account_id=account.account_id,
            security_id=aapl.security_id,
            quantity=Decimal(100),
        )
    )
    # A buy of 30 between target and anchor — walking back must undo it.
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-buy",
            account_id=account.account_id,
            security_id=aapl.security_id,
            date=date(2025, 1, 5),
            type="buy",
            quantity=Decimal(30),
            amount=Decimal(3000),
        )
    )
    session.commit()

    accts = active_account_ids(session)
    result = _qty_walk_back(session, anchor, target, accts)
    assert result == {"AAPL": Decimal(70)}


def test_qty_walk_back_normalizes_pre_split_quantity(session):
    # A 2:1 split sits between a pre-split buy and the anchor snapshot. The
    # snapshot (200) is post-split; the buy was recorded as 100 as-traded
    # (pre-split) shares. Reversing the raw 100 against 200 would leave a
    # phantom 100; scaled by the 2x split factor it reverses 200 -> 0.
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    anchor = date(2025, 1, 10)
    target = date(2025, 1, 1)
    session.add(
        HoldingSnapshot(
            snapshot_date=anchor,
            account_id=account.account_id,
            security_id=aapl.security_id,
            quantity=Decimal(200),  # post-split units
        )
    )
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-presplit-buy",
            account_id=account.account_id,
            security_id=aapl.security_id,
            date=date(2025, 1, 3),  # before the split
            type="buy",
            quantity=Decimal(100),  # as-traded, pre-split
            amount=Decimal(40000),
        )
    )
    session.add(
        StockSplit(security_id=aapl.security_id, split_date=date(2025, 1, 5), ratio=Decimal(2))
    )
    session.commit()

    accts = active_account_ids(session)
    result = _qty_walk_back(session, anchor, target, accts)
    assert result == {"AAPL": Decimal(0)}


def test_qty_walk_back_no_split_unchanged(session):
    # No split rows -> identity factor -> the original reversal (200 - 30 = 170).
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    anchor = date(2025, 1, 10)
    session.add(
        HoldingSnapshot(
            snapshot_date=anchor,
            account_id=account.account_id,
            security_id=aapl.security_id,
            quantity=Decimal(200),
        )
    )
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-buy",
            account_id=account.account_id,
            security_id=aapl.security_id,
            date=date(2025, 1, 5),
            type="buy",
            quantity=Decimal(30),
            amount=Decimal(3000),
        )
    )
    session.commit()
    accts = active_account_ids(session)
    assert _qty_walk_back(session, anchor, date(2025, 1, 1), accts) == {"AAPL": Decimal(170)}


def test_qty_walk_back_acats_acquired_at_zeroes_position(session):
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    anchor = date(2025, 1, 10)
    target = date(2025, 1, 1)
    session.add(
        HoldingSnapshot(
            snapshot_date=anchor,
            account_id=account.account_id,
            security_id=aapl.security_id,
            quantity=Decimal(100),
        )
    )
    # ACATS-in dated AFTER the window start: the user did not hold these
    # shares in this account at `target`, so the walk-back must zero them.
    session.add(
        CostBasisOverride(
            account_id=account.account_id,
            security_id=aapl.security_id,
            total_cost_basis=Decimal(5000),
            source="inferred_acats",
            acquired_at=date(2025, 1, 5),
        )
    )
    session.commit()

    accts = active_account_ids(session)
    result = _qty_walk_back(session, anchor, target, accts)
    assert result == {"AAPL": Decimal(0)}


def test_qty_walk_back_acats_acquired_on_or_before_target_is_kept(session):
    # Boundary guard: acquired_at == target is NOT after target, so the
    # position is retained (the `>` comparison must not be `>=`).
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    anchor = date(2025, 1, 10)
    target = date(2025, 1, 1)
    session.add(
        HoldingSnapshot(
            snapshot_date=anchor,
            account_id=account.account_id,
            security_id=aapl.security_id,
            quantity=Decimal(100),
        )
    )
    session.add(
        CostBasisOverride(
            account_id=account.account_id,
            security_id=aapl.security_id,
            total_cost_basis=Decimal(5000),
            source="inferred_acats",
            acquired_at=target,
        )
    )
    session.commit()

    accts = active_account_ids(session)
    result = _qty_walk_back(session, anchor, target, accts)
    assert result == {"AAPL": Decimal(100)}


# ---------------------------------------------------------------------------
# M5: _price_per_ticker_at_date prefers a positive close across security_ids
# ---------------------------------------------------------------------------


def test_price_per_ticker_prefers_positive_across_security_ids(session):
    target = date(2025, 6, 2)
    # Same ticker ingested twice (Plaid + SnapTrade). The first-iterated row
    # carries a 0 close; the second a real price. The function must return
    # the positive price, not break on the 0.
    nu_zero = Security(plaid_security_id="plaid:NU", ticker="NU", type="cs")
    session.add(nu_zero)
    session.flush()
    nu_real = Security(plaid_security_id="snaptrade:NU", ticker="NU", type="cs")
    session.add(nu_real)
    session.flush()
    # Ensure the zero-price row is iterated first.
    assert nu_zero.security_id < nu_real.security_id
    session.add(_split_adjusted_price(nu_zero.security_id, target, 0))
    session.add(_split_adjusted_price(nu_real.security_id, target, 100))
    session.commit()

    result = _price_per_ticker_at_date(session, ["NU"], target)
    assert result["NU"] == Decimal(100)


def test_price_per_ticker_prefers_freshest_eligible_close_across_security_ids(session):
    target = date(2025, 6, 10)
    older = Security(plaid_security_id="plaid:NU", ticker="NU", type="cs")
    fresher = Security(plaid_security_id="snaptrade:NU", ticker="NU", type="cs")
    session.add_all([older, fresher])
    session.flush()
    assert older.security_id < fresher.security_id
    session.add_all(
        [
            _split_adjusted_price(older.security_id, date(2025, 6, 2), 90),
            _split_adjusted_price(fresher.security_id, target, 100),
        ]
    )
    session.commit()

    result = _price_per_ticker_at_date(session, ["NU"], target, require_position_basis=True)

    assert result == {"NU": Decimal(100)}


def test_price_per_ticker_single_sid_unchanged(session):
    target = date(2025, 6, 2)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    session.add(_split_adjusted_price(aapl.security_id, target, "150.25"))
    session.commit()
    result = _price_per_ticker_at_date(session, ["AAPL"], target)
    assert result["AAPL"] == Decimal("150.25")


def test_position_price_lookup_rejects_non_matching_adjustment_basis(session):
    target = date(2025, 6, 2)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    session.add(
        Price(
            security_id=aapl.security_id,
            date=target,
            close=Decimal(100),
            source=PriceSource.YFINANCE.value,
            adjustment_basis=PriceAdjustmentBasis.RAW_UNADJUSTED.value,
        )
    )
    session.commit()

    assert _price_per_ticker_at_date(session, ["AAPL"], target, require_position_basis=True) == {}


# ---------------------------------------------------------------------------
# compute_position_alpha — end-to-end headline numbers
# ---------------------------------------------------------------------------


def test_compute_position_alpha_held_position_outperforms_spy(session):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()

    # Held 10 AAPL across the window, no trades. AAPL 100 -> 120 (+20%).
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1000),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1200),
            ),
            _split_adjusted_price(aapl.security_id, start, 100),
            *_position_price_bridge(aapl.security_id, start, end, 100),
            _split_adjusted_price(aapl.security_id, end, 120),
            # SPY 400 -> 440 (+10%): the counterfactual underperforms AAPL.
            Benchmark(symbol="SPY", date=start, close=Decimal(400)),
            *_benchmark_bridge("SPY", start, end, 400),
            Benchmark(symbol="SPY", date=end, close=Decimal(440)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    assert result.calculation_status == "available"
    assert result.calculation_reason_codes == []
    assert result.total_actual_pl == Decimal(200)  # 1200 - 1000
    assert result.total_spy_pl == Decimal(100)  # (1000/400)*440 - 1000
    assert result.total_alpha == Decimal(100)  # 200 - 100
    assert result.matched_returns.dietz_denominator == Decimal("1000.00")
    assert result.matched_returns.portfolio_return_pct == Decimal("20.0000")
    assert result.matched_returns.spy_return_pct == Decimal("10.0000")
    assert result.matched_returns.alpha_vs_spy_pct == Decimal("10.0000")
    assert result.matched_returns.qqq_return_pct is None
    assert result.matched_returns.alpha_vs_qqq_pct is None
    assert result.matched_returns.policy_return_pct is None
    assert result.matched_returns.alpha_vs_policy_pct is None
    assert (
        result.matched_returns.dietz_denominator
        * result.matched_returns.alpha_vs_spy_pct
        / Decimal(100)
        == result.total_alpha
    )
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.ticker == "AAPL"
    assert row.value_at_start == Decimal(1000)
    assert row.value_at_end == Decimal(1200)
    assert row.alpha == Decimal(100)
    assert row.incomplete is False


@pytest.mark.parametrize("invalid_end", [None, Decimal(0)])
def test_position_alpha_rejects_stale_or_nonpositive_primary_benchmark(
    session, invalid_end: Decimal | None
):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    rows: list[object] = [
        HoldingSnapshot(
            snapshot_date=start,
            account_id=account.account_id,
            security_id=aapl.security_id,
            quantity=Decimal(10),
            institution_value=Decimal(1000),
        ),
        HoldingSnapshot(
            snapshot_date=end,
            account_id=account.account_id,
            security_id=aapl.security_id,
            quantity=Decimal(10),
            institution_value=Decimal(1000),
        ),
        _split_adjusted_price(aapl.security_id, start, 100),
        _split_adjusted_price(aapl.security_id, end, 100),
        Benchmark(symbol="SPY", date=start, close=Decimal(100)),
    ]
    if invalid_end is not None:
        rows.append(Benchmark(symbol="SPY", date=end, close=invalid_end))
    session.add_all(rows)
    session.commit()

    result = compute_position_alpha(session, start, end)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["primary_benchmark_price_unavailable"]
    assert result.rows == []
    assert result.matched_returns.dietz_denominator is None


def test_position_alpha_rejects_stale_interior_position_marks(session):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1000),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1100),
            ),
            _split_adjusted_price(aapl.security_id, start, 100),
            _split_adjusted_price(aapl.security_id, end, 110),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            *_benchmark_bridge("SPY", start, end, 100),
            Benchmark(symbol="SPY", date=end, close=Decimal(105)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["position_price_unavailable"]
    assert result.matched_returns.dietz_denominator is None
    assert all(point.portfolio_return_pct is None for point in result.series)


def test_position_alpha_rejects_nonpositive_intermediate_dietz_denominator(session):
    start = date(2025, 1, 1)
    sell_date = date(2025, 1, 2)
    buy_date = date(2025, 1, 4)
    end = date(2025, 1, 6)
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(1),
                institution_value=Decimal(100),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(9),
                institution_value=Decimal(900),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-sell-beyond-opening",
                account_id=account.account_id,
                security_id=aapl.security_id,
                date=sell_date,
                type="sell",
                quantity=Decimal(2),
                amount=Decimal(200),
                price=Decimal(100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-recapitalise",
                account_id=account.account_id,
                security_id=aapl.security_id,
                date=buy_date,
                type="buy",
                quantity=Decimal(10),
                amount=Decimal(1000),
                price=Decimal(100),
            ),
            *(
                _split_adjusted_price(aapl.security_id, d, 100)
                for d in [start, sell_date, buy_date, end]
            ),
            *(
                Benchmark(symbol="SPY", date=d, close=Decimal(100))
                for d in [start, sell_date, buy_date, end]
            ),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["nonpositive_dietz_denominator"]
    assert result.matched_returns.dietz_denominator is None
    assert all(point.portfolio_return_pct is None for point in result.series)


def test_position_series_fails_closed_when_bought_ticker_has_no_price_until_end(session):
    start = date(2025, 5, 1)
    buy_date = date(2025, 5, 5)
    end = date(2025, 5, 10)
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="aapl", type="cs")
    session.add(aapl)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(0),
                institution_value=Decimal(0),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-buy-with-late-price",
                account_id=account.account_id,
                security_id=aapl.security_id,
                date=buy_date,
                type="buy",
                quantity=Decimal(10),
                price=Decimal(100),
                amount=Decimal(1000),
            ),
            # No eligible price exists from the buy date through the day before
            # the endpoint. Publishing that partial daily series as available
            # would treat the held position as worth zero on those dates.
            _split_adjusted_price(aapl.security_id, end, 110),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=end, close=Decimal(105)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    assert result.calculation_status == "unavailable"
    assert "position_price_unavailable" in result.calculation_reason_codes
    assert result.total_actual_pl is None
    assert result.matched_returns.dietz_denominator is None
    assert all(point.portfolio_return_pct is None for point in result.series)


def test_position_result_fails_closed_without_invested_position_capital(session):
    start = date(2025, 5, 1)
    end = date(2025, 5, 10)
    _active_account(session)
    session.add_all(
        [
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=end, close=Decimal(105)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["no_invested_position_capital"]
    assert result.rows == []
    assert result.matched_returns.dietz_denominator is None


def test_matched_returns_weight_midwindow_trade_and_reconcile_dollars(session):
    start = date(2025, 5, 1)
    mid = date(2025, 5, 6)
    end = date(2025, 5, 11)
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1000),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(15),
                institution_value=Decimal(1650),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-mid-buy",
                account_id=account.account_id,
                security_id=aapl.security_id,
                date=mid,
                type="buy",
                quantity=Decimal(5),
                amount=Decimal(500),
                price=Decimal(100),
            ),
            _split_adjusted_price(aapl.security_id, start, 100),
            _split_adjusted_price(aapl.security_id, mid, 100),
            _split_adjusted_price(aapl.security_id, end, 110),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=mid, close=Decimal(100)),
            Benchmark(symbol="SPY", date=end, close=Decimal(105)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    # D = 1000 + 500 * (5 remaining days / 10 total days) = 1250.
    assert result.total_actual_pl == Decimal("150.00")
    assert result.total_spy_pl == Decimal("75.00")
    assert result.total_alpha == Decimal("75.00")
    assert result.matched_returns.dietz_denominator == Decimal("1250.00")
    assert result.matched_returns.portfolio_return_pct == Decimal("12.0000")
    assert result.matched_returns.spy_return_pct == Decimal("6.0000")
    assert result.matched_returns.alpha_vs_spy_pct == Decimal("6.0000")
    assert (
        result.matched_returns.dietz_denominator
        * result.matched_returns.alpha_vs_spy_pct
        / Decimal(100)
        == result.total_alpha
    )
    assert result.series[-1].portfolio_return_pct == Decimal("12.0000")
    assert result.series[-1].spy_return_pct == Decimal("6.0000")


def test_start_date_trade_is_part_of_opening_value_not_a_second_cashflow(session):
    start = date(2025, 5, 1)
    end = date(2025, 5, 11)
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1000),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-opening-day-buy",
                account_id=account.account_id,
                security_id=aapl.security_id,
                date=start,
                type="buy",
                quantity=Decimal(10),
                amount=Decimal(1000),
            ),
            _split_adjusted_price(aapl.security_id, start, 100),
            _split_adjusted_price(aapl.security_id, end, 110),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=end, close=Decimal(105)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    assert result.rows[0].bought_in_window == Decimal("0.00")
    assert result.total_actual_pl == Decimal("100.00")
    assert result.matched_returns.dietz_denominator == Decimal("1000.00")
    assert result.matched_returns.portfolio_return_pct == Decimal("10.0000")


def test_price_trade_counterfactual_uses_raw_benchmark_close(session):
    # Position alpha deliberately excludes cash income and fees from the actual
    # leg, so its benchmark leg must also be price-only. Whole-account total
    # return remains available from the performance service.
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1000),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1200),
            ),
            _split_adjusted_price(aapl.security_id, start, 100),
            *_position_price_bridge(aapl.security_id, start, end, 100),
            _split_adjusted_price(aapl.security_id, end, 120),
            Benchmark(
                symbol="SPY", date=start, close=Decimal(400), total_return_close=Decimal(400)
            ),
            *_benchmark_bridge("SPY", start, end, 400),
            Benchmark(symbol="SPY", date=end, close=Decimal(440), total_return_close=Decimal(444)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    assert result.total_spy_pl == Decimal(100)
    assert result.total_alpha == Decimal(100)


def test_unmatched_in_kind_transfer_suppresses_matched_returns(session):
    start = date(2025, 5, 1)
    transfer_date = date(2025, 5, 20)
    end = date(2025, 6, 1)
    account = _active_account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(0),
                institution_value=Decimal(0),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=aapl.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-acat-in",
                account_id=account.account_id,
                security_id=aapl.security_id,
                date=transfer_date,
                type="cash",
                subtype="external_asset_transfer_in",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
            _split_adjusted_price(aapl.security_id, start, 100),
            _split_adjusted_price(aapl.security_id, end, 110),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            *_benchmark_bridge("SPY", start, end, 100),
            Benchmark(symbol="SPY", date=end, close=Decimal(105)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    assert result.calculation_status == "unavailable"
    assert "share_movement_unmatched" in result.calculation_reason_codes
    assert "share_movement_price_unavailable" in result.calculation_reason_codes
    assert result.total_actual_pl is None
    assert result.total_spy_pl is None
    assert result.total_alpha is None
    assert result.rows[0].actual_pl is None
    assert result.rows[0].spy_counterfactual_pl is None
    assert result.rows[0].alpha is None
    assert result.matched_returns.dietz_denominator is None
    assert result.matched_returns.portfolio_return_pct is None
    assert all(point.portfolio_return_pct is None for point in result.series)
    assert all(point.spy_counterfactual_value is None for point in result.series)


def test_same_day_opposite_transfer_legs_cancel_across_active_accounts(session):
    start = date(2025, 5, 1)
    transfer_date = date(2025, 5, 15)
    end = date(2025, 6, 1)
    first = _active_account(session)
    second = Account(
        item_id=first.item_id,
        plaid_account_id="a-2",
        name="IRA",
        type="investment",
    )
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add_all([second, security])
    session.flush()
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-acat-out",
                account_id=first.account_id,
                security_id=security.security_id,
                date=transfer_date,
                type="transfer",
                quantity=Decimal(-10),
                amount=Decimal(0),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-acat-in",
                account_id=second.account_id,
                security_id=security.security_id,
                date=transfer_date,
                type="transfer",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
        ]
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )
    assert ledger.calculation_reason_codes == ()
    assert ledger.events == ()


def test_same_day_transfer_cancels_across_provider_ids_after_split_normalization(session):
    start = date(2025, 5, 1)
    movement_date = date(2025, 5, 15)
    end = date(2025, 6, 1)
    first = _active_account(session)
    second = Account(
        item_id=first.item_id,
        plaid_account_id="a-2",
        name="IRA",
        type="investment",
    )
    old_sid = Security(plaid_security_id="plaid:aapl", ticker="aapl", type="cs")
    new_sid = Security(plaid_security_id="snaptrade:aapl", ticker="AAPL", type="cs")
    session.add_all([second, old_sid, new_sid])
    session.flush()
    session.add_all(
        [
            # Ten as-traded shares on old_sid become twenty current split units.
            StockSplit(
                security_id=old_sid.security_id,
                split_date=date(2025, 5, 20),
                ratio=Decimal(2),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-provider-out",
                account_id=first.account_id,
                security_id=old_sid.security_id,
                date=movement_date,
                type="transfer",
                quantity=Decimal(-10),
                amount=Decimal(0),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-provider-in",
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

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )
    assert ledger.calculation_reason_codes == ()
    assert ledger.events == ()


def test_partial_same_day_transfer_fails_closed(session):
    start = date(2025, 5, 1)
    movement_date = date(2025, 5, 15)
    end = date(2025, 6, 1)
    account = _active_account(session)
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(security)
    session.flush()
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-out",
                account_id=account.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="transfer",
                quantity=Decimal(-10),
                amount=Decimal(0),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-in",
                account_id=account.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="transfer",
                quantity=Decimal(6),
                amount=Decimal(0),
            ),
        ]
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )
    assert "share_movement_unmatched" in ledger.calculation_reason_codes
    assert "share_movement_price_unavailable" in ledger.calculation_reason_codes


def test_same_account_opposite_transfer_legs_do_not_cancel(session):
    start = date(2025, 5, 1)
    movement_date = date(2025, 5, 15)
    end = date(2025, 6, 1)
    account = _active_account(session)
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(security)
    session.flush()
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-reversal-out",
                account_id=account.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="transfer",
                quantity=Decimal(-10),
                amount=Decimal(0),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-reversal-in",
                account_id=account.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="transfer",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
        ]
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )

    assert "share_movement_unmatched" in ledger.calculation_reason_codes
    assert len(ledger.events) == 2


def test_cross_date_transfer_pair_fails_closed_even_when_net_zero(session):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(security)
    session.flush()
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-out",
                account_id=account.account_id,
                security_id=security.security_id,
                date=date(2025, 5, 14),
                type="transfer",
                quantity=Decimal(-10),
                amount=Decimal(0),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-in",
                account_id=account.account_id,
                security_id=security.security_id,
                date=date(2025, 5, 15),
                type="transfer",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
        ]
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )
    assert "share_movement_cross_date" in ledger.calculation_reason_codes
    assert "share_movement_unmatched" not in ledger.calculation_reason_codes


@pytest.mark.parametrize(
    ("tx_type", "subtype", "expected_reason"),
    [
        ("transfer", None, "share_movement_unmatched"),
        ("cash", "external_asset_transfer_in", "share_movement_unmatched"),
        ("cash", "rei", "share_movement_unclassified"),
        ("cash", "optionassignment", "share_movement_unclassified"),
        ("cash", "optionexpiration", "share_movement_unclassified"),
    ],
)
def test_known_non_trade_share_movements_fail_closed(
    session, tx_type: str, subtype: str | None, expected_reason: str
):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(security)
    session.flush()
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id=f"tx-{tx_type}-{subtype}",
            account_id=account.account_id,
            security_id=security.security_id,
            date=date(2025, 5, 15),
            type=tx_type,
            subtype=subtype,
            quantity=Decimal(1),
            amount=Decimal(0),
        )
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )
    assert expected_reason in ledger.calculation_reason_codes


def test_mixed_reinvestment_and_expiration_cannot_cancel(session):
    start = date(2025, 5, 1)
    movement_date = date(2025, 5, 15)
    end = date(2025, 6, 1)
    first = _active_account(session)
    second = Account(
        item_id=first.item_id,
        plaid_account_id="a-2",
        name="IRA",
        type="investment",
    )
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add_all([second, security])
    session.flush()
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-reinvestment",
                account_id=first.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="cash",
                subtype="rei",
                quantity=Decimal(1),
                amount=Decimal(0),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-expiration",
                account_id=second.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="cash",
                subtype="optionexpiration",
                quantity=Decimal(-1),
                amount=Decimal(0),
            ),
        ]
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )

    assert ledger.calculation_reason_codes == ("share_movement_unclassified",)
    assert len(ledger.events) == 2
    assert {event.quantity_delta for event in ledger.events} == {Decimal(-1), Decimal(1)}


@pytest.mark.parametrize(
    ("internal_subtype", "internal_name"),
    [
        ("assignment", None),
        ("exercise", None),
        ("merger", None),
        ("spin off", None),
        ("split", None),
        ("stock distribution", None),
        ("transfer", "Dividend reinvestment purchase of 1 share"),
        ("transfer", "DRIP purchase"),
    ],
)
def test_internal_share_event_cannot_cancel_against_transfer_leg(
    session,
    internal_subtype: str,
    internal_name: str | None,
):
    start = date(2025, 5, 1)
    movement_date = date(2025, 5, 15)
    end = date(2025, 6, 1)
    first = _active_account(session)
    second = Account(
        item_id=first.item_id,
        plaid_account_id="a-2",
        name="IRA",
        type="investment",
    )
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add_all([second, security])
    session.flush()
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id=f"tx-internal-{internal_subtype}",
                account_id=first.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="transfer",
                subtype=internal_subtype,
                name=internal_name,
                quantity=Decimal(1),
                amount=Decimal(0),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id=f"tx-external-{internal_subtype}",
                account_id=second.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="transfer",
                subtype="transfer",
                quantity=Decimal(-1),
                amount=Decimal(0),
            ),
        ]
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )

    assert "share_movement_unclassified" in ledger.calculation_reason_codes
    assert "share_movement_unmatched" in ledger.calculation_reason_codes
    assert len(ledger.events) == 2


def test_unknown_quantity_bearing_event_fails_closed(session):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(security)
    session.flush()
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-new-provider-event",
            account_id=account.account_id,
            security_id=security.security_id,
            date=date(2025, 5, 15),
            type="corporate_action",
            subtype="new_event",
            quantity=Decimal(1),
            amount=Decimal(0),
        )
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )
    assert "share_movement_unclassified" in ledger.calculation_reason_codes


@pytest.mark.parametrize(
    ("security_kind", "expected_reason"),
    [
        ("missing", "share_movement_missing_security"),
        ("blank_ticker", "share_movement_missing_ticker"),
    ],
)
def test_unresolvable_share_movement_identity_fails_closed(
    session, security_kind: str, expected_reason: str
):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    security_id: int | None = None
    if security_kind == "blank_ticker":
        security = Security(plaid_security_id="s-blank", ticker=None, type="cs")
        session.add(security)
        session.flush()
        security_id = security.security_id
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id=f"tx-{security_kind}",
            account_id=account.account_id,
            security_id=security_id,
            date=date(2025, 5, 15),
            type="transfer",
            quantity=Decimal(1),
            amount=Decimal(0),
        )
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )
    assert expected_reason in ledger.calculation_reason_codes


def test_trade_notional_uses_quantity_times_price_and_excludes_separate_fee(session):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(security)
    session.flush()
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-fee-bearing-buy",
            account_id=account.account_id,
            security_id=security.security_id,
            date=date(2025, 5, 15),
            type="buy",
            quantity=Decimal(2),
            price=Decimal(10),
            amount=Decimal(25),
            fees=Decimal(5),
        )
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )
    assert ledger.calculation_reason_codes == ()
    assert ledger.events[0].capital_flow == Decimal(20)


def test_trade_with_fee_and_no_execution_price_fails_closed(session):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(security)
    session.flush()
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-ambiguous-notional",
            account_id=account.account_id,
            security_id=security.security_id,
            date=date(2025, 5, 15),
            type="buy",
            quantity=Decimal(2),
            price=None,
            amount=Decimal(25),
            fees=Decimal(5),
        )
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )
    assert ledger.calculation_reason_codes == ("trade_notional_unavailable",)
    assert ledger.events[0].capital_flow == 0


def test_trade_amount_is_safe_fallback_only_when_fee_is_explicitly_zero(session):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(security)
    session.flush()
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-zero-fee-amount",
            account_id=account.account_id,
            security_id=security.security_id,
            date=date(2025, 5, 15),
            type="buy",
            quantity=Decimal(2),
            price=None,
            amount=Decimal(20),
            fees=Decimal(0),
        )
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )
    assert ledger.calculation_reason_codes == ()
    assert ledger.events[0].capital_flow == Decimal(20)


@pytest.mark.parametrize("security_type", ["derivative", "bond", None])
def test_trade_notional_fails_closed_without_direct_price_unit_contract(
    session, security_type: str | None
):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    security = Security(
        plaid_security_id=f"s-{security_type or 'unknown'}",
        ticker="OPT",
        type=security_type,
    )
    session.add(security)
    session.flush()
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id=f"tx-{security_type or 'unknown'}",
            account_id=account.account_id,
            security_id=security.security_id,
            date=date(2025, 5, 15),
            type="buy",
            quantity=Decimal(1),
            price=Decimal(2),
            amount=Decimal(200),
            fees=Decimal(0),
        )
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )

    assert ledger.calculation_reason_codes == ("trade_notional_unavailable",)
    assert ledger.events[0].capital_flow == 0


def test_closed_derivative_round_trip_cannot_bypass_endpoint_gate(session):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    option = Security(plaid_security_id="s-option", ticker="OPT", type="derivative")
    session.add(option)
    session.flush()
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-option-buy",
                account_id=account.account_id,
                security_id=option.security_id,
                date=date(2025, 5, 10),
                type="buy",
                quantity=Decimal(1),
                price=Decimal(2),
                amount=Decimal(200),
                fees=Decimal(0),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-option-sell",
                account_id=account.account_id,
                security_id=option.security_id,
                date=date(2025, 5, 20),
                type="sell",
                quantity=Decimal(1),
                price=Decimal(3),
                amount=Decimal(300),
                fees=Decimal(0),
            ),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            *_benchmark_bridge("SPY", start, end, 100),
            Benchmark(symbol="SPY", date=end, close=Decimal(110)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["trade_notional_unavailable"]
    assert result.total_actual_pl is None
    assert result.rows == []
    assert result.matched_returns.portfolio_return_pct is None


def test_non_quantity_income_and_fee_events_do_not_enter_position_ledger(session):
    start = date(2025, 5, 1)
    end = date(2025, 6, 1)
    account = _active_account(session)
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(security)
    session.flush()
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-dividend",
                account_id=account.account_id,
                security_id=security.security_id,
                date=date(2025, 5, 15),
                type="cash",
                subtype="dividend",
                quantity=Decimal(0),
                amount=Decimal(-10),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-fee",
                account_id=account.account_id,
                security_id=security.security_id,
                date=date(2025, 5, 16),
                type="fee",
                subtype="fee",
                quantity=Decimal(0),
                amount=Decimal(1),
            ),
        ]
    )
    session.commit()

    ledger = _load_position_event_ledger(
        session,
        start,
        end,
        active_account_ids(session),
        exclude_broad_index=False,
    )
    assert ledger.events == ()
    assert ledger.calculation_reason_codes == ()


def test_position_metric_keeps_split_adjusted_price_basis_under_broker_mark_drift(session):
    # Broker institution_value drifts from qty × the eligible yfinance Close.
    # Holdings may display that raw broker fact, but the derived price/trade
    # calculation must stay on one proven split-adjusted basis throughout.
    start = date(2025, 5, 1)
    mid = date(2025, 5, 15)
    end = date(2025, 6, 1)
    account = _active_account(session)
    nvo = Security(plaid_security_id="s-nvo", ticker="NVO", type="ad")
    session.add(nvo)
    session.flush()
    session.add_all(
        [
            # Eligible qty × Close is 1000 / 1000 / 1200; broker marks drift higher.
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=nvo.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1050),
            ),
            HoldingSnapshot(
                snapshot_date=mid,
                account_id=account.account_id,
                security_id=nvo.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1100),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=nvo.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1260),
            ),
            _split_adjusted_price(nvo.security_id, start, 100),
            *_position_price_bridge(nvo.security_id, start, end, 100),
            _split_adjusted_price(nvo.security_id, end, 120),
            Benchmark(symbol="SPY", date=start, close=Decimal(400)),
            *_benchmark_bridge("SPY", start, end, 400),
            Benchmark(symbol="SPY", date=end, close=Decimal(440)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    assert result.calculation_status == "available"
    assert result.v_start == Decimal("1000.00")
    assert result.v_end == Decimal("1200.00")

    by_date = {p.date: p for p in result.series}
    # Chart endpoints and interior values all consume the same eligible Close.
    assert by_date[start].portfolio_value == result.v_start
    assert by_date[end].portfolio_value == result.v_end
    assert by_date[mid].portfolio_value == Decimal("1000.00")
    # Every benchmark sleeve anchors to the same V_start.
    assert by_date[start].spy_counterfactual_value == Decimal("1000.00")
