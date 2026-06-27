"""DB-backed assembly of the positioning cuts (services/positioning.compute_positioning).

Seeds a small multi-account book and asserts each breakdown sums correctly,
that only the latest snapshot of active accounts is counted, and that the
correlation table is populated from prices/benchmarks (cash excluded).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    Benchmark,
    HoldingSnapshot,
    Item,
    PolicyWeight,
    Price,
    Security,
    SecurityClassification,
)
from portfolio_tracker.schemas import PositioningBucketOut, PositioningOut
from portfolio_tracker.services.positioning import compute_positioning

_START = date(2025, 5, 27)
_END = date(2025, 6, 2)
_LATEST = date(2025, 6, 2)
_OLDER = date(2025, 6, 1)
_PRICE_DATES = [
    date(2025, 5, 27),
    date(2025, 5, 28),
    date(2025, 5, 29),
    date(2025, 5, 30),
    date(2025, 6, 2),
]


def _bucket_map(buckets: list[PositioningBucketOut]) -> dict[str, PositioningBucketOut]:
    return {b.label: b for b in buckets}


def _add_prices(session, security_id: int, closes: list[float]) -> None:
    for d, c in zip(_PRICE_DATES, closes):
        session.add(Price(security_id=security_id, date=d, close=Decimal(str(c))))


def _seed(session) -> None:
    active = Item(
        source="plaid",
        plaid_item_id="itm-active",
        institution_name="Robinhood",
        is_data_active=True,
    )
    inactive = Item(
        source="plaid", plaid_item_id="itm-inactive", institution_name="Old", is_data_active=False
    )
    session.add_all([active, inactive])
    session.flush()

    acct_taxable = Account(
        item_id=active.item_id,
        plaid_account_id="a-tax",
        name="Robinhood Individual",
        type="investment",
        subtype="INDIVIDUAL",
    )
    acct_retire = Account(
        item_id=active.item_id,
        plaid_account_id="a-ira",
        name="Robinhood Roth Ira",
        type="investment",
        subtype="ROTH_IRA",
    )
    acct_dead = Account(
        item_id=inactive.item_id,
        plaid_account_id="a-dead",
        name="Dead",
        type="investment",
        subtype="INDIVIDUAL",
    )
    session.add_all([acct_taxable, acct_retire, acct_dead])
    session.flush()

    nvda = Security(plaid_security_id="s-nvda", ticker="NVDA", type="cs", is_cash_equivalent=False)
    voo = Security(plaid_security_id="s-voo", ticker="VOO", type="et", is_cash_equivalent=False)
    nvo = Security(plaid_security_id="s-nvo", ticker="NVO", type="ad", is_cash_equivalent=False)
    sgov = Security(plaid_security_id="s-sgov", ticker="SGOV", type="oef", is_cash_equivalent=True)
    junk = Security(plaid_security_id="s-junk", ticker="JUNK", type="cs", is_cash_equivalent=False)
    session.add_all([nvda, voo, nvo, sgov, junk])
    session.flush()

    session.add_all(
        [
            SecurityClassification(
                security_id=nvda.security_id, sector="Technology", region="US", source="auto"
            ),
            SecurityClassification(
                security_id=nvo.security_id,
                sector="Healthcare",
                region="International",
                source="manual",
            ),
            SecurityClassification(
                security_id=voo.security_id, sector="ETF/Fund", region=None, source="auto"
            ),
        ]
    )

    def snap(acct, sec, qty, value, d=_LATEST):
        return HoldingSnapshot(
            snapshot_date=d,
            account_id=acct.account_id,
            security_id=sec.security_id,
            quantity=Decimal(qty),
            institution_value=Decimal(value),
        )

    session.add_all(
        [
            snap(acct_taxable, nvda, 10, 2000),
            snap(acct_taxable, voo, 20, 1000),
            snap(acct_taxable, nvo, 16, 1000),
            snap(acct_retire, sgov, 1000, 1000),
            # excluded — inactive item:
            snap(acct_dead, junk, 1, 99999),
            # excluded — older snapshot date:
            snap(acct_taxable, nvda, 10, 1234, d=_OLDER),
        ]
    )

    _add_prices(session, nvda.security_id, [100, 102, 101, 103, 105])
    _add_prices(session, voo.security_id, [50, 50.5, 50.2, 50.8, 51])
    _add_prices(session, nvo.security_id, [60, 61, 60.5, 62, 63])
    for d, c in zip(_PRICE_DATES, [500, 505, 503, 508, 512]):
        session.add(Benchmark(symbol="SPY", date=d, close=Decimal(str(c))))
    for d, c in zip(_PRICE_DATES, [400, 404, 402, 408, 412]):
        session.add(Benchmark(symbol="QQQ", date=d, close=Decimal(str(c))))
    session.add(PolicyWeight(ticker="SPY", weight_bps=10000))
    session.commit()


def test_total_value_excludes_inactive_and_older_snapshots(session):
    _seed(session)
    out = compute_positioning(session, _START, _END)
    assert isinstance(out, PositioningOut)
    assert out.snapshot_date == _LATEST
    assert out.total_value == Decimal(5000)  # 99999 (inactive) + 1234 (older) excluded


def test_asset_type_breakdown(session):
    _seed(session)
    out = compute_positioning(session, _START, _END)
    by = _bucket_map(out.by_asset_type)
    assert by["Stock"].value == Decimal(3000)  # NVDA + NVO (ADR is equity)
    assert by["Stock"].count == 2
    assert round(float(by["Stock"].weight_pct)) == 60
    assert by["ETF"].value == Decimal(1000)
    assert by["Cash"].value == Decimal(1000)  # SGOV (cash-equivalent beats 'oef')
    assert sum(float(b.weight_pct) for b in out.by_asset_type) == 100.0


def test_sector_breakdown_buckets_funds_and_cash(session):
    _seed(session)
    by = _bucket_map(compute_positioning(session, _START, _END).by_sector)
    assert by["Technology"].value == Decimal(2000)
    assert by["Healthcare"].value == Decimal(1000)
    assert by["ETF/Fund"].value == Decimal(1000)
    assert by["Cash"].value == Decimal(1000)


def test_etf_lookthrough_caveat_note_when_funds_present(session):
    _seed(session)
    out = compute_positioning(session, _START, _END)
    lookthrough = [n for n in out.notes if "look-through" in n]
    assert lookthrough, "expected an ETF/fund look-through caveat note"
    assert "20%" in lookthrough[0]  # VOO is 1000 / 5000 of the book


def test_region_breakdown(session):
    _seed(session)
    by = _bucket_map(compute_positioning(session, _START, _END).by_region)
    assert by["US"].value == Decimal(2000)
    assert by["International"].value == Decimal(1000)
    assert by["Cash"].value == Decimal(1000)
    assert by["Unknown"].value == Decimal(1000)  # VOO ETF has no region


def test_account_type_breakdown(session):
    _seed(session)
    by = _bucket_map(compute_positioning(session, _START, _END).by_account_type)
    assert by["Taxable"].value == Decimal(4000)
    assert by["Retirement / tax-advantaged"].value == Decimal(1000)


def test_concentration(session):
    _seed(session)
    c = compute_positioning(session, _START, _END).concentration
    assert c.num_positions == 4  # NVDA, VOO, NVO, SGOV
    assert round(float(c.top1_weight_pct)) == 40  # NVDA 2000 / 5000


def test_correlation_table_excludes_cash_and_populates(session):
    _seed(session)
    out = compute_positioning(session, _START, _END)
    tickers = {r.ticker for r in out.correlations}
    assert "SGOV" not in tickers  # cash excluded from correlation
    assert {"NVDA", "VOO", "NVO"} <= tickers
    nvda_row = next(r for r in out.correlations if r.ticker == "NVDA")
    assert nvda_row.sample_size >= 2
    assert nvda_row.correlation_spy is not None
    assert nvda_row.correlation_policy is not None  # policy = SPY mix
    assert out.has_policy is True
    assert out.weighted_avg_correlation_spy is not None
