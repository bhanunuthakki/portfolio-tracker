"""HTTP surface for GET /api/v1/portfolio/positions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    HoldingSnapshot,
    Item,
    Security,
)

_D = date(2025, 6, 2)


def _seed(session) -> None:
    """A book spanning all four tax buckets, with one name across two accounts."""
    item = Item(source="plaid", plaid_item_id="i1", institution_name="Brk", is_data_active=True)
    session.add(item)
    session.flush()

    brokerage = Account(
        item_id=item.item_id,
        plaid_account_id="a-brk",
        name="Taxable Brokerage",
        type="brokerage",
        subtype="brokerage",
    )
    roth = Account(
        item_id=item.item_id,
        plaid_account_id="a-roth",
        name="Roth IRA",
        type="investment",
        subtype="roth ira",
    )
    k401 = Account(
        item_id=item.item_id,
        plaid_account_id="a-401k",
        name="Workplace 401k",
        type="investment",
        subtype="401k",
    )
    individual = Account(
        item_id=item.item_id,
        plaid_account_id="a-ind",
        name="Self-directed",
        type="investment",
        subtype="individual",  # bare individual -> unknown in the 4-way contract
    )
    session.add_all([brokerage, roth, k401, individual])
    session.flush()

    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs", is_cash_equivalent=False)
    msft = Security(plaid_security_id="s-msft", ticker="MSFT", type="cs", is_cash_equivalent=False)
    tsla = Security(plaid_security_id="s-tsla", ticker="TSLA", type="cs", is_cash_equivalent=False)
    session.add_all([aapl, msft, tsla])
    session.flush()

    def snap(acct: Account, sec: Security, qty: str, value: str) -> HoldingSnapshot:
        return HoldingSnapshot(
            snapshot_date=_D,
            account_id=acct.account_id,
            security_id=sec.security_id,
            quantity=Decimal(qty),
            institution_value=Decimal(value),
            cost_basis=Decimal(value),
        )

    session.add_all(
        [
            snap(brokerage, aapl, "60", "6000"),  # AAPL taxable lot
            snap(roth, aapl, "40", "4000"),  # AAPL tax_free lot
            snap(k401, msft, "10", "5000"),  # MSFT tax_deferred
            snap(individual, tsla, "20", "5000"),  # TSLA unknown
        ]
    )
    session.commit()


def test_positions_v1(client, session):
    _seed(session)
    resp = client.get("/api/v1/portfolio/positions")
    assert resp.status_code == 200
    data = resp.json()

    assert data["snapshot_date"] == _D.isoformat()
    assert float(data["total_market_value"]) == 20000.0

    by_ticker = {p["ticker"]: p for p in data["positions"]}
    assert float(by_ticker["AAPL"]["percent_of_portfolio"]) == 50.0
    assert float(by_ticker["MSFT"]["percent_of_portfolio"]) == 25.0
    assert float(by_ticker["TSLA"]["percent_of_portfolio"]) == 25.0

    # AAPL spans a taxable + a tax_free lot.
    aapl_lots = {lot["account_name"]: lot["tax_treatment"] for lot in by_ticker["AAPL"]["accounts"]}
    assert aapl_lots == {"Taxable Brokerage": "taxable", "Roth IRA": "tax_free"}

    buckets = {k: float(v) for k, v in data["by_tax_treatment"].items()}
    assert buckets == {
        "taxable": 6000.0,
        "tax_deferred": 5000.0,
        "tax_free": 4000.0,
        "unknown": 5000.0,  # the bare "individual" account
    }


def test_positions_v1_empty_book(client):
    resp = client.get("/api/v1/portfolio/positions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["snapshot_date"] is None
    assert float(data["total_market_value"]) == 0.0
    assert data["positions"] == []
    assert {k: float(v) for k, v in data["by_tax_treatment"].items()} == {
        "taxable": 0.0,
        "tax_deferred": 0.0,
        "tax_free": 0.0,
        "unknown": 0.0,
    }
