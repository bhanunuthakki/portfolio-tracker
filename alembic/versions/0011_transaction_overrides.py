"""Add `transaction_overrides` for user-controlled cashflow classification.

The TWR / contributions calculation classifies each investment_transactions
row as external-in (contribution), external-out (withdrawal), or internal
(transparent to TWR) based on type+subtype heuristics. Brokers report these
inconsistently — a single ACATS share-transfer can look like a contribution
in one feed and an internal move in another — which is why contribution
totals swing across 6M / 1Y / 2Y windows.

This table lets the user explicitly tag any transaction so the pipeline
uses their classification instead of the heuristic.

Classifications:
  * `external_in`   — treat as contribution / deposit
  * `external_out`  — treat as withdrawal / distribution
  * `internal`      — treat as an internal move (no cashflow impact)

A row in this table OVERRIDES the heuristic. Deleting it reverts to default.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transaction_overrides",
        sa.Column(
            "plaid_investment_transaction_id",
            sa.String(),
            sa.ForeignKey(
                "investment_transactions.plaid_investment_transaction_id",
                ondelete="CASCADE",
            ),
            primary_key=True,
        ),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "classification IN ('external_in', 'external_out', 'internal')",
            name="ck_transaction_overrides_classification",
        ),
    )


def downgrade() -> None:
    op.drop_table("transaction_overrides")
