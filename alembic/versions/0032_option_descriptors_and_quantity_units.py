"""Preserve provider option descriptors and snapshot quantity units.

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("securities", sa.Column("option_contract_json", sa.Text(), nullable=True))
    op.add_column(
        "holdings_snapshots",
        sa.Column("quantity_unit", sa.String(32), nullable=False, server_default="unknown"),
    )


def downgrade() -> None:
    op.drop_column("holdings_snapshots", "quantity_unit")
    op.drop_column("securities", "option_contract_json")
