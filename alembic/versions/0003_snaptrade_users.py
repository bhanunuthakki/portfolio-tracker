"""Add snaptrade_users table.

Persists the SnapTrade `user_secret` returned by `register_snap_trade_user`
so it survives across the open-portal / return / sync flow. Without this
the secret is created at portal time and lost on the next request.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-06

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "snaptrade_users",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("user_secret_encrypted", sa.String(), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("snaptrade_users")
