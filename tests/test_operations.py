"""Governed operations: catalog, read-only preview, digest-bound idempotent runs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from portfolio_tracker.config import get_settings
from portfolio_tracker.jobs import operations as operations_cli
from portfolio_tracker.services import operations
from portfolio_tracker.services.operations import OperationError


def _db_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str) -> Path:
    path = tmp_path / "portfolio.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path.as_posix()}")
    get_settings.cache_clear()
    command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), revision)
    get_settings.cache_clear()
    return path


def test_catalog_lists_service_and_cli_operations() -> None:
    names = {op.name: op for op in operations.catalog()}

    assert names["schema_migrate"].interface == "service"
    assert names["schema_migrate"].backup_required is True
    assert names["reconcile_cashflow_manifest"].interface == "cli"
    assert {op.effect_class for op in names.values()} <= {
        "read",
        "local_write",
        "external_call",
        "destructive",
    }
    with pytest.raises(OperationError, match="preview_via_cli"):
        operations.preview("daily_refresh", Path("unused.db"))


def test_first_schema_run_records_itself_and_replays_by_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db_at(tmp_path, monkeypatch, "0032")
    plan = operations.preview("schema_migrate", db)

    first = operations.run(
        "schema_migrate", db, expected_plan_digest=plan.plan_digest, idempotency_key="k-1"
    )
    again = operations.run(
        "schema_migrate", db, expected_plan_digest=plan.plan_digest, idempotency_key="k-1"
    )

    assert first.status == "succeeded"
    assert first.receipt is not None and first.receipt["applied"] is True
    assert again.replayed is True
    assert again.run_id == first.run_id
    stored = operations.get_run(db, first.run_id)
    assert stored is not None and stored.status == "succeeded"
    with pytest.raises(OperationError, match="idempotency_key_reused"):
        operations.run("backup", db, expected_plan_digest="x" * 64, idempotency_key="k-1")


def test_digest_mismatch_is_refused_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db_at(tmp_path, monkeypatch, "head")

    result = operations.run("backup", db, expected_plan_digest="0" * 64, idempotency_key="b-1")

    assert result.status == "refused"
    assert result.error_code == "plan_digest_mismatch"
    assert not (tmp_path / "backups").exists()
    stored = operations.get_run(db, result.run_id)
    assert stored is not None and stored.error_code == "plan_digest_mismatch"


def test_backup_run_writes_a_verified_backup_and_a_value_free_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db_at(tmp_path, monkeypatch, "head")
    plan = operations.preview("backup", db)

    result = operations.run(
        "backup", db, expected_plan_digest=plan.plan_digest, idempotency_key="b-2"
    )

    assert result.status == "succeeded"
    assert result.receipt is not None
    backup = result.receipt["backup"]
    assert set(backup) == {"file_name", "size_bytes", "sha256", "revision", "integrity"}
    assert backup["integrity"] == "ok"
    assert (tmp_path / "backups" / backup["file_name"]).is_file()


def test_cli_preview_then_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _db_at(tmp_path, monkeypatch, "0032")

    assert operations_cli.main(["preview", "schema_migrate", "--database-path", str(db)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["summary"]["classification"] == "additive"
    assert (
        operations_cli.main(
            [
                "run",
                "schema_migrate",
                "--database-path",
                str(db),
                "--expected-plan-digest",
                "0" * 64,
                "--idempotency-key",
                "cli-1",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error_code"] == "plan_digest_mismatch"
    assert (
        operations_cli.main(
            [
                "run",
                "schema_migrate",
                "--database-path",
                str(db),
                "--expected-plan-digest",
                preview["plan_digest"],
                "--idempotency-key",
                "cli-2",
            ]
        )
        == 0
    )
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM operation_runs").fetchone()[0] == 1
    conn.close()


@pytest.fixture
def file_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[TestClient, Path]]:
    from portfolio_tracker.api.main import app
    from portfolio_tracker.db import get_session

    db = _db_at(tmp_path, monkeypatch, "head")
    engine = create_engine(f"sqlite:///{db.as_posix()}")

    def _override() -> Generator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = _override
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            yield client, db
    finally:
        app.dependency_overrides.pop(get_session, None)
        engine.dispose()


def test_http_catalog_preview_and_status(file_client: tuple[TestClient, Path]) -> None:
    client, db = file_client

    catalog = client.get("/api/v1/operations")
    preview = client.post("/api/v1/operations/schema_migrate/preview", json={"inputs": {}})
    unknown = client.post("/api/v1/operations/drop_everything/preview")
    no_run = client.post("/api/v1/operations/schema_migrate/runs", json={})

    assert catalog.status_code == 200
    assert preview.status_code == 200
    body: dict[str, Any] = preview.json()
    assert body["summary"]["classification"] == "noop"
    assert "--expected-plan-digest" in body["run_command"]
    assert unknown.status_code == 404
    assert no_run.status_code in {404, 405}
    backup_plan = client.post("/api/v1/operations/backup/preview").json()
    run = operations.run(
        "backup", db, expected_plan_digest=backup_plan["plan_digest"], idempotency_key="h-1"
    )
    status = client.get(f"/api/v1/operations/runs/{run.run_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert client.get("/api/v1/operations/runs/missing").status_code == 404


def test_in_memory_database_cannot_preview(client: Any) -> None:
    response = client.post("/api/v1/operations/backup/preview")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "file_database_required"
