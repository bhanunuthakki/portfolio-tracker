from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portfolio_tracker.models import (
    Account,
    Benchmark,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    PortfolioValueDaily,
    Security,
)
from portfolio_tracker.services.benchmark_estimate import (
    _matched_share_transfers,  # pyright: ignore[reportPrivateUsage]
    build_benchmark_estimate,
)
from portfolio_tracker.services.external_flow_ledger import (
    ExternalFlowEntry,
    build_external_flow_ledger,
)

START = date(2026, 5, 13)
MIDDLE = date(2026, 8, 3)
END = date(2026, 11, 9)


def _account(session, suffix: str) -> Account:
    item = Item(source="plaid", plaid_item_id=f"item-{suffix}", is_data_active=True)
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id,
        plaid_account_id=f"account-{suffix}",
        name=f"Account {suffix}",
        type="investment",
    )
    session.add(account)
    session.flush()
    return account


def _security(session, ticker: str) -> Security:
    security = Security(plaid_security_id=f"security-{ticker}", ticker=ticker, type="cs")
    session.add(security)
    session.flush()
    return security


def _snapshot(session, account: Account, security: Security, day: date, value: str) -> None:
    session.add(
        HoldingSnapshot(
            snapshot_date=day,
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal(1),
            institution_value=Decimal(value),
        )
    )


def _benchmark(session, symbol: str, closes: dict[date, str]) -> None:
    for day, close in closes.items():
        session.add(
            Benchmark(
                symbol=symbol,
                date=day,
                close=Decimal(close),
                total_return_close=Decimal(close),
            )
        )


def _deposit(session, account: Account, day: date, amount: str) -> None:
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id=f"deposit-{day}",
            account_id=account.account_id,
            date=day,
            name="Deposit",
            quantity=Decimal(0),
            amount=Decimal(amount),
            type="cash",
            subtype="deposit",
            currency="USD",
        )
    )


def test_observed_window_matches_hand_calculation(session):
    account = _account(session, "a")
    stock = _security(session, "AAA")
    _snapshot(session, account, stock, START, "1000")
    _snapshot(session, account, stock, END, "1300")
    _deposit(session, account, MIDDLE, "-100")
    _benchmark(session, "VOO", {START: "100", MIDDLE: "105", END: "110"})
    session.commit()

    result = build_benchmark_estimate(
        session, end_date=END, lookback_days=[180], benchmarks=["voo", "ZZZ"]
    )

    assert result.methodology == "benchmark_comparison.estimate"
    assert result.as_of == END
    window = result.windows[0]
    assert window.start_date == START
    assert window.end_date == END
    assert window.opening_is_modeled is False
    assert window.tier in {"estimate", "canonical_available"}
    assert window.canonical is not None

    ledger = build_external_flow_ledger(
        session, START, END, account_ids=frozenset({account.account_id})
    )
    flows = [(e.date, e.signed_external_amount) for e in ledger.entries if START < e.date <= END]
    net = sum((amount for _, amount in flows), Decimal(0))
    assert net != 0
    assert window.net_external_cashflow_in == net
    days = Decimal((END - START).days)
    denominator = Decimal(1000) + sum(
        (amount * Decimal((END - d).days) / days for d, amount in flows), Decimal(0)
    )
    expected = (Decimal(1300) - Decimal(1000) - net) / denominator * 100
    assert window.portfolio_return_pct is not None
    assert window.portfolio_return_pct.central == pytest.approx(expected, abs=Decimal("0.00001"))
    assert window.portfolio_return_pct.low == window.portfolio_return_pct.high

    voo, missing = window.benchmarks
    assert voo.ticker == "VOO" and voo.available
    shares = Decimal(1000) / Decimal(100) + sum(
        (amount / Decimal(105) for _, amount in flows), Decimal(0)
    )
    bench_end = shares * Decimal(110)
    assert voo.dollar_gap is not None
    assert voo.dollar_gap.central == pytest.approx(Decimal(1300) - bench_end, abs=Decimal("0.001"))
    assert voo.gap_pp is not None and voo.return_pct is not None
    assert voo.gap_pp.central == pytest.approx(
        window.portfolio_return_pct.central - voo.return_pct.central, abs=Decimal("0.00001")
    )
    assert missing.ticker == "ZZZ" and missing.available is False and missing.gap_pp is None


def test_modeled_opening_widens_band_by_distance_to_observed_book(session):
    account = _account(session, "a")
    stock = _security(session, "AAA")
    opening = date(2025, 11, 9)
    session.add(PortfolioValueDaily(date=opening, total_value=Decimal(900), source="backfill"))
    _snapshot(session, account, stock, START, "1000")
    _snapshot(session, account, stock, END, "1200")
    _benchmark(session, "VOO", {opening: "90", START: "100", END: "110"})
    session.commit()

    window = build_benchmark_estimate(
        session, end_date=END, lookback_days=[365], benchmarks=["VOO"]
    ).windows[0]

    assert window.start_date == opening
    assert window.opening_is_modeled is True
    expected_error = Decimal("0.0163") * Decimal((START - opening).days) / Decimal(180)
    assert window.opening_error_fraction == pytest.approx(expected_error, abs=Decimal("0.000001"))
    band = window.portfolio_return_pct
    assert band is not None
    assert band.low < band.central < band.high
    assert band.central == pytest.approx(Decimal(300) / Decimal(900) * 100, abs=Decimal("0.0001"))


def test_missing_opening_value_is_unavailable_not_guessed(session):
    account = _account(session, "a")
    stock = _security(session, "AAA")
    _snapshot(session, account, stock, END, "1200")
    session.commit()

    window = build_benchmark_estimate(session, end_date=END, lookback_days=[730]).windows[0]

    assert window.tier == "unavailable"
    assert window.unavailable_reason == "no_retained_value_near_opening_date"
    assert window.portfolio_return_pct is None


def test_share_transfer_with_opposite_leg_in_valued_account_is_internal(session):
    sender = _account(session, "out")
    receiver = _account(session, "in")
    stock = _security(session, "VTI")
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="acats-in",
            account_id=receiver.account_id,
            security_id=stock.security_id,
            date=START,
            name="ACATS in",
            quantity=Decimal(372),
            amount=Decimal(0),
            type="cash",
            subtype="external_asset_transfer_in",
            currency="USD",
        )
    )
    session.commit()
    entry = ExternalFlowEntry(
        flow_id=f"share-transfer:{START}:VTI",
        date=START,
        source_kind="share_transfer_valuation",
        source_provider="plaid",
        signed_external_amount=Decimal("-135672.12"),
        classification="external_out",
        classification_source="derived_share_transfer_net",
        classification_rule="share_transfer.net_quantity_valued_at_available_price",
        currency="USD",
        account_ids=(sender.account_id,),
        security_id=stock.security_id,
        security_ids=(stock.security_id,),
        ticker="VTI",
        type="derived",
        subtype="unmatched_share_transfer",
        valuation_price=Decimal("364.71"),
    )
    accounts = frozenset({sender.account_id, receiver.account_id})

    matched = _matched_share_transfers(session, [entry], accounts)
    assert set(matched) == {entry.flow_id}
    assert matched[entry.flow_id].withheld_amount == Decimal("-135672.12")
    assert _matched_share_transfers(session, [entry], frozenset({sender.account_id})) == {}


def test_route_validates_lookbacks_and_returns_envelope(client, session):
    account = _account(session, "a")
    stock = _security(session, "AAA")
    _snapshot(session, account, stock, START, "1000")
    _snapshot(session, account, stock, END, "1100")
    session.commit()

    bad = client.get("/api/v1/analytics/benchmark-estimate", params={"lookback_days": 0})
    assert bad.status_code == 422
    response = client.get(
        "/api/v1/analytics/benchmark-estimate",
        params=[("lookback_days", "180"), ("benchmarks", "QQQ"), ("end_date", str(END))],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["methodology"] == "benchmark_comparison.estimate"
    assert body["result"]["benchmarks"] == ["QQQ"]
    assert body["result"]["windows"][0]["lookback_days"] == 180


def test_benchmark_never_mixes_total_return_and_raw_closes(session):
    account = _account(session, "a")
    stock = _security(session, "AAA")
    _snapshot(session, account, stock, START, "1000")
    _snapshot(session, account, stock, END, "1100")
    # Legacy opening row has no total-return close; the ending row does.
    session.add(Benchmark(symbol="VOO", date=START, close=Decimal(100), total_return_close=None))
    session.add(
        Benchmark(symbol="VOO", date=END, close=Decimal(110), total_return_close=Decimal(130))
    )
    session.commit()

    window = build_benchmark_estimate(
        session, end_date=END, lookback_days=[180], benchmarks=["VOO"]
    ).windows[0]

    voo = window.benchmarks[0]
    assert voo.available and voo.return_basis == "raw_price_fallback"
    assert voo.return_pct is not None
    assert voo.return_pct.central == pytest.approx(Decimal(10), abs=Decimal("0.0001"))
    assert any("raw_price_fallback" in note for note in window.assumptions)


def test_non_positive_denominator_is_unavailable(session):
    account = _account(session, "a")
    stock = _security(session, "AAA")
    _snapshot(session, account, stock, START, "100")
    _snapshot(session, account, stock, END, "50")
    # A withdrawal far larger than the opening value early in the window.
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="withdrawal-large",
            account_id=account.account_id,
            date=date(2026, 5, 14),
            name="Withdrawal",
            quantity=Decimal(0),
            amount=Decimal(5000),
            type="cash",
            subtype="withdrawal",
            currency="USD",
        )
    )
    _benchmark(session, "VOO", {START: "100", date(2026, 5, 14): "100", END: "110"})
    session.commit()

    window = build_benchmark_estimate(
        session, end_date=END, lookback_days=[180], benchmarks=["VOO"]
    ).windows[0]

    assert window.tier == "unavailable"
    assert window.unavailable_reason == "non_positive_modified_dietz_denominator"
    assert window.portfolio_return_pct is None and window.benchmarks == []
