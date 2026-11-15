"""Migrations and models must agree. Known historical drift is pinned; new drift fails.

This is the standing check for every database addition: a new model without a
migration (or a migration without its model) appears here as new drift.
Remove an entry from KNOWN_DRIFT when a migration retires it; never add one to
make a new change pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from portfolio_tracker.config import get_settings
from portfolio_tracker.models import Base

# Retired features whose tables stay in old databases, plus two legacy mismatches.
KNOWN_DRIFT = frozenset(
    {
        ("remove_table", "action_queue"),
        ("remove_table", "chat_sessions"),
        ("remove_table", "chat_turns"),
        ("remove_table", "earnings_calendar"),
        ("remove_table", "human_capital_overlap"),
        ("remove_table", "monthly_brief_jobs"),
        ("remove_table", "monthly_briefs"),
        ("remove_table", "trade_decisions"),
        ("remove_table", "trade_tags"),
        ("remove_index", "ix_action_queue_status"),
        ("remove_index", "ix_chat_turns_session"),
        ("remove_index", "ix_monthly_brief_jobs_period"),
        ("remove_index", "ix_monthly_brief_jobs_status"),
        ("remove_index", "ix_trade_decisions_ticker_date"),
        ("remove_index", "ix_trade_tags_ticker"),
        ("modify_nullable", "provider_transaction_correction_receipts.created_at"),
        ("remove_constraint", "ck_transaction_overrides_classification"),
    }
)


def _key(diff: Any) -> tuple[str, str]:
    if isinstance(diff, list):  # column-level changes arrive as a list of tuples
        kind, _schema, table, column = diff[0][:4]
        return str(kind), f"{table}.{column}"
    kind, obj = diff[0], diff[1]
    return str(kind), str(getattr(obj, "name", obj))


def test_migrated_schema_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "drift.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path.as_posix()}")
    get_settings.cache_clear()
    command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), "head")
    get_settings.cache_clear()
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": False})
        drift = {_key(diff) for diff in compare_metadata(context, Base.metadata)}
    engine.dispose()

    assert drift - KNOWN_DRIFT == set(), "new schema drift: add the migration or the model"
    assert KNOWN_DRIFT - drift == set(), "drift retired: remove it from KNOWN_DRIFT"
