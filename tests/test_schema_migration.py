"""The schema_migrate fast path: in-memory rehearsal, additive proof, verified apply."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

from portfolio_tracker.config import get_settings
from portfolio_tracker.services import schema_migration
from portfolio_tracker.services.schema_migration import (
    MigrationRefusedError,
    SchemaSnapshot,
    apply,
    classify,
    head_revision,
    rehearse,
)

PREVIOUS = "0032"


def _db_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str) -> Path:
    path = tmp_path / "portfolio.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path.as_posix()}")
    get_settings.cache_clear()
    command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), revision)
    get_settings.cache_clear()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO policy_weights (ticker, weight_bps, notes, updated_at) "
        "VALUES ('VOO', 10000, NULL, '2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()
    return path


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _revision(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        conn.close()


def test_rehearsal_is_read_only_and_proves_the_run_log_is_additive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db_at(tmp_path, monkeypatch, PREVIOUS)
    before = _file_sha(db)

    plan = rehearse(db)

    assert _file_sha(db) == before
    assert not (tmp_path / "backups").exists()
    assert plan.classification == "additive"
    assert plan.from_revision == PREVIOUS
    assert plan.to_revision == head_revision()
    assert "operation_runs" in plan.added_tables
    assert plan.changed == []
    assert plan.integrity == "ok"
    assert rehearse(db).plan_digest == plan.plan_digest


def test_apply_needs_the_previewed_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db_at(tmp_path, monkeypatch, PREVIOUS)

    with pytest.raises(MigrationRefusedError) as refused:
        apply(db, expected_plan_digest="0" * 64)

    assert refused.value.code == "plan_digest_mismatch"
    assert _revision(db) == PREVIOUS


def test_apply_backs_up_then_upgrades_and_keeps_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db_at(tmp_path, monkeypatch, PREVIOUS)
    conn = sqlite3.connect(db)
    rows_before = conn.execute("SELECT * FROM policy_weights ORDER BY ticker").fetchall()
    conn.close()
    plan = rehearse(db)

    receipt = apply(db, expected_plan_digest=plan.plan_digest)

    assert receipt["applied"] is True
    assert receipt["post_schema_matches_rehearsal"] is True
    assert receipt["post_revision"] == head_revision()
    backup = tmp_path / "backups" / receipt["backup"]["file_name"]
    assert backup.is_file()
    assert _revision(backup) == PREVIOUS
    assert receipt["backup"]["integrity"] == "ok"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT * FROM policy_weights ORDER BY ticker").fetchall() == rows_before
    conn.close()
    assert rehearse(db).classification == "noop"


def test_non_additive_plan_needs_owner_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db_at(tmp_path, monkeypatch, PREVIOUS)
    real = rehearse(db)
    risky = schema_migration._finish(  # pyright: ignore[reportPrivateUsage]
        replace(real, classification="requires_approval", changed=["row content changed: x"])
    )
    monkeypatch.setattr(schema_migration, "rehearse", lambda _path: risky)

    with pytest.raises(MigrationRefusedError) as refused:
        apply(db, expected_plan_digest=risky.plan_digest)

    assert refused.value.code == "owner_approval_required"
    assert _revision(db) == PREVIOUS


def _snap(
    tables: dict[str, list[list[Any]]],
    content: dict[str, str],
    indexes: dict[str, str] | None = None,
) -> SchemaSnapshot:
    return SchemaSnapshot("1", tables, indexes or {}, content)


COLS: dict[str, list[list[Any]]] = {"t": [["id", "INTEGER", 1, None, 1], ["v", "TEXT", 0, None, 0]]}


@pytest.mark.parametrize(
    ("after_tables", "after_content", "after_indexes", "expected"),
    [
        ({**COLS, "new": [["id", "INTEGER", 1, None, 1]]}, {"t": "h", "new": "e"}, {}, "additive"),
        ({"t": [*COLS["t"], ["n", "TEXT", 0, None, 0]]}, {"t": "h"}, {}, "additive"),
        ({"t": [*COLS["t"], ["n", "TEXT", 1, None, 0]]}, {"t": "h"}, {}, "requires_approval"),
        ({"t": COLS["t"][:1]}, {"t": "h"}, {}, "requires_approval"),
        (COLS, {"t": "changed"}, {}, "requires_approval"),
        ({"t": [COLS["t"][0], ["v", "INTEGER", 0, None, 0]]}, {"t": "h"}, {}, "requires_approval"),
        (COLS, {"t": "h"}, {"ix": "CREATE INDEX ix ON t (v DESC)"}, "requires_approval"),
        (
            COLS,
            {"t": "h"},
            {"ix": "CREATE INDEX ix ON t (v)", "ix2": "CREATE INDEX ix2 ON t (id)"},
            "additive",
        ),
    ],
)
def test_classification(
    after_tables: dict[str, list[list[Any]]],
    after_content: dict[str, str],
    after_indexes: dict[str, str],
    expected: str,
) -> None:
    before = _snap(COLS, {"t": "h"}, {"ix": "CREATE INDEX ix ON t (v)"})
    after = _snap(after_tables, after_content, after_indexes or {"ix": "CREATE INDEX ix ON t (v)"})

    classification, _detail = classify(before, after)

    assert classification == expected
