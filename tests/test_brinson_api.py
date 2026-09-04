"""HTTP surface for GET /api/portfolio/brinson."""

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
    SecurityClassification,
)
from portfolio_tracker.services.brinson import _SECTORS  # pyright: ignore[reportPrivateUsage]

_START = date(2025, 1, 2)
_END = date(2025, 6, 2)


def _seed(session) -> None:
    item = Item(source="plaid", plaid_item_id="i1", institution_name="Brk", is_data_active=True)
    session.add(item)
    session.flush()
    acct = Account(
        item_id=item.item_id,
        plaid_account_id="a1",
        name="Brokerage",
        type="brokerage",
        subtype="brokerage",
    )
    session.add(acct)
    session.flush()

    nvda = Security(plaid_security_id="s-nvda", ticker="NVDA", type="cs", is_cash_equivalent=False)
    jpm = Security(plaid_security_id="s-jpm", ticker="JPM", type="cs", is_cash_equivalent=False)
    session.add_all([nvda, jpm])
    session.flush()
    session.add_all(
        [
            SecurityClassification(
                security_id=nvda.security_id, sector="Technology", region="US", source="auto"
            ),
            SecurityClassification(
                security_id=jpm.security_id,
                sector="Financial Services",
                region="US",
                source="auto",
            ),
        ]
    )
    current = _START + timedelta(days=7)
    while current < _END:
        session.add_all(
            [
                Price(
                    security_id=nvda.security_id,
                    date=current,
                    close=Decimal(100),
                    source=PriceSource.YFINANCE.value,
                    adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
                ),
                Price(
                    security_id=jpm.security_id,
                    date=current,
                    close=Decimal(100),
                    source=PriceSource.YFINANCE.value,
                    adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
                ),
            ]
        )
        current += timedelta(days=7)

    def snap(sec: Security, d: date, value: str) -> HoldingSnapshot:
        return HoldingSnapshot(
            snapshot_date=d,
            account_id=acct.account_id,
            security_id=sec.security_id,
            quantity=Decimal(10),
            institution_value=Decimal(value),
        )

    # NVDA 1000 -> 1300 (+30%); JPM 1000 -> 1050 (+5%).
    session.add_all(
        [
            snap(nvda, _START, "1000"),
            snap(nvda, _END, "1300"),
            snap(jpm, _START, "1000"),
            snap(jpm, _END, "1050"),
            Price(
                security_id=nvda.security_id,
                date=_START,
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
            Price(
                security_id=nvda.security_id,
                date=_END,
                close=Decimal(130),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
            Price(
                security_id=jpm.security_id,
                date=_START,
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
            Price(
                security_id=jpm.security_id,
                date=_END,
                close=Decimal(105),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
        ]
    )

    # Benchmarks: SPY 100->110 (+10%), XLK 100->125 (+25%), XLF 100->110 (+10%),
    # every other sector ETF 100->105 (+5%) so the reconstruction is complete.
    def bench(symbol: str, d: date, close: str) -> Benchmark:
        return Benchmark(
            symbol=symbol, date=d, close=Decimal(close), total_return_close=Decimal(close)
        )

    rows = [
        bench("SPY", _START, "100"),
        bench("SPY", _END, "110"),
        bench("XLK", _START, "100"),
        bench("XLK", _END, "125"),
        bench("XLF", _START, "100"),
        bench("XLF", _END, "110"),
    ]
    current = _START + timedelta(days=7)
    while current < _END:
        rows.append(bench("SPY", current, "100"))
        current += timedelta(days=7)
    seeded = {"XLK", "XLF"}
    for s in _SECTORS:
        if s.etf in seeded:
            continue
        rows.append(bench(s.etf, _START, "100"))
        rows.append(bench(s.etf, _END, "105"))
    session.add_all(rows)
    session.commit()


def test_brinson(client, session):
    _seed(session)
    resp = client.get(f"/api/portfolio/brinson?start_date={_START}&end_date={_END}")
    assert resp.status_code == 200
    data = resp.json()

    assert data["benchmark"] == "SPY"
    # Sleeve return = total P&L / total beginning value = 350 / 2000 = 17.5%.
    assert float(data["portfolio_return_pct"]) == 17.5
    assert float(data["spy_actual_return_pct"]) == 10.0
    assert float(data["classified_value"]) == 2000.0
    assert float(data["excluded_value"]) == 0.0
    assert float(data["classified_weight_pct"]) == 100.0
    assert data["benchmark_return_pct"] is not None
    assert data["total_active_return_pct"] is not None

    by_sector = {r["sector"]: r for r in data["sectors"]}
    assert len(by_sector) == 11

    tech = by_sector["Information Technology"]
    assert tech["etf"] == "XLK"
    assert float(tech["portfolio_weight_pct"]) == 50.0
    assert float(tech["portfolio_return_pct"]) == 30.0
    assert float(tech["benchmark_return_pct"]) == 25.0
    # Held a sector that beat its ETF -> positive selection.
    assert float(tech["selection_effect_pct"]) > 0

    fin = by_sector["Financials"]
    assert fin["etf"] == "XLF"
    assert float(fin["portfolio_weight_pct"]) == 50.0
    assert float(fin["portfolio_return_pct"]) == 5.0
    assert float(fin["benchmark_return_pct"]) == 10.0
    # Picks lagged the sector ETF -> negative selection.
    assert float(fin["selection_effect_pct"]) < 0

    # An unheld sector: zero portfolio weight, null portfolio return + selection,
    # but allocation (the under-weight decision) is still attributed.
    energy = by_sector["Energy"]
    assert float(energy["portfolio_weight_pct"]) == 0.0
    assert energy["portfolio_return_pct"] is None
    assert energy["selection_effect_pct"] is None
    assert energy["allocation_effect_pct"] is not None


def test_brinson_empty_book(client):
    resp = client.get(f"/api/portfolio/brinson?start_date={_START}&end_date={_END}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["portfolio_return_pct"] is None
    assert float(data["classified_value"]) == 0.0
    assert any("No classifiable equity positions" in n for n in data["notes"])


def test_brinson_propagates_unmatched_share_movement_unavailable(client, session):
    _seed(session)
    account = session.query(Account).one()
    security = session.query(Security).filter(Security.ticker == "NVDA").one()
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-unmatched-transfer",
            account_id=account.account_id,
            security_id=security.security_id,
            date=date(2025, 5, 15),
            type="transfer",
            quantity=Decimal(1),
            amount=Decimal(0),
        )
    )
    session.commit()

    data = client.get(f"/api/portfolio/brinson?start_date={_START}&end_date={_END}").json()

    assert data["calculation_status"] == "unavailable"
    assert "share_movement_unmatched" in data["calculation_reason_codes"]
    assert data["portfolio_return_pct"] is None
    assert data["total_active_return_pct"] is None
    assert data["sectors"] == []
