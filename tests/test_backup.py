from __future__ import annotations

import re
import sqlite3
from datetime import date as real_date

from portfolio_tracker.config import get_settings
from portfolio_tracker.jobs import backup


def test_backup_uses_a_distinct_timestamped_filename(tmp_path, monkeypatch):
    database_path = tmp_path / "source.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE proof (value INTEGER NOT NULL)")
        connection.execute("INSERT INTO proof VALUES (1)")

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setattr(backup, "PROJECT_ROOT", tmp_path)
    get_settings.cache_clear()
    try:
        first = backup.run()
        second = backup.run()
    finally:
        get_settings.cache_clear()

    pattern = re.compile(r"portfolio_\d{4}-\d{2}-\d{2}T\d{6}\.\d{6}Z\.db")
    assert pattern.fullmatch(first.name)
    assert pattern.fullmatch(second.name)
    assert first != second
    assert first.exists()
    assert second.exists()
    with sqlite3.connect(second) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_prune_understands_timestamped_and_legacy_names(tmp_path, monkeypatch):
    old_timestamped = tmp_path / "portfolio_2020-01-02T030405.000000Z.db"
    old_legacy = tmp_path / "portfolio_2020-01-02.db"
    unrelated = tmp_path / "portfolio_not-a-date.db"
    for path in (old_timestamped, old_legacy, unrelated):
        path.touch()

    class _FixedDate:
        @classmethod
        def today(cls):
            return real_date(2026, 9, 3)

        @classmethod
        def fromisoformat(cls, value: str):
            return real_date.fromisoformat(value)

    monkeypatch.setattr(backup, "date", _FixedDate)
    backup._prune(tmp_path, keep_days=30)

    assert not old_timestamped.exists()
    assert not old_legacy.exists()
    assert unrelated.exists()
