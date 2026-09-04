"""Persist the requested return window on reconciliation run receipts.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing 0026 receipts predate explicit return-scope provenance and keep
    # the all-null legacy shape. New schema-v3 runs always populate both dates.
    with op.batch_alter_table("cashflow_reconciliation_runs") as batch_op:
        batch_op.add_column(sa.Column("requested_return_start", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("requested_return_end", sa.Date(), nullable=True))
        batch_op.create_check_constraint(
            "ck_cashflow_reconciliation_runs_requested_return_window",
            "(requested_return_start IS NULL AND requested_return_end IS NULL) OR "
            "(requested_return_start IS NOT NULL AND requested_return_end IS NOT NULL AND "
            "requested_return_start < requested_return_end)",
        )


def downgrade() -> None:
    with op.batch_alter_table("cashflow_reconciliation_runs") as batch_op:
        batch_op.drop_constraint(
            "ck_cashflow_reconciliation_runs_requested_return_window",
            type_="check",
        )
        batch_op.drop_column("requested_return_end")
        batch_op.drop_column("requested_return_start")
