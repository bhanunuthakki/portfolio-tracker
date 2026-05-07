"""Initial schema.

Revision ID: 0001
Revises:
Create Date: 2026-05-05

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "items",
        sa.Column("item_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plaid_item_id", sa.String(), nullable=False),
        sa.Column("plaid_access_token_encrypted", sa.String(), nullable=False),
        sa.Column("plaid_institution_id", sa.String(), nullable=True),
        sa.Column("institution_name", sa.String(), nullable=True),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_transactions_cursor", sa.String(), nullable=True),
        sa.UniqueConstraint("plaid_item_id"),
    )

    op.create_table(
        "accounts",
        sa.Column("account_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.item_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plaid_account_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("official_name", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("subtype", sa.String(), nullable=True),
        sa.Column("mask", sa.String(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.UniqueConstraint("plaid_account_id"),
    )

    op.create_table(
        "securities",
        sa.Column("security_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plaid_security_id", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=True),
        sa.Column("cusip", sa.String(), nullable=True),
        sa.Column("isin", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("is_cash_equivalent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("plaid_security_id"),
    )
    op.create_index("ix_securities_ticker", "securities", ["ticker"])

    op.create_table(
        "holdings_snapshots",
        sa.Column("snapshot_date", sa.Date(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "security_id",
            sa.Integer(),
            sa.ForeignKey("securities.security_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("quantity", sa.Numeric(28, 10), nullable=False),
        sa.Column("institution_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("institution_value", sa.Numeric(20, 6), nullable=True),
        sa.Column("cost_basis", sa.Numeric(20, 6), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
    )
    op.create_index("ix_holdings_snapshots_date", "holdings_snapshots", ["snapshot_date"])
    op.create_index(
        "ix_holdings_snapshots_account",
        "holdings_snapshots",
        ["account_id", "snapshot_date"],
    )

    op.create_table(
        "investment_transactions",
        sa.Column("plaid_investment_transaction_id", sa.String(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.account_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "security_id",
            sa.Integer(),
            sa.ForeignKey("securities.security_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("quantity", sa.Numeric(28, 10), nullable=False, server_default="0"),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("price", sa.Numeric(20, 6), nullable=True),
        sa.Column("fees", sa.Numeric(20, 6), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("subtype", sa.String(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
    )
    op.create_index(
        "ix_inv_tx_account_date",
        "investment_transactions",
        ["account_id", "date"],
    )
    op.create_index("ix_inv_tx_date", "investment_transactions", ["date"])

    op.create_table(
        "prices",
        sa.Column(
            "security_id",
            sa.Integer(),
            sa.ForeignKey("securities.security_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("close", sa.Numeric(20, 6), nullable=False),
    )

    op.create_table(
        "benchmarks",
        sa.Column("symbol", sa.String(length=16), primary_key=True),
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("close", sa.Numeric(20, 6), nullable=False),
    )

    op.create_table(
        "portfolio_values_daily",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("total_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("total_cost_basis", sa.Numeric(20, 6), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
    )

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


def downgrade() -> None:
    op.drop_table("watchlist")
    op.drop_table("portfolio_values_daily")
    op.drop_table("benchmarks")
    op.drop_table("prices")
    op.drop_index("ix_inv_tx_date", table_name="investment_transactions")
    op.drop_index("ix_inv_tx_account_date", table_name="investment_transactions")
    op.drop_table("investment_transactions")
    op.drop_index("ix_holdings_snapshots_account", table_name="holdings_snapshots")
    op.drop_index("ix_holdings_snapshots_date", table_name="holdings_snapshots")
    op.drop_table("holdings_snapshots")
    op.drop_index("ix_securities_ticker", table_name="securities")
    op.drop_table("securities")
    op.drop_table("accounts")
    op.drop_table("items")
