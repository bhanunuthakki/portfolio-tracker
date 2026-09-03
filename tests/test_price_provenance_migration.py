from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from portfolio_tracker.config import get_settings


def _config(database_path: Path, monkeypatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    get_settings.cache_clear()
    return Config(str(Path(__file__).parents[1] / "alembic.ini"))


def test_clean_database_upgrade_has_price_provenance_columns(tmp_path, monkeypatch):
    database_path = tmp_path / "clean.db"
    config = _config(database_path, monkeypatch)

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    columns = {column["name"]: column for column in inspect(engine).get_columns("prices")}
    assert columns["source"]["nullable"] is False
    assert columns["adjustment_basis"]["nullable"] is False
    constraints = {
        constraint["name"] for constraint in inspect(engine).get_check_constraints("prices")
    }
    assert constraints == {"ck_prices_adjustment_basis", "ck_prices_source"}
    engine.dispose()
    get_settings.cache_clear()


def test_existing_price_rows_migrate_to_unknown_and_downgrade_cleanly(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "0023")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO securities
                    (security_id, plaid_security_id, currency, is_cash_equivalent)
                VALUES
                    (1, 'synthetic-security', 'USD', 0)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO prices (security_id, date, close)
                VALUES (1, '2025-01-02', 10.0)
                """
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT source, adjustment_basis
                FROM prices
                WHERE security_id = 1 AND date = '2025-01-02'
                """
            )
        ).one()
    assert row.source == "unknown"
    assert row.adjustment_basis == "unknown"
    engine.dispose()

    command.downgrade(config, "0023")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    columns = {column["name"] for column in inspect(engine).get_columns("prices")}
    assert "source" not in columns
    assert "adjustment_basis" not in columns
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM prices")) == 1
    engine.dispose()
    get_settings.cache_clear()
