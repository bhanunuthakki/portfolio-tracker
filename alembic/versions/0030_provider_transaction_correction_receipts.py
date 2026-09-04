"""Add append-only provider transaction correction receipts.

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_transaction_correction_receipts",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("provider_record_id", sa.String(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("source_provider", sa.String(length=16), nullable=False),
        sa.Column("source_locator_kind", sa.String(length=32), nullable=False),
        sa.Column("changed_fields_json", sa.Text(), nullable=False),
        sa.Column("before_payload_json", sa.Text(), nullable=False),
        sa.Column("after_payload_json", sa.Text(), nullable=False),
        sa.Column("before_payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("after_payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("delivery_record_set_sha256", sa.String(length=64), nullable=False),
        sa.Column("decision_authority", sa.String(length=32), nullable=False),
        sa.Column("backup_sha256", sa.String(length=64), nullable=False),
        sa.Column("preview_sha256", sa.String(length=64), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "source_provider IN ('plaid', 'snaptrade')",
            name="ck_provider_tx_correction_receipts_provider",
        ),
        sa.CheckConstraint(
            "source_locator_kind = 'provider_record'",
            name="ck_provider_tx_correction_receipts_locator_kind",
        ),
        sa.CheckConstraint(
            "decision_authority = 'owner_approved'",
            name="ck_provider_tx_correction_receipts_authority",
        ),
        sa.CheckConstraint(
            "length(before_payload_sha256) = 64 AND length(after_payload_sha256) = 64 "
            "AND length(delivery_record_set_sha256) = 64 AND length(backup_sha256) = 64 "
            "AND length(preview_sha256) = 64",
            name="ck_provider_tx_correction_receipts_sha256_lengths",
        ),
        sa.CheckConstraint(
            "length(changed_fields_json) > 2 AND length(before_payload_json) > 2 "
            "AND length(after_payload_json) > 2",
            name="ck_provider_tx_correction_receipts_payloads_present",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["cashflow_reconciliation_runs.run_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["provider_record_id"],
            ["investment_transactions.plaid_investment_transaction_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.account_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("run_id", "provider_record_id"),
    )
    op.create_index(
        "ix_provider_tx_correction_receipts_record",
        "provider_transaction_correction_receipts",
        ["provider_record_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    populated = connection.execute(
        sa.text("SELECT count(*) FROM provider_transaction_correction_receipts")
    ).scalar_one()
    if populated:
        raise RuntimeError(
            "cannot downgrade 0030 while provider transaction correction receipts exist"
        )
    op.drop_index(
        "ix_provider_tx_correction_receipts_record",
        table_name="provider_transaction_correction_receipts",
    )
    op.drop_table("provider_transaction_correction_receipts")
