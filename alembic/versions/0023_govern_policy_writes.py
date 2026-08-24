"""Add policy revision, invalidation state, and durable write receipts.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "policy_state",
        sa.Column("singleton_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("benchmark_status", sa.String(length=16), nullable=False),
        sa.Column("benchmark_invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("singleton_id = 1", name="ck_policy_state_singleton"),
        sa.CheckConstraint("revision >= 0", name="ck_policy_state_revision_nonnegative"),
        sa.CheckConstraint(
            "benchmark_status IN ('current', 'required')",
            name="ck_policy_state_benchmark_status",
        ),
        sa.PrimaryKeyConstraint("singleton_id"),
    )
    op.create_table(
        "policy_write_receipts",
        sa.Column("receipt_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("accepted_revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('applied', 'unchanged')",
            name="ck_policy_write_receipts_outcome",
        ),
        sa.PrimaryKeyConstraint("receipt_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_policy_write_receipts_key"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO policy_state
                (singleton_id, revision, source, as_of, benchmark_status)
            VALUES
                (1, 0, 'migration_0023', CURRENT_TIMESTAMP, 'current')
            """
        )
    )


def downgrade() -> None:
    op.drop_table("policy_write_receipts")
    op.drop_table("policy_state")
