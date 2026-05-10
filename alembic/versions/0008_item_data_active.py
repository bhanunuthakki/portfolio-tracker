"""Add `is_data_active` flag to items.

Lets the user keep an Item linked (so the Plaid connection slot stays
occupied and the access_token continues to be touched) while telling
every aggregation query to ignore the data it produces. Useful when the
same brokerage is reachable through two aggregators — e.g., Robinhood
via both Plaid and SnapTrade — and the user has consciously moved their
source-of-truth to one of them but doesn't want to unlink the other.

Default is TRUE so existing items keep counting.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-09

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column(
            "is_data_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("items", "is_data_active")
