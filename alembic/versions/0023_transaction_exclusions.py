"""transaction_exclusions — durable record of deliberately removed transactions

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transaction_exclusions",
        sa.Column("plaid_investment_transaction_id", sa.String(), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "excluded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("transaction_exclusions")
