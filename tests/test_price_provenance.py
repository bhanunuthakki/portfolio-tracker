from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from portfolio_tracker.jobs import prices
from portfolio_tracker.jobs.dedupe_securities import reassign
from portfolio_tracker.models import (
    Price,
    PriceAdjustmentBasis,
    PriceSource,
    Security,
)


def _security(session: Session) -> Security:
    security = Security(plaid_security_id="synthetic-security", ticker="TEST")
    session.add(security)
    session.flush()
    return security


def test_price_defaults_to_unknown_provenance_and_is_not_eligible(session: Session):
    security = _security(session)
    row = Price(
        security_id=security.security_id,
        date=date(2025, 1, 2),
        close=Decimal("10.00"),
    )
    session.add(row)
    session.commit()

    assert row.source == PriceSource.UNKNOWN.value
    assert row.adjustment_basis == PriceAdjustmentBasis.UNKNOWN.value
    assert row.is_position_price_trade_eligible is False
    assert (
        session.scalar(select(Price).where(Price.position_price_trade_eligibility_clause())) is None
    )


def test_yfinance_split_adjusted_price_is_queryably_eligible(session: Session):
    security = _security(session)
    row = Price(
        security_id=security.security_id,
        date=date(2025, 1, 2),
        close=Decimal("10.00"),
        source=PriceSource.YFINANCE.value,
        adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
    )
    session.add(row)
    session.commit()

    assert row.is_position_price_trade_eligible is True
    assert (
        session.scalar(select(Price).where(Price.position_price_trade_eligibility_clause())) is row
    )


def test_fetch_history_labels_yfinance_close_as_split_adjusted(monkeypatch):
    expected = {date(2025, 1, 2): Decimal("10.00")}
    monkeypatch.setattr(prices, "_yfinance_history", lambda *_args: expected)
    monkeypatch.setattr(prices, "_stooq_history", lambda *_args: {})

    history, source, basis = prices._fetch_history("TEST", date(2025, 1, 1), date(2025, 1, 3))

    assert history == expected
    assert source == PriceSource.YFINANCE.value
    assert basis == PriceAdjustmentBasis.SPLIT_ADJUSTED.value


def test_yfinance_history_requests_unadjusted_close(monkeypatch):
    requested: dict[str, object] = {}

    class _History:
        empty = False

        @staticmethod
        def iterrows():
            return [(date(2025, 1, 2), {"Close": 10.25})]

    class _Ticker:
        def history(self, **kwargs):
            requested.update(kwargs)
            return _History()

    monkeypatch.setattr(prices.yf, "Ticker", lambda _ticker: _Ticker())

    result = prices._yfinance_history("TEST", date(2025, 1, 1), date(2025, 1, 3))

    assert requested["auto_adjust"] is False
    assert result == {date(2025, 1, 2): Decimal("10.25")}


def test_fetch_history_labels_stooq_adjustment_basis_unknown(monkeypatch):
    expected = {date(2025, 1, 2): Decimal("10.00")}
    monkeypatch.setattr(prices, "_yfinance_history", lambda *_args: {})
    monkeypatch.setattr(prices, "_stooq_history", lambda *_args: expected)

    history, source, basis = prices._fetch_history("TEST", date(2025, 1, 1), date(2025, 1, 3))

    assert history == expected
    assert source == PriceSource.STOOQ.value
    assert basis == PriceAdjustmentBasis.UNKNOWN.value


def test_write_history_upsert_refreshes_provenance_without_duplicates(session: Session):
    security = _security(session)
    bar_date = date(2025, 1, 2)

    assert (
        prices._write_history(
            session,
            security,
            {bar_date: Decimal("10.00")},
            source=PriceSource.STOOQ.value,
            adjustment_basis=PriceAdjustmentBasis.UNKNOWN.value,
        )
        == 1
    )
    session.flush()

    assert (
        prices._write_history(
            session,
            security,
            {bar_date: Decimal("11.00")},
            source=PriceSource.YFINANCE.value,
            adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
        )
        == 0
    )
    session.commit()

    rows = list(session.scalars(select(Price)))
    assert len(rows) == 1
    assert rows[0].close == Decimal("11.000000")
    assert rows[0].source == PriceSource.YFINANCE.value
    assert rows[0].adjustment_basis == PriceAdjustmentBasis.SPLIT_ADJUSTED.value
    assert rows[0].is_position_price_trade_eligible is True


def test_security_dedupe_preserves_price_provenance(engine):
    with Session(engine) as session:
        canonical = Security(plaid_security_id="canonical", ticker="TEST")
        duplicate = Security(plaid_security_id="duplicate", ticker="TEST")
        session.add_all([canonical, duplicate])
        session.flush()
        canonical_id = canonical.security_id
        duplicate_id = duplicate.security_id
        session.add(
            Price(
                security_id=duplicate_id,
                date=date(2025, 1, 2),
                close=Decimal("10.00"),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            )
        )
        session.commit()

    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        reassign(cursor, duplicate_id, canonical_id)
        connection.commit()
    finally:
        connection.close()

    with Session(engine) as session:
        row = session.get(Price, (canonical_id, date(2025, 1, 2)))
        assert row is not None
        assert row.source == PriceSource.YFINANCE.value
        assert row.adjustment_basis == PriceAdjustmentBasis.SPLIT_ADJUSTED.value


@pytest.mark.parametrize("eligible_on_duplicate", [False, True])
def test_security_dedupe_collision_preserves_superior_price_provenance(
    engine, eligible_on_duplicate: bool
):
    price_date = date(2025, 1, 2)
    with Session(engine) as session:
        canonical = Security(plaid_security_id="canonical-collision", ticker="COLLISION")
        duplicate = Security(plaid_security_id="duplicate-collision", ticker="COLLISION")
        session.add_all([canonical, duplicate])
        session.flush()
        canonical_id = canonical.security_id
        duplicate_id = duplicate.security_id
        eligible_sid = duplicate_id if eligible_on_duplicate else canonical_id
        inferior_sid = canonical_id if eligible_on_duplicate else duplicate_id
        session.add_all(
            [
                Price(
                    security_id=eligible_sid,
                    date=price_date,
                    close=Decimal("11.00"),
                    source=PriceSource.YFINANCE.value,
                    adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
                ),
                Price(
                    security_id=inferior_sid,
                    date=price_date,
                    close=Decimal("10.00"),
                    source=PriceSource.STOOQ.value,
                    adjustment_basis=PriceAdjustmentBasis.UNKNOWN.value,
                ),
            ]
        )
        session.commit()

    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        reassign(cursor, duplicate_id, canonical_id)
        connection.commit()
    finally:
        connection.close()

    with Session(engine) as session:
        row = session.get(Price, (canonical_id, price_date))
        assert row is not None
        assert row.close == Decimal("11.000000")
        assert row.source == PriceSource.YFINANCE.value
        assert row.adjustment_basis == PriceAdjustmentBasis.SPLIT_ADJUSTED.value


def test_position_price_trade_eligibility_fails_closed(session: Session):
    security = _security(session)
    rows = [
        Price(
            security_id=security.security_id,
            date=date(2025, 1, 2),
            close=Decimal("10.00"),
            source=PriceSource.YFINANCE.value,
            adjustment_basis=PriceAdjustmentBasis.RAW_UNADJUSTED.value,
        ),
        Price(
            security_id=security.security_id,
            date=date(2025, 1, 3),
            close=Decimal("10.00"),
            source=PriceSource.STOOQ.value,
            adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
        ),
        Price(
            security_id=security.security_id,
            date=date(2025, 1, 4),
            close=Decimal(0),
            source=PriceSource.YFINANCE.value,
            adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
        ),
    ]
    session.add_all(rows)
    session.commit()

    assert all(row.is_position_price_trade_eligible is False for row in rows)
    assert (
        list(session.scalars(select(Price).where(Price.position_price_trade_eligibility_clause())))
        == []
    )


def test_price_source_constraint_rejects_unsupported_provider(session: Session):
    security = _security(session)
    session.add(
        Price(
            security_id=security.security_id,
            date=date(2025, 1, 2),
            close=Decimal("10.00"),
            source="unsupported-provider",
            adjustment_basis=PriceAdjustmentBasis.UNKNOWN.value,
        )
    )

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
    else:
        raise AssertionError("unsupported price provider was persisted")
