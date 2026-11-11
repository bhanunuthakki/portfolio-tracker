"""The price step stays bounded, keeps completed rows and prices held securities."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from portfolio_tracker.api.routes import snaptrade as snaptrade_routes
from portfolio_tracker.jobs import backfill, benchmarks, classify_securities, daily_refresh, prices
from portfolio_tracker.models import HoldingSnapshot, InvestmentTransaction, Price, Security

START = date(2026, 10, 8)
END = date(2026, 11, 7)
TASK_LIMIT_SECONDS = 1800.0


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _security(session: Session, ticker: str, **fields: object) -> Security:
    security = Security(plaid_security_id=f"synthetic-{ticker}", ticker=ticker, **fields)
    session.add(security)
    session.flush()
    return security


def _hold(session: Session, security: Security, day: date, quantity: str = "1") -> None:
    session.add(
        HoldingSnapshot(
            snapshot_date=day,
            account_id=1,
            security_id=security.security_id,
            quantity=Decimal(quantity),
        )
    )


def _trade(session: Session, security: Security, day: date) -> None:
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id=f"tx-{security.ticker}-{day}",
            account_id=1,
            security_id=security.security_id,
            date=day,
            quantity=Decimal("1"),
            amount=Decimal("1"),
            type="buy",
        )
    )


@pytest.fixture
def job_db(engine, monkeypatch):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(prices, "SessionLocal", factory)
    return factory


def _tickers(session: Session) -> list[str]:
    return [ticker for _, ticker in prices._securities_to_price(session, START, END)]


def test_selection_keeps_held_and_traded_and_skips_dead_securities(session: Session):
    held = _security(session, "HELD")
    short_option = _security(session, "SHORT260101C00010000", type="derivative")
    traded = _security(session, "TRADED")
    closed = _security(session, "CLOSED")
    dead = _security(session, "DEAD")
    _security(session, "NOACTIVITY")
    cash = _security(session, "CASH", is_cash_equivalent=True)
    expired = _security(
        session,
        "OLD250117C00040000",
        type="derivative",
        option_contract_json='{"expiration_date": "2025-01-17"}',
    )
    _hold(session, held, END)
    _hold(session, short_option, END, "-1")
    _hold(session, cash, END)
    _hold(session, expired, END)
    _hold(session, closed, START - timedelta(days=10))
    _hold(session, closed, END, "0")
    _trade(session, traded, START + timedelta(days=2))
    _trade(session, dead, date(2024, 1, 2))
    session.commit()

    assert _tickers(session) == ["HELD", "SHORT260101C00010000", "TRADED"]


def test_position_held_across_a_narrow_window_is_selected(session: Session):
    security = _security(session, "LONGHOLD")
    _trade(session, security, START - timedelta(days=200))
    _trade(session, security, END + timedelta(days=200))
    session.commit()

    assert _tickers(session) == ["LONGHOLD"]


def test_unreachable_stooq_is_tried_once_per_run(session, job_db, monkeypatch):
    for index in range(150):
        _hold(session, _security(session, f"GONE{index}"), END)
    session.commit()
    calls: list[str] = []
    clock = _Clock()

    def _timeout(*_args, **_kwargs):
        calls.append("stooq")
        clock.now += 31.0
        raise httpx.ConnectTimeout("synthetic")

    monkeypatch.setattr(prices, "monotonic", clock)
    monkeypatch.setattr(prices, "_yfinance_history", lambda *_args: {})
    monkeypatch.setattr(prices.httpx, "get", _timeout)

    written, by_source = prices.run(START, END)

    assert written == 0
    assert by_source["none"] == 150
    assert calls == ["stooq"]
    assert clock.now < TASK_LIMIT_SECONDS / 10


def test_budget_defers_remaining_securities_and_keeps_open_positions_first(
    session, job_db, monkeypatch
):
    closed_in_window = _security(session, "AAAOLD")
    _trade(session, closed_in_window, START)
    held = [_security(session, f"HELD{index}") for index in range(3)]
    for security in held:
        _hold(session, security, END)
    session.commit()
    clock = _Clock()
    seen: list[str] = []

    def _history(ticker, *_args):
        seen.append(ticker)
        clock.now += 10.0
        return {END: Decimal("10")}

    monkeypatch.setattr(prices, "monotonic", clock)
    monkeypatch.setattr(prices, "_yfinance_history", _history)

    _, by_source = prices.run(START, END, 25.0)

    assert seen == ["HELD0", "HELD1", "HELD2"]
    assert by_source["deferred"] == 1


def test_completed_securities_survive_a_stopped_run(session, job_db, monkeypatch):
    first, second = _security(session, "FIRST"), _security(session, "SECOND")
    _hold(session, first, END)
    _hold(session, second, END)
    session.commit()
    first_id = first.security_id

    def _history(ticker, *_args):
        if ticker == "SECOND":
            raise KeyboardInterrupt
        return {END: Decimal("10")}

    monkeypatch.setattr(prices, "_yfinance_history", _history)

    with pytest.raises(KeyboardInterrupt):
        prices.run(START, END)

    with job_db() as check:
        rows = list(check.scalars(select(Price)))
    assert [(row.security_id, row.source) for row in rows] == [(first_id, "yfinance")]
    assert rows[0].is_position_price_trade_eligible is True


def test_daily_refresh_reaches_classification_and_done_inside_the_limit(
    session, job_db, monkeypatch, capsys
):
    today = date.today()
    held = _security(session, "HELD")
    _hold(session, held, today)
    for index in range(150):
        _trade(session, _security(session, f"DEAD{index}"), date(2024, 1, 2))
    session.commit()
    held_id = held.security_id
    clock = _Clock()

    def _history(ticker, *_args):
        clock.now += 1.0
        return {today: Decimal("10")} if ticker == "HELD" else {}

    def _timeout(*_args, **_kwargs):
        clock.now += 31.0
        raise httpx.ConnectTimeout("synthetic")

    classified: list[bool] = []

    def _classify(**_kwargs):
        classified.append(True)
        return {"classified": 0, "no_data": 0}

    monkeypatch.setattr(prices, "monotonic", clock)
    monkeypatch.setattr(prices, "_yfinance_history", _history)
    monkeypatch.setattr(prices.httpx, "get", _timeout)
    monkeypatch.setattr(daily_refresh.snapshot, "run", lambda: 0)
    monkeypatch.setattr(
        backfill,
        "run_with_delivery_receipts",
        lambda **_kwargs: backfill.BackfillRunResult(rows_written=0, item_receipts=()),
    )
    monkeypatch.setattr(daily_refresh.daily_values, "run", lambda **_kwargs: 0)
    monkeypatch.setattr(daily_refresh.reconcile_option_direction, "run", lambda *_a: 0)
    monkeypatch.setattr(
        snaptrade_routes,
        "sync",
        lambda *_args: SimpleNamespace(
            items_synced=0,
            accounts_synced=0,
            holdings_written=0,
            transactions_written=0,
            descriptions_preserved=0,
        ),
    )
    monkeypatch.setattr(
        benchmarks,
        "run_with_coverage",
        lambda *_args: SimpleNamespace(status="complete", rows_written=0),
    )
    monkeypatch.setattr(classify_securities, "run", _classify)

    status = daily_refresh.run()

    output = capsys.readouterr().out
    assert status == 0
    assert "prices: 1 rows written" in output
    assert classified == [True]
    assert f"[daily_refresh] {today} done: OK" in output
    assert clock.now < 2.0
    with job_db() as check:
        assert check.get(Price, (held_id, today)) is not None
