"""Add provenance-backed whole-account valuation observations.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_valuation_observations",
        sa.Column("valuation_observation_id", sa.Integer(), nullable=False),
        sa.Column("observation_key", sa.String(64), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("as_of_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("cash_value", sa.Numeric(20, 6), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_provider", sa.String(64), nullable=False),
        sa.Column("source_reference", sa.String(512), nullable=False),
        sa.Column("source_record_id", sa.String(512), nullable=True),
        sa.Column("source_payload_sha256", sa.String(64), nullable=True),
        sa.Column("normalization_version", sa.String(16), server_default="1", nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_complete", sa.Boolean(), nullable=False),
        sa.Column("is_empty", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(observation_key) = 64",
            name="ck_account_valuation_observations_key_length",
        ),
        sa.CheckConstraint(
            "source_kind IN ('provider_api', 'brokerage_statement', 'provider_export')",
            name="ck_account_valuation_observations_source_kind",
        ),
        sa.CheckConstraint(
            "length(currency) = 3",
            name="ck_account_valuation_observations_currency_length",
        ),
        sa.CheckConstraint(
            "length(source_provider) > 0 AND length(source_reference) > 0",
            name="ck_account_valuation_observations_source_locator_present",
        ),
        sa.CheckConstraint(
            "source_record_id IS NOT NULL OR source_payload_sha256 IS NOT NULL",
            name="ck_account_valuation_observations_source_identity",
        ),
        sa.CheckConstraint(
            "source_payload_sha256 IS NULL OR length(source_payload_sha256) = 64",
            name="ck_account_valuation_observations_payload_sha256_length",
        ),
        sa.CheckConstraint(
            "is_empty = 0 OR (is_complete = 1 AND total_value = 0 AND "
            "(cash_value IS NULL OR cash_value = 0))",
            name="ck_account_valuation_observations_empty_zero",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.account_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("valuation_observation_id"),
        sa.UniqueConstraint("observation_key"),
    )
    op.create_index(
        "ix_account_valuation_observations_account_date",
        "account_valuation_observations",
        ["account_id", "as_of_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_account_valuation_observations_account_date",
        table_name="account_valuation_observations",
    )
    op.drop_table("account_valuation_observations")
