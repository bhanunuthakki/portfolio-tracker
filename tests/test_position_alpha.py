"""Unit + integration tests for the position-alpha service.

Covers the counterfactual P&L math, the `_qty_walk_back` reconstruction
(including the ACATS `acquired_at` zeroing), the M5 fix to
`_price_per_ticker_at_date` (prefer a positive close across a ticker's
security_ids), and a small end-to-end `compute_position_alpha`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    Benchmark,
    CostBasisOverride,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Price,
    Security,
)
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.position_alpha import (
    _counterfactual_pl,
    _price_per_ticker_at_date,
    _qty_walk_back,
    compute_position_alpha,
)


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
    session.add(Price(security_id=nu_zero.security_id, date=target, close=Decimal(0)))
    session.add(Price(security_id=nu_real.security_id, date=target, close=Decimal(100)))
    session.commit()

    result = _price_per_ticker_at_date(session, ["NU"], target)
    assert result["NU"] == Decimal(100)


def test_price_per_ticker_single_sid_unchanged(session):
    target = date(2025, 6, 2)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    session.add(Price(security_id=aapl.security_id, date=target, close=Decimal("150.25")))
    session.commit()
    result = _price_per_ticker_at_date(session, ["AAPL"], target)
    assert result["AAPL"] == Decimal("150.25")


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
            Price(security_id=aapl.security_id, date=start, close=Decimal(100)),
            Price(security_id=aapl.security_id, date=end, close=Decimal(120)),
            # SPY 400 -> 440 (+10%): the counterfactual underperforms AAPL.
            Benchmark(symbol="SPY", date=start, close=Decimal(400)),
            Benchmark(symbol="SPY", date=end, close=Decimal(440)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    assert result.total_actual_pl == Decimal(200)  # 1200 - 1000
    assert result.total_spy_pl == Decimal(100)  # (1000/400)*440 - 1000
    assert result.total_alpha == Decimal(100)  # 200 - 100
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.ticker == "AAPL"
    assert row.value_at_start == Decimal(1000)
    assert row.value_at_end == Decimal(1200)
    assert row.alpha == Decimal(100)
    assert row.incomplete is False


def test_benchmark_counterfactual_uses_total_return_close(session):
    # Same setup as above, but the SPY benchmark carries a `total_return_close`
    # that reinvests dividends: price 400 -> 440 (+10%), total return 400 -> 444
    # (+11%). The counterfactual must use the total-return series, so the SPY
    # leg earns more and the user's alpha shrinks accordingly — removing the
    # ~dividend-yield over-credit a price-only comparison bakes in.
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
            Price(security_id=aapl.security_id, date=start, close=Decimal(100)),
            Price(security_id=aapl.security_id, date=end, close=Decimal(120)),
            Benchmark(
                symbol="SPY", date=start, close=Decimal(400), total_return_close=Decimal(400)
            ),
            Benchmark(symbol="SPY", date=end, close=Decimal(440), total_return_close=Decimal(444)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    # (1000 / 400) * 444 - 1000 = 110, NOT the price-only 100.
    assert result.total_spy_pl == Decimal(110)
    assert result.total_alpha == Decimal(90)  # 200 - 110, down from 100


def test_chart_series_reconciles_to_table_under_broker_close_drift(session):
    # Broker `institution_value` drifts from qty × Price.close (the realistic
    # case for ADRs / thin names). The table prefers the broker mark; the chart
    # series must reconcile to that SAME V_start / V_end, and interior snapshot
    # dates must show the broker mark too — not qty × yfinance close.
    start = date(2025, 5, 1)
    mid = date(2025, 5, 15)
    end = date(2025, 6, 1)
    account = _active_account(session)
    nvo = Security(plaid_security_id="s-nvo", ticker="NVO", type="ad")
    session.add(nvo)
    session.flush()
    session.add_all(
        [
            # qty × close would be 1000 / 1000 / 1200; broker marks drift higher.
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
            Price(security_id=nvo.security_id, date=start, close=Decimal(100)),
            Price(security_id=nvo.security_id, date=end, close=Decimal(120)),
            Benchmark(symbol="SPY", date=start, close=Decimal(400)),
            Benchmark(symbol="SPY", date=end, close=Decimal(440)),
        ]
    )
    session.commit()

    result = compute_position_alpha(session, start, end)

    # Table reports the broker-preferred V at both endpoints.
    assert result.v_start == Decimal("1050.00")
    assert result.v_end == Decimal("1260.00")

    by_date = {p.date: p for p in result.series}
    # Chart endpoints reconcile to the table (previously 1000 / 1200 from
    # qty × close — the mismatch this fix removes).
    assert by_date[start].portfolio_value == result.v_start
    assert by_date[end].portfolio_value == result.v_end
    # Interior snapshot date shows the broker mark, not qty × forward-filled close.
    assert by_date[mid].portfolio_value == Decimal("1100.00")
    # Every benchmark sleeve anchors to the same V_start.
    assert by_date[start].spy_counterfactual_value == Decimal("1050.00")
