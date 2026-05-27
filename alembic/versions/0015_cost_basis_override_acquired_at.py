"""Add `acquired_at` column to cost_basis_overrides.

Records the date the user (or the ACATS source broker) actually
acquired the shares the override covers. Used as the anchor for
synthetic SPY buys in the Trade Timeline matched-flow counterfactual
and as the pre-window quantity anchor in Position Alpha. Without it,
ACATS-in shares get a synthetic SPY buy anchored to the receiving
broker's first snapshot date, which is typically months/years after
the user's real entry — overstating alpha vs SPY for tickers that
ran during the gap.

NULL is the existing-row default: leaves prior behavior intact (the
synthetic-buy logic falls back to `earliest_snapshot_date` etc.).
There's no source-of-truth to backfill `inferred_acats` rows from;
the user supplies them manually when they want a more accurate
counterfactual.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cost_basis_overrides") as batch_op:
        batch_op.add_column(sa.Column("acquired_at", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cost_basis_overrides") as batch_op:
        batch_op.drop_column("acquired_at")
