"""Integration tests for the exit-quality / regret service."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    Benchmark,
    InvestmentTransaction,
    Item,
    Price,
    Security,
    StockSplit,
)
from portfolio_tracker.services.exit_quality import compute_exit_quality


def _account(session) -> Account:
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id, plaid_account_id="a-1", name="Taxable", type="investment"
    )
    session.add(account)
    session.flush()
    return account


def test_exit_quality_good_exit_name_fell_spy_rose(session):
    start, end = date(2025, 5, 1), date(2025, 6, 1)
    account = _account(session)
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add(aapl)
    session.flush()
    # Sold 10 @ $120 ($1200 proceeds) on 5/15; today AAPL is $100 (it fell).
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-sell",
            account_id=account.account_id,
            security_id=aapl.security_id,
            date=date(2025, 5, 15),
            type="sell",
            quantity=Decimal(10),
            amount=Decimal(1200),
        )
    )
    session.add(Price(security_id=aapl.security_id, date=end, close=Decimal(100)))
    session.add_all(
        [
            Benchmark(symbol="SPY", date=date(2025, 5, 15), close=Decimal(400)),
            Benchmark(symbol="SPY", date=end, close=Decimal(440)),
        ]
    )
    session.commit()

    result = compute_exit_quality(session, start, end)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.ticker == "AAPL"
    assert row.sold_shares == Decimal("10.0000")
    assert row.sold_proceeds == Decimal("1200.00")
    assert row.avg_sell_price == Decimal("120.0000")
    assert row.value_if_held == Decimal("1000.00")  # 10 × 100
    assert row.regret_vs_hold == Decimal("-200.00")  # sold for more than it's worth now
    assert row.spy_value_if_reinvested == Decimal("1320.00")  # 1200 × 440/400
    assert row.exit_alpha_vs_spy == Decimal("320.00")  # 1320 − 1000, good exit vs market
    assert row.still_held is False
    assert row.incomplete is False
    assert result.total_regret_vs_hold == Decimal("-200.00")


def test_exit_quality_normalizes_sold_shares_across_split(session):
    start, end = date(2025, 5, 1), date(2025, 6, 1)
    account = _account(session)
    nv = Security(plaid_security_id="s-nv", ticker="NV", type="cs")
    session.add(nv)
    session.flush()
    # Sold 10 pre-split shares on 5/10; a 2:1 split on 5/20; today's price is
    # the split-adjusted $50. value_if_held must use 20 today-shares, not 10.
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-sell",
            account_id=account.account_id,
            security_id=nv.security_id,
            date=date(2025, 5, 10),
            type="sell",
            quantity=Decimal(10),
            amount=Decimal(1000),
        )
    )
    session.add(
        StockSplit(security_id=nv.security_id, split_date=date(2025, 5, 20), ratio=Decimal(2))
    )
    session.add(Price(security_id=nv.security_id, date=end, close=Decimal(50)))
    session.add_all(
        [
            Benchmark(symbol="SPY", date=date(2025, 5, 10), close=Decimal(400)),
            Benchmark(symbol="SPY", date=end, close=Decimal(400)),
        ]
    )
    session.commit()

    row = compute_exit_quality(session, start, end).rows[0]
    assert row.sold_shares == Decimal("20.0000")  # 10 pre-split × 2
    assert row.value_if_held == Decimal("1000.00")  # 20 × 50, not 10 × 50


def test_exit_quality_empty_when_no_sells(session):
    _account(session)
    assert compute_exit_quality(session, date(2025, 1, 1), date(2025, 6, 1)).rows == []
