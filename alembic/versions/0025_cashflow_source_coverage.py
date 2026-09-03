"""Add durable account/date source-coverage attestations for cash flows.

Existing databases deliberately receive no inferred coverage rows. An empty
table means historical source coverage has not yet been attested and return
calculations fail closed until approved evidence is recorded.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cashflow_source_attestations",
        sa.Column("attestation_id", sa.Integer(), nullable=False),
        sa.Column("attestation_key", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("coverage_start", sa.Date(), nullable=False),
        sa.Column("coverage_end", sa.Date(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_reference", sa.String(length=512), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("methodology_version", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_attestation_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "coverage_start <= coverage_end",
            name="ck_cashflow_source_attestations_date_order",
        ),
        sa.CheckConstraint(
            "source_type IN "
            "('brokerage_statement', 'provider_export', 'owner_reconciliation')",
            name="ck_cashflow_source_attestations_source_type",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_cashflow_source_attestations_sha256_length",
        ),
        sa.CheckConstraint(
            "approved_at IS NULL OR approved_at >= captured_at",
            name="ck_cashflow_source_attestations_approval_order",
        ),
        sa.CheckConstraint(
            "(superseded_at IS NULL AND superseded_by_attestation_id IS NULL) OR "
            "(superseded_at IS NOT NULL AND superseded_by_attestation_id IS NOT NULL)",
            name="ck_cashflow_source_attestations_supersession_pair",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["superseded_by_attestation_id"],
            ["cashflow_source_attestations.attestation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("attestation_id"),
        sa.UniqueConstraint("attestation_key"),
    )
    op.create_index(
        "ix_cashflow_source_attestations_account_dates",
        "cashflow_source_attestations",
        ["account_id", "coverage_start", "coverage_end"],
        unique=False,
    )
    op.create_table(
        "cashflow_source_gaps",
        sa.Column("gap_id", sa.Integer(), nullable=False),
        sa.Column("attestation_id", sa.Integer(), nullable=False),
        sa.Column("gap_start", sa.Date(), nullable=False),
        sa.Column("gap_end", sa.Date(), nullable=False),
        sa.Column("reason_code", sa.String(length=48), nullable=False),
        sa.CheckConstraint(
            "gap_start <= gap_end",
            name="ck_cashflow_source_gaps_date_order",
        ),
        sa.CheckConstraint(
            "reason_code IN "
            "('provider_history_unavailable', 'statement_missing', "
            "'unresolved_classification', 'unreconciled_difference')",
            name="ck_cashflow_source_gaps_reason_code",
        ),
        sa.ForeignKeyConstraint(
            ["attestation_id"],
            ["cashflow_source_attestations.attestation_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("gap_id"),
    )
    op.create_index(
        "ix_cashflow_source_gaps_attestation",
        "cashflow_source_gaps",
        ["attestation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cashflow_source_gaps_attestation", table_name="cashflow_source_gaps")
    op.drop_table("cashflow_source_gaps")
    op.drop_index(
        "ix_cashflow_source_attestations_account_dates",
        table_name="cashflow_source_attestations",
    )
    op.drop_table("cashflow_source_attestations")
