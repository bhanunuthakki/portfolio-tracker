"""Add policy_weights table + seed initial weights.

Stores the user's target asset allocation as a list of (ticker, weight_bps)
rows. The seed reflects a typical "growth-tilted with USD/index de-risking"
mix that matches the user's stated allocation strategy.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Initial weights (in basis points; 10000 = 100%). User can edit these
# from the Dashboard once the UI is up.
_INITIAL_WEIGHTS: list[tuple[str, int, str]] = [
    ("QQQ", 6000, "US large-cap growth (Nasdaq-100)"),
    ("VXUS", 2000, "International equity ex-US"),
    ("SGOV", 2000, "0–3mo Treasury bills (USD risk-free)"),
]


def upgrade() -> None:
    op.create_table(
        "policy_weights",
        sa.Column("ticker", sa.String(length=16), primary_key=True),
        sa.Column("weight_bps", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Seed defaults via bulk_insert so the constraint check runs against
    # actual rows.
    policy_weights = sa.table(
        "policy_weights",
        sa.column("ticker", sa.String),
        sa.column("weight_bps", sa.Integer),
        sa.column("notes", sa.String),
    )
    op.bulk_insert(
        policy_weights,
        [
            {"ticker": ticker, "weight_bps": bps, "notes": note}
            for ticker, bps, note in _INITIAL_WEIGHTS
        ],
    )


def downgrade() -> None:
    op.drop_table("policy_weights")
