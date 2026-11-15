"""Governed operations: catalog -> preview (plan digest) -> run -> receipt.

One contract for state-changing work. Preview never writes. A run needs the
exact preview digest and an idempotency key; a repeated key returns the first
run instead of running again. Every run is recorded in `operation_runs` with a
value-free receipt (revisions, counts, file names; never balances).

Runs are started from the CLI (`python -m portfolio_tracker.jobs.operations`),
never from an agent tool: agents get the catalog, previews, and run status.
Operations marked `interface="cli"` keep their own preview/commit CLI and are
listed so the catalog is complete.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from portfolio_tracker.services import schema_migration

EffectClass = Literal["read", "local_write", "external_call", "destructive"]
RunStatus = Literal["running", "succeeded", "failed", "refused"]


class OperationError(RuntimeError):
    """A stable, value-free refusal reason."""

    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


class OperationV1(BaseModel):
    name: str
    effect_class: EffectClass
    description: str
    interface: Literal["service", "cli"]
    preview_supported: bool
    backup_required: bool
    inputs: dict[str, str]
    cli: str


class OperationPreviewV1(BaseModel):
    operation: str
    effect_class: EffectClass
    backup_required: bool
    plan_digest: str
    summary: dict[str, Any]
    run_command: str


class OperationRunV1(BaseModel):
    run_id: str
    operation: str
    idempotency_key: str
    plan_digest: str
    status: RunStatus
    requested_at: datetime
    finished_at: datetime | None
    receipt: dict[str, Any] | None
    error_code: str | None
    replayed: bool = False


@dataclass(frozen=True)
class _Preview:
    plan_digest: str
    summary: dict[str, Any]


@dataclass(frozen=True)
class _Spec:
    model: OperationV1
    preview: Callable[[Path, dict[str, Any]], _Preview] | None = None
    run: Callable[[Path, dict[str, Any], str], dict[str, Any]] | None = None


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# -- schema_migrate -----------------------------------------------------------


def _schema_preview(db_path: Path, inputs: dict[str, Any]) -> _Preview:
    plan = schema_migration.rehearse(db_path)
    return _Preview(plan.plan_digest, plan.summary())


def _schema_run(db_path: Path, inputs: dict[str, Any], digest: str) -> dict[str, Any]:
    try:
        return schema_migration.apply(
            db_path,
            expected_plan_digest=digest,
            approve_non_additive=bool(inputs.get("approve_non_additive")),
        )
    except schema_migration.MigrationRefusedError as exc:
        raise OperationError(exc.code, exc.detail) from None


# -- backup -------------------------------------------------------------------


def _backups(db_path: Path) -> list[Path]:
    return sorted((db_path.parent / "backups").glob("portfolio_*.db"))


def _backup_preview(db_path: Path, inputs: dict[str, Any]) -> _Preview:
    if not db_path.is_file():
        raise OperationError("database_not_found")
    backups = _backups(db_path)
    latest = backups[-1] if backups else None
    age_hours = (
        round((datetime.now(UTC).timestamp() - latest.stat().st_mtime) / 3600, 1)
        if latest is not None
        else None
    )
    summary: dict[str, Any] = {
        "database_mb": round(db_path.stat().st_size / (1024 * 1024), 1),
        "existing_backups": len(backups),
        "latest_backup_age_hours": age_hours,
        "backup_dir": "backups/ next to the database",
    }
    # The digest binds the database identity, not its changing contents.
    return _Preview(_digest({"operation": "backup", "database": db_path.name}), summary)


def _backup_run(db_path: Path, inputs: dict[str, Any], digest: str) -> dict[str, Any]:
    if _backup_preview(db_path, inputs).plan_digest != digest:
        raise OperationError("plan_digest_mismatch")
    try:
        ref = schema_migration.take_verified_backup(
            db_path, db_path.parent / "backups", label="manual"
        )
    except schema_migration.MigrationRefusedError as exc:
        raise OperationError(exc.code) from None
    return {"backup": ref.__dict__}


# -- catalog ------------------------------------------------------------------

_RUN = "python -m portfolio_tracker.jobs.operations"


def _cli_entry(
    name: str, effect: EffectClass, description: str, cli: str, *, backup: bool
) -> _Spec:
    return _Spec(
        OperationV1(
            name=name,
            effect_class=effect,
            description=description,
            interface="cli",
            preview_supported=True,
            backup_required=backup,
            inputs={},
            cli=cli,
        )
    )


_SPECS: dict[str, _Spec] = {
    "schema_migrate": _Spec(
        OperationV1(
            name="schema_migrate",
            effect_class="local_write",
            description=(
                "Apply pending Alembic revisions. Preview rehearses them on an in-memory copy "
                "and classifies the change as noop, additive, requires_approval, or blocked."
            ),
            interface="service",
            preview_supported=True,
            backup_required=True,
            inputs={
                "approve_non_additive": "Run only: owner approval for a requires_approval plan."
            },
            cli=f"{_RUN} run schema_migrate",
        ),
        _schema_preview,
        _schema_run,
    ),
    "backup": _Spec(
        OperationV1(
            name="backup",
            effect_class="local_write",
            description="Write a verified SQLite backup next to the database.",
            interface="service",
            preview_supported=True,
            backup_required=False,
            inputs={},
            cli=f"{_RUN} run backup",
        ),
        _backup_preview,
        _backup_run,
    ),
    "daily_refresh": _cli_entry(
        "daily_refresh",
        "external_call",
        "Provider snapshot and sync, then the value cache.",
        "python -m portfolio_tracker.jobs.daily_refresh",
        backup=False,
    ),
    "benchmarks": _cli_entry(
        "benchmarks",
        "external_call",
        "Fetch benchmark closes (SPY, QQQ, policy tickers).",
        "python -m portfolio_tracker.jobs.benchmarks",
        backup=False,
    ),
    "reconcile_cashflow_manifest": _cli_entry(
        "reconcile_cashflow_manifest",
        "local_write",
        "Attest cash-flow sources from private manifests (preview, digest, --commit).",
        "python -m portfolio_tracker.jobs.reconcile_cashflow_manifest",
        backup=True,
    ),
    "import_account_valuation_manifest": _cli_entry(
        "import_account_valuation_manifest",
        "local_write",
        "Import broker account valuations from a manifest (preview, digest, --commit).",
        "python -m portfolio_tracker.jobs.import_account_valuation_manifest",
        backup=True,
    ),
}


def catalog() -> list[OperationV1]:
    return [spec.model for spec in _SPECS.values()]


def _spec(name: str) -> _Spec:
    spec = _SPECS.get(name)
    if spec is None:
        raise OperationError("unknown_operation")
    return spec


def preview(name: str, db_path: Path, inputs: dict[str, Any] | None = None) -> OperationPreviewV1:
    spec = _spec(name)
    if spec.preview is None:
        raise OperationError("preview_via_cli")
    result = spec.preview(db_path, dict(inputs or {}))
    return OperationPreviewV1(
        operation=name,
        effect_class=spec.model.effect_class,
        backup_required=spec.model.backup_required,
        plan_digest=result.plan_digest,
        summary=result.summary,
        run_command=(
            f"{spec.model.cli} --expected-plan-digest {result.plan_digest} "
            "--idempotency-key <unique key>"
        ),
    )


# -- run log ------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _has_run_log(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_runs'"
    ).fetchone()
    return row is not None


def _row_to_run(row: sqlite3.Row, *, replayed: bool = False) -> OperationRunV1:
    receipt = row["receipt_json"]
    return OperationRunV1(
        run_id=row["run_id"],
        operation=row["operation"],
        idempotency_key=row["idempotency_key"],
        plan_digest=row["plan_digest"],
        status=row["status"],
        requested_at=datetime.fromisoformat(row["requested_at"]),
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        receipt=json.loads(receipt) if receipt else None,
        error_code=row["error_code"],
        replayed=replayed,
    )


def get_run(db_path: Path, run_id: str) -> OperationRunV1 | None:
    conn = _connect(db_path)
    try:
        if not _has_run_log(conn):
            return None
        row = conn.execute("SELECT * FROM operation_runs WHERE run_id = ?", (run_id,)).fetchone()
        return None if row is None else _row_to_run(row)
    finally:
        conn.close()


def _find_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM operation_runs WHERE idempotency_key = ?", (key,)).fetchone()


def _upsert(conn: sqlite3.Connection, run: OperationRunV1) -> None:
    conn.execute(
        "INSERT INTO operation_runs (run_id, operation, idempotency_key, plan_digest, status, "
        "requested_at, finished_at, receipt_json, error_code) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, "
        "finished_at=excluded.finished_at, receipt_json=excluded.receipt_json, "
        "error_code=excluded.error_code",
        (
            run.run_id,
            run.operation,
            run.idempotency_key,
            run.plan_digest,
            run.status,
            run.requested_at.isoformat(),
            run.finished_at.isoformat() if run.finished_at else None,
            json.dumps(run.receipt, sort_keys=True, default=str)
            if run.receipt is not None
            else None,
            run.error_code,
        ),
    )
    conn.commit()


def run(
    name: str,
    db_path: Path,
    *,
    expected_plan_digest: str,
    idempotency_key: str,
    inputs: dict[str, Any] | None = None,
) -> OperationRunV1:
    """Run one service operation. A repeated idempotency key returns the first run."""
    spec = _spec(name)
    if spec.run is None:
        raise OperationError("run_via_cli")
    if not idempotency_key.strip() or len(idempotency_key) > 128:
        raise OperationError("invalid_idempotency_key")
    conn = _connect(db_path)
    try:
        if _has_run_log(conn):
            existing = _find_key(conn, idempotency_key)
            if existing is not None:
                if existing["operation"] != name or existing["plan_digest"] != expected_plan_digest:
                    raise OperationError("idempotency_key_reused")
                return _row_to_run(existing, replayed=True)
        record = OperationRunV1(
            run_id=uuid.uuid4().hex,
            operation=name,
            idempotency_key=idempotency_key,
            plan_digest=expected_plan_digest,
            status="running",
            requested_at=datetime.now(UTC),
            finished_at=None,
            receipt=None,
            error_code=None,
        )
        if _has_run_log(conn):
            _upsert(conn, record)
    finally:
        conn.close()

    try:
        receipt = spec.run(db_path, dict(inputs or {}), expected_plan_digest)
        status: RunStatus = "succeeded"
        error_code: str | None = None
    except OperationError as exc:
        receipt = exc.detail or None
        status = "refused"
        error_code = exc.code
    except Exception as exc:  # recorded with its type only; the CLI exits non-zero
        receipt = None
        status = "failed"
        error_code = type(exc).__name__
    final = record.model_copy(
        update={
            "status": status,
            "finished_at": datetime.now(UTC),
            "receipt": receipt,
            "error_code": error_code,
        }
    )
    conn = _connect(db_path)
    try:
        # schema_migrate may have just created the run log; record the run either way.
        if _has_run_log(conn):
            _upsert(conn, final)
    finally:
        conn.close()
    return final
