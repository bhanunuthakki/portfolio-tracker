"""Add `origin` marker to holdings_snapshots and investment_transactions.

Distinguishes broker-emitted rows from manual interpolations / synthetic
backfills that this app inserts to smooth over data gaps (Plaid lag windows,
SnapTrade sync errors, cross-aggregator transfer attribution, etc.). Calculations
that need pristine broker truth can filter by origin; calculations that prefer
the smoothed best-estimate view can use everything.

Values:
  * `broker`  — pass-through of broker data (Plaid or SnapTrade emitted)
  * `manual`  — synthesized/backfilled by the app

The same column is added to both tables for symmetry. We don't sub-divide
`broker` into plaid/snaptrade because that's already derivable from the
parent Item.source.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "holdings_snapshots",
        sa.Column(
            "origin",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'broker'"),
        ),
    )
    op.add_column(
        "investment_transactions",
        sa.Column(
            "origin",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'broker'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("investment_transactions", "origin")
    op.drop_column("holdings_snapshots", "origin")
