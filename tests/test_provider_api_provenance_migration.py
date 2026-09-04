from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from portfolio_tracker.config import get_settings


def _config(database_path: Path, monkeypatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    get_settings.cache_clear()
    return Config(str(Path(__file__).parents[1] / "alembic.ini"))


def test_provider_api_source_type_migration_round_trip(tmp_path, monkeypatch):
    database_path = tmp_path / "provider-provenance.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "0028")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    constraints = {
        constraint["name"]: constraint.get("sqltext", "")
        for constraint in inspect(engine).get_check_constraints("cashflow_source_attestations")
    }
    assert "provider_api" in constraints["ck_cashflow_source_attestations_source_type"]
    engine.dispose()

    command.downgrade(config, "0028")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    constraints = {
        constraint["name"]: constraint.get("sqltext", "")
        for constraint in inspect(engine).get_check_constraints("cashflow_source_attestations")
    }
    assert "provider_api" not in constraints["ck_cashflow_source_attestations_source_type"]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0028"
    engine.dispose()
    get_settings.cache_clear()


def test_provider_api_source_type_downgrade_refuses_data_loss(tmp_path, monkeypatch):
    database_path = tmp_path / "provider-provenance-populated.db"
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO items (source) VALUES ('plaid')"))
        account_id = connection.scalar(
            text(
                "INSERT INTO accounts (item_id, plaid_account_id, name, type, currency) "
                "VALUES (1, 'provider-account', 'Brokerage', 'investment', 'USD') "
                "RETURNING account_id"
            )
        )
        connection.execute(
            text(
                "INSERT INTO cashflow_source_attestations "
                "(attestation_key, account_id, coverage_start, coverage_end, source_type, "
                "broker_archive_coverage, source_reference, source_sha256, captured_at, "
                "methodology_version) VALUES "
                "(:key, :account_id, '2025-01-01', '2025-01-31', 'provider_api', "
                "'unasserted', 'provider_api:fixture', :sha, '2025-02-01', 'provider-api-v1')"
            ),
            {"key": "a" * 64, "account_id": account_id, "sha": "b" * 64},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="cannot downgrade 0029"):
        command.downgrade(config, "0028")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0029"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM cashflow_source_attestations "
                    "WHERE source_type = 'provider_api'"
                )
            )
            == 1
        )
    assert inspect(engine).has_table("cashflow_source_attestation_event_links")
    engine.dispose()
    get_settings.cache_clear()
