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


def test_source_coverage_migration_round_trip(tmp_path, monkeypatch):
    database_path = tmp_path / "coverage.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "0024")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    assert "cashflow_source_attestations" in inspector.get_table_names()
    assert "cashflow_source_gaps" in inspector.get_table_names()
    attestation_columns = {
        column["name"] for column in inspector.get_columns("cashflow_source_attestations")
    }
    assert {
        "attestation_key",
        "account_id",
        "coverage_start",
        "coverage_end",
        "source_reference",
        "source_sha256",
        "captured_at",
        "approved_at",
        "superseded_at",
        "superseded_by_attestation_id",
    } <= attestation_columns
    constraints = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("cashflow_source_attestations")
    }
    assert {
        "ck_cashflow_source_attestations_date_order",
        "ck_cashflow_source_attestations_source_type",
        "ck_cashflow_source_attestations_sha256_length",
        "ck_cashflow_source_attestations_approval_order",
        "ck_cashflow_source_attestations_supersession_pair",
    } <= constraints
    engine.dispose()

    command.downgrade(config, "0024")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    assert "cashflow_source_attestations" not in inspector.get_table_names()
    assert "cashflow_source_gaps" not in inspector.get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0024"
    engine.dispose()
    get_settings.cache_clear()
