"""Add the governed operation run log.

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operation_runs",
        sa.Column("run_id", sa.String(32), primary_key=True),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_operation_runs_idempotency_key"),
    )
    op.create_index(
        "ix_operation_runs_operation_requested_at",
        "operation_runs",
        ["operation", "requested_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_operation_runs_operation_requested_at", table_name="operation_runs")
    op.drop_table("operation_runs")
