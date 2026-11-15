"""Tested fast path for schema changes (the `schema_migrate` operation).

Preview is a rehearsal: copy the live SQLite file into memory with the online
backup API, run every pending Alembic revision on the copy, and compare before
and after. The live file is opened read-only and nothing is written to disk.

A change is **additive** when every pre-existing table keeps its columns,
indexes, and row content (hashed, never printed), and the copy passes
`integrity_check` and `foreign_key_check`. Only new tables, columns, and indexes
appear. An additive plan may be applied with its digest alone. Any other plan is
`requires_approval` and needs an explicit owner approval flag; a failing
rehearsal is `blocked`.

Apply re-runs the rehearsal, requires the same digest, writes a verified backup
(integrity and revision checked on the backup file), upgrades the live file, and
proves the live schema equals the rehearsed schema. On a failed post-check the
backup file is the recovery point; nothing is restored automatically.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

Classification = Literal["noop", "additive", "requires_approval", "blocked"]


class MigrationRefusedError(RuntimeError):
    """The plan cannot be applied as requested. `code` is a stable reason."""

    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


@dataclass(frozen=True)
class SchemaSnapshot:
    revision: str | None
    tables: dict[str, list[list[Any]]]  # table -> PRAGMA table_info rows (cid dropped)
    indexes: dict[str, str]  # index name -> CREATE sql
    content: dict[str, str]  # table -> sha256 of rows over its columns (never printed)

    def schema_sha256(self) -> str:
        return _sha256({"tables": self.tables, "indexes": self.indexes})


@dataclass(frozen=True)
class MigrationPlan:
    from_revision: str | None
    to_revision: str | None
    revisions: list[str]
    classification: Classification
    added_tables: list[str] = field(default_factory=list[str])
    added_columns: list[str] = field(default_factory=list[str])
    added_indexes: list[str] = field(default_factory=list[str])
    changed: list[str] = field(default_factory=list[str])
    integrity: str = "ok"
    error: str | None = None
    before_schema_sha256: str = ""
    after_schema_sha256: str = ""
    plan_digest: str = ""

    def summary(self) -> dict[str, Any]:
        """Names, revisions, and counts only. Safe to show and log."""
        data = asdict(self)
        data.pop("plan_digest")
        return data


@dataclass(frozen=True)
class BackupRef:
    file_name: str
    size_bytes: int
    sha256: str
    revision: str | None
    integrity: str


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _revision(conn: sqlite3.Connection) -> str | None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    if exists is None:
        return None
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return None if row is None else str(row[0])


def snapshot(
    conn: sqlite3.Connection, *, content_columns: dict[str, list[str]] | None = None
) -> SchemaSnapshot:
    """Schema plus per-table content hashes.

    `content_columns` limits each table's hash to the given columns, so a new
    column with a default does not count as a content change.
    """
    tables: dict[str, list[list[Any]]] = {}
    content: dict[str, str] = {}
    names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    for name in names:
        info = [list(row)[1:] for row in conn.execute(f"PRAGMA table_info({_quote(name)})")]
        tables[name] = info
        if name == "alembic_version":
            continue
        columns = [str(col[0]) for col in info]
        if content_columns is not None and name in content_columns:
            columns = [c for c in content_columns[name] if c in columns]
        digest = hashlib.sha256()
        select = ", ".join(_quote(c) for c in columns) or "1"
        for row in conn.execute(f"SELECT {select} FROM {_quote(name)} ORDER BY rowid"):
            digest.update(repr(tuple(row)).encode("utf-8"))
        content[name] = digest.hexdigest()
    indexes = {
        str(row[0]): str(row[1])
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('index','trigger','view') "
            "AND sql IS NOT NULL ORDER BY name"
        )
    }
    return SchemaSnapshot(_revision(conn), tables, indexes, content)


def classify(
    before: SchemaSnapshot, after: SchemaSnapshot
) -> tuple[Classification, dict[str, list[str]]]:
    added_tables = sorted(set(after.tables) - set(before.tables))
    added_columns: list[str] = []
    added_indexes = sorted(set(after.indexes) - set(before.indexes))
    changed: list[str] = []
    for table, columns in before.tables.items():
        if table == "alembic_version":
            continue
        if table not in after.tables:
            changed.append(f"table removed: {table}")
            continue
        after_columns = {str(c[0]): c for c in after.tables[table]}
        for column in columns:
            if str(column[0]) not in after_columns:
                changed.append(f"column removed: {table}.{column[0]}")
            elif after_columns[str(column[0])] != column:
                changed.append(f"column changed: {table}.{column[0]}")
        before_names = {str(c[0]) for c in columns}
        for name, column in after_columns.items():
            if name in before_names:
                continue
            not_null, default = column[2], column[3]
            if not_null and default is None:
                changed.append(f"column not null without default: {table}.{name}")
            added_columns.append(f"{table}.{name}")
        if before.content.get(table) != after.content.get(table):
            changed.append(f"row content changed: {table}")
    for index, sql in before.indexes.items():
        if after.indexes.get(index) != sql:
            changed.append(f"index removed or changed: {index}")
    detail = {
        "added_tables": added_tables,
        "added_columns": sorted(added_columns),
        "added_indexes": added_indexes,
        "changed": sorted(changed),
    }
    return ("requires_approval" if changed else "additive"), detail


def _alembic_config(connection: Any = None) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revision() -> str | None:
    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def _pending_revisions(from_revision: str | None, head: str | None) -> list[str]:
    script = ScriptDirectory.from_config(_alembic_config())
    if head is None or from_revision == head:
        return []
    steps = list(script.iterate_revisions(head, from_revision or "base"))
    return [str(step.revision) for step in reversed(steps)]


def _upgrade(conn: sqlite3.Connection) -> None:
    engine = create_engine("sqlite://", creator=lambda: conn, poolclass=StaticPool)
    with engine.begin() as connection:
        command.upgrade(_alembic_config(connection), "head")


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise MigrationRefusedError("database_not_found")
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def _finish(plan: MigrationPlan) -> MigrationPlan:
    digest = _sha256({k: v for k, v in asdict(plan).items() if k != "plan_digest"})
    return replace(plan, plan_digest=digest)


def rehearse(db_path: Path) -> MigrationPlan:
    """Read-only plan for upgrading `db_path` to head. Writes nothing to disk."""
    source = _open_read_only(db_path)
    memory = sqlite3.connect(":memory:")
    try:
        source.backup(memory)
    finally:
        source.close()
    try:
        before = snapshot(memory)
        head = head_revision()
        revisions = _pending_revisions(before.revision, head)
        if not revisions:
            return _finish(
                MigrationPlan(
                    from_revision=before.revision,
                    to_revision=head,
                    revisions=[],
                    classification="noop",
                    before_schema_sha256=before.schema_sha256(),
                    after_schema_sha256=before.schema_sha256(),
                )
            )
        try:
            _upgrade(memory)
        except Exception as exc:  # the rehearsal reports, never raises, a failed revision
            return _finish(
                MigrationPlan(
                    from_revision=before.revision,
                    to_revision=head,
                    revisions=revisions,
                    classification="blocked",
                    error=type(exc).__name__,
                    before_schema_sha256=before.schema_sha256(),
                )
            )
        original_columns = {t: [str(c[0]) for c in cols] for t, cols in before.tables.items()}
        after = snapshot(memory, content_columns=original_columns)
        integrity = str(memory.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = memory.execute("PRAGMA foreign_key_check").fetchall()
        classification, detail = classify(before, after)
        if integrity != "ok" or foreign_keys or after.revision != head:
            classification = "blocked"
        return _finish(
            MigrationPlan(
                from_revision=before.revision,
                to_revision=head,
                revisions=revisions,
                classification=classification,
                integrity=integrity if not foreign_keys else "foreign_key_violations",
                before_schema_sha256=before.schema_sha256(),
                after_schema_sha256=after.schema_sha256(),
                added_tables=detail["added_tables"],
                added_columns=detail["added_columns"],
                added_indexes=detail["added_indexes"],
                changed=detail["changed"],
            )
        )
    finally:
        memory.close()


def take_verified_backup(db_path: Path, backup_dir: Path, *, label: str) -> BackupRef:
    """Online-backup `db_path`, then prove the file opens and passes integrity_check."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%S.%fZ")
    target = backup_dir / f"portfolio_{stamp}_{label}.db"
    source = _open_read_only(db_path)
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    check = _open_read_only(target)
    try:
        integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
        revision = _revision(check)
    finally:
        check.close()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    ref = BackupRef(target.name, target.stat().st_size, digest, revision, integrity)
    if integrity != "ok":
        raise MigrationRefusedError("backup_integrity_failed")
    return ref


def apply(
    db_path: Path,
    *,
    expected_plan_digest: str,
    approve_non_additive: bool = False,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """Apply a previewed plan to the live file. Returns a value-free receipt."""
    plan = rehearse(db_path)
    if plan.plan_digest != expected_plan_digest:
        raise MigrationRefusedError("plan_digest_mismatch")
    if plan.classification == "noop":
        return {"applied": False, "plan": plan.summary()}
    if plan.classification == "blocked":
        raise MigrationRefusedError("rehearsal_blocked")
    if plan.classification == "requires_approval" and not approve_non_additive:
        raise MigrationRefusedError("owner_approval_required")
    backup = take_verified_backup(
        db_path, backup_dir or db_path.parent / "backups", label=f"pre-{plan.to_revision}"
    )
    if backup.revision != plan.from_revision:
        raise MigrationRefusedError("backup_revision_mismatch")
    live = sqlite3.connect(str(db_path))
    try:
        _upgrade(live)
        after = snapshot(live)
        integrity = str(live.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        live.close()
    receipt: dict[str, Any] = {
        "applied": True,
        "plan": plan.summary(),
        "backup": asdict(backup),
        "post_revision": after.revision,
        "post_integrity": integrity,
        "post_schema_matches_rehearsal": after.schema_sha256() == plan.after_schema_sha256,
    }
    if not (receipt["post_schema_matches_rehearsal"] and integrity == "ok"):
        raise MigrationRefusedError("post_check_failed", receipt)
    return receipt
