"""`/api/v1/operations`: catalog, read-only previews, and run status.

There is no HTTP run endpoint. Runs start from the CLI on the canonical host
(`python -m portfolio_tracker.jobs.operations run ...`), so an agent tool or a
browser can never commit a change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from portfolio_tracker.db import get_session
from portfolio_tracker.services import operations
from portfolio_tracker.services.v1_common import V1_SCHEMA_VERSION

router = APIRouter(prefix="/api/v1/operations", tags=["operations-v1"])


class OperationsCatalogV1(BaseModel):
    schema_version: str
    generated_at: datetime
    operations: list[operations.OperationV1]


class OperationPreviewRequestV1(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict[str, Any])


def _database_path(session: Session) -> Path:
    database = session.get_bind().engine.url.database
    if not database or database == ":memory:":
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "file_database_required"})
    return Path(database)


@router.get("", response_model=OperationsCatalogV1)
def operations_catalog() -> OperationsCatalogV1:
    """Every governed operation: effect class, inputs, preview support, backup need."""
    return OperationsCatalogV1(
        schema_version=V1_SCHEMA_VERSION,
        generated_at=datetime.now(UTC),
        operations=operations.catalog(),
    )


@router.post("/{name}/preview", response_model=operations.OperationPreviewV1)
def operation_preview(
    name: str,
    session: Annotated[Session, Depends(get_session)],
    request: OperationPreviewRequestV1 | None = None,
) -> operations.OperationPreviewV1:
    """Read-only plan and digest for one operation. Writes nothing."""
    try:
        return operations.preview(
            name, _database_path(session), request.inputs if request else None
        )
    except operations.OperationError as exc:
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code == "unknown_operation"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(code, detail={"code": exc.code}) from None


@router.get("/runs/{run_id}", response_model=operations.OperationRunV1)
def operation_run(
    run_id: str, session: Annotated[Session, Depends(get_session)]
) -> operations.OperationRunV1:
    """Status and value-free receipt of one run."""
    found = operations.get_run(_database_path(session), run_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "run_not_found"})
    return found
