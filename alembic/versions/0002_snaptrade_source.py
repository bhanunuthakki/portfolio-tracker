"""Add `source` column + SnapTrade fields to items.

Plaid columns become nullable since SnapTrade items don't have them and
vice versa. Existing rows are migrated to source='plaid' (the prior default).

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-06

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("items") as batch:
        batch.add_column(
            sa.Column("source", sa.String(length=16), nullable=False, server_default="plaid")
        )
        batch.add_column(sa.Column("snaptrade_user_id", sa.String(), nullable=True))
        batch.add_column(
            sa.Column("snaptrade_user_secret_encrypted", sa.String(), nullable=True)
        )
        batch.add_column(sa.Column("snaptrade_authorization_id", sa.String(), nullable=True))
        batch.alter_column("plaid_item_id", existing_type=sa.String(), nullable=True)
        batch.alter_column(
            "plaid_access_token_encrypted", existing_type=sa.String(), nullable=True
        )
        batch.create_unique_constraint(
            "uq_items_snaptrade_authorization_id", ["snaptrade_authorization_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("items") as batch:
        batch.drop_constraint("uq_items_snaptrade_authorization_id", type_="unique")
        batch.alter_column(
            "plaid_access_token_encrypted", existing_type=sa.String(), nullable=False
        )
        batch.alter_column("plaid_item_id", existing_type=sa.String(), nullable=False)
        batch.drop_column("snaptrade_authorization_id")
        batch.drop_column("snaptrade_user_secret_encrypted")
        batch.drop_column("snaptrade_user_id")
        batch.drop_column("source")
