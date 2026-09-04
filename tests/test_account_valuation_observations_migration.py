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


def test_account_valuation_observation_migration_round_trip(tmp_path, monkeypatch):
    database_path = tmp_path / "valuations.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "0027")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    assert "account_valuation_observations" in inspector.get_table_names()
    columns = {
        column["name"]: column for column in inspector.get_columns("account_valuation_observations")
    }
    assert columns["observation_key"]["nullable"] is False
    assert columns["total_value"]["nullable"] is False
    assert columns["cash_value"]["nullable"] is True
    assert columns["as_of_at"]["nullable"] is True
    assert columns["normalization_version"]["nullable"] is False
    assert columns["fetched_at"]["nullable"] is False
    indexes = {index["name"] for index in inspector.get_indexes("account_valuation_observations")}
    assert "ix_account_valuation_observations_account_date" in indexes
    engine.dispose()

    command.downgrade(config, "0027")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    assert "account_valuation_observations" not in inspector.get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0027"
    engine.dispose()
    get_settings.cache_clear()
