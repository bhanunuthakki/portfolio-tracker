"""Allow provider API cash-flow source attestations.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cashflow_source_attestations") as batch_op:
        batch_op.add_column(
            sa.Column("broker_archive_coverage", sa.String(length=32), nullable=True)
        )
        batch_op.drop_constraint(
            "ck_cashflow_source_attestations_source_type",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_cashflow_source_attestations_source_type",
            "source_type IN ('brokerage_statement', 'provider_api', 'provider_export', "
            "'owner_reconciliation')",
        )
        batch_op.create_check_constraint(
            "ck_cashflow_source_attestations_broker_archive_coverage",
            "broker_archive_coverage IS NULL OR broker_archive_coverage IN "
            "('unasserted', 'provider_asserted')",
        )
    op.create_table(
        "cashflow_source_attestation_event_links",
        sa.Column("attestation_id", sa.Integer(), nullable=False),
        sa.Column("source_event_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["attestation_id"],
            ["cashflow_source_attestations.attestation_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["cashflow_source_events.source_event_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("attestation_id", "source_event_id"),
    )
    op.create_index(
        "ix_cashflow_source_attestation_event_links_event",
        "cashflow_source_attestation_event_links",
        ["source_event_id"],
        unique=False,
    )


def downgrade() -> None:
    provider_rows = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM cashflow_source_attestations WHERE source_type = 'provider_api'"
        )
    )
    if provider_rows:
        raise RuntimeError(
            "cannot downgrade 0029 while provider API provenance exists; "
            "preserve or explicitly remove that private provenance first"
        )
    op.drop_index(
        "ix_cashflow_source_attestation_event_links_event",
        table_name="cashflow_source_attestation_event_links",
    )
    op.drop_table("cashflow_source_attestation_event_links")
    with op.batch_alter_table("cashflow_source_attestations") as batch_op:
        batch_op.drop_constraint(
            "ck_cashflow_source_attestations_broker_archive_coverage",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_cashflow_source_attestations_source_type",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_cashflow_source_attestations_source_type",
            "source_type IN ('brokerage_statement', 'provider_export', 'owner_reconciliation')",
        )
        batch_op.drop_column("broker_archive_coverage")
