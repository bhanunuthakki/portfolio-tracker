"""Add stock_splits (corporate split events for walk-back quantity adjustment).

`prices` are split-adjusted (back-adjusted) closes, but transaction and
snapshot quantities are recorded in as-traded units. When the walk-back
reconstructs historical quantities and values them against the adjusted price
series, a split inside the window doubles/halves the reconstructed V. This
table records each security's split events so quantities can be normalized to
today's split-adjusted units. Populated by `jobs.splits`.

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_splits",
        sa.Column(
            "security_id",
            sa.Integer(),
            sa.ForeignKey("securities.security_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("split_date", sa.Date(), primary_key=True),
        sa.Column("ratio", sa.Numeric(20, 10), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("stock_splits")
