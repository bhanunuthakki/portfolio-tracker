"""Persist provider and adjustment-basis provenance for security prices.

Existing rows are deliberately marked unknown. Provenance is filled only when
a writer re-fetches a row from a source whose semantics it can name.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("prices") as batch_op:
        batch_op.add_column(
            sa.Column(
                "source",
                sa.String(length=32),
                server_default="unknown",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "adjustment_basis",
                sa.String(length=32),
                server_default="unknown",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_prices_source",
            "source IN ('unknown', 'yfinance', 'stooq')",
        )
        batch_op.create_check_constraint(
            "ck_prices_adjustment_basis",
            "adjustment_basis IN "
            "('unknown', 'raw_unadjusted', 'split_adjusted', 'total_return_adjusted')",
        )


def downgrade() -> None:
    with op.batch_alter_table("prices") as batch_op:
        batch_op.drop_constraint("ck_prices_adjustment_basis", type_="check")
        batch_op.drop_constraint("ck_prices_source", type_="check")
        batch_op.drop_column("adjustment_basis")
        batch_op.drop_column("source")
