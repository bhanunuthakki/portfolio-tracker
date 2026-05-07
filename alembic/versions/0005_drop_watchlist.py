"""Drop the watchlist table.

The watchlist feature was removed to keep the app focused on portfolio
tracking. Schema is dropped to clean up the DB; a future re-add can use
a fresh migration.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-06

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("watchlist")


def downgrade() -> None:
    op.create_table(
        "watchlist",
        sa.Column("watchlist_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("ticker", name="uq_watchlist_ticker"),
    )
