from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from portfolio_tracker.config import get_settings


def test_provider_transaction_correction_receipt_migration_round_trip(
    tmp_path: Path,
    monkeypatch,
):
    database_path = tmp_path / "provider-corrections.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    get_settings.cache_clear()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    assert inspect(engine).has_table("provider_transaction_correction_receipts")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0030"
    engine.dispose()

    command.downgrade(config, "0029")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    assert not inspect(engine).has_table("provider_transaction_correction_receipts")
    engine.dispose()
    get_settings.cache_clear()
