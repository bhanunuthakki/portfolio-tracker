"""Manual override tables for cost basis and ticker symbols.

Lets the user fill in fields the aggregator didn't supply:
  * `cost_basis_overrides` — when Plaid/SnapTrade returns no cost_basis
    (e.g., SoFi via Plaid), so unrealized P&L can be computed.
  * `ticker_overrides` — when Plaid returns no ticker_symbol, so the
    yfinance price fetch can populate historical pricing.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-06

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cost_basis_overrides",
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "security_id",
            sa.Integer(),
            sa.ForeignKey("securities.security_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("total_cost_basis", sa.Numeric(20, 6), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "ticker_overrides",
        sa.Column(
            "security_id",
            sa.Integer(),
            sa.ForeignKey("securities.security_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("ticker_overrides")
    op.drop_table("cost_basis_overrides")
