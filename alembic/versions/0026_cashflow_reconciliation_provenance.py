"""Add event-level cash-flow reconciliation provenance and apply receipts.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # These columns are nullable only for rows created under migration 0025.
    # The bundle check prevents partially populated new provenance; later
    # certification logic treats the all-null legacy shape as non-certifying.
    with op.batch_alter_table("cashflow_source_attestations") as batch_op:
        batch_op.add_column(sa.Column("account_identity_sha256", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("account_mapping_basis", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("account_mapping_confidence", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("source_format", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("parser_version", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("source_timezone", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("source_row_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("cashflow_candidate_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("source_event_set_sha256", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("manifest_sha256", sa.String(64), nullable=True))
        batch_op.create_check_constraint(
            "ck_cashflow_source_attestations_account_sha256_length",
            "account_identity_sha256 IS NULL OR length(account_identity_sha256) = 64",
        )
        batch_op.create_check_constraint(
            "ck_cashflow_source_attestations_mapping_basis",
            "account_mapping_basis IS NULL OR account_mapping_basis IN "
            "('provider_account_id', 'statement_account_identifier', 'owner_confirmed')",
        )
        batch_op.create_check_constraint(
            "ck_cashflow_source_attestations_mapping_confidence",
            "account_mapping_confidence IS NULL OR account_mapping_confidence IN "
            "('exact', 'high', 'provisional')",
        )
        batch_op.create_check_constraint(
            "ck_cashflow_source_attestations_source_row_count",
            "source_row_count IS NULL OR source_row_count >= 0",
        )
        batch_op.create_check_constraint(
            "ck_cashflow_source_attestations_candidate_count",
            "cashflow_candidate_count IS NULL OR cashflow_candidate_count >= 0",
        )
        batch_op.create_check_constraint(
            "ck_cashflow_source_attestations_candidate_within_rows",
            "source_row_count IS NULL OR cashflow_candidate_count IS NULL OR "
            "cashflow_candidate_count <= source_row_count",
        )
        batch_op.create_check_constraint(
            "ck_cashflow_source_attestations_event_set_sha256_length",
            "source_event_set_sha256 IS NULL OR length(source_event_set_sha256) = 64",
        )
        batch_op.create_check_constraint(
            "ck_cashflow_source_attestations_manifest_sha256_length",
            "manifest_sha256 IS NULL OR length(manifest_sha256) = 64",
        )
        batch_op.create_check_constraint(
            "ck_cashflow_source_attestations_provenance_bundle",
            "(account_identity_sha256 IS NULL AND account_mapping_basis IS NULL AND "
            "account_mapping_confidence IS NULL AND source_format IS NULL AND "
            "parser_version IS NULL AND source_timezone IS NULL AND source_row_count IS NULL AND "
            "cashflow_candidate_count IS NULL AND source_event_set_sha256 IS NULL AND "
            "manifest_sha256 IS NULL) OR "
            "(account_identity_sha256 IS NOT NULL AND account_mapping_basis IS NOT NULL AND "
            "account_mapping_confidence IS NOT NULL AND source_format IS NOT NULL AND "
            "parser_version IS NOT NULL AND source_timezone IS NOT NULL AND "
            "source_row_count IS NOT NULL AND cashflow_candidate_count IS NOT NULL AND "
            "source_event_set_sha256 IS NOT NULL AND manifest_sha256 IS NOT NULL)",
        )

    op.create_table(
        "cashflow_source_events",
        sa.Column("source_event_id", sa.String(64), nullable=False),
        sa.Column("attestation_id", sa.Integer(), nullable=False),
        sa.Column("source_record_id", sa.String(512), nullable=True),
        sa.Column("source_locator_kind", sa.String(32), nullable=False),
        sa.Column("source_locator", sa.String(512), nullable=False),
        sa.Column("source_row_ordinal", sa.Integer(), nullable=True),
        sa.Column("source_page", sa.Integer(), nullable=True),
        sa.Column("source_line", sa.Integer(), nullable=True),
        sa.Column("source_row_sha256", sa.String(64), nullable=False),
        sa.Column("activity_date", sa.Date(), nullable=True),
        sa.Column("process_date", sa.Date(), nullable=True),
        sa.Column("settlement_date", sa.Date(), nullable=True),
        sa.Column("source_amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("source_amount_sign_basis", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source_code", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(source_event_id) = 64",
            name="ck_cashflow_source_events_id_length",
        ),
        sa.CheckConstraint(
            "length(source_row_sha256) = 64",
            name="ck_cashflow_source_events_row_sha256_length",
        ),
        sa.CheckConstraint(
            "source_locator_kind IN ('row', 'page_line', 'provider_record')",
            name="ck_cashflow_source_events_locator_kind",
        ),
        sa.CheckConstraint(
            "source_amount_sign_basis IN "
            "('statement_printed', 'provider_reported', 'normalized_external')",
            name="ck_cashflow_source_events_amount_sign_basis",
        ),
        sa.CheckConstraint(
            "activity_date IS NOT NULL OR process_date IS NOT NULL OR settlement_date IS NOT NULL",
            name="ck_cashflow_source_events_has_date",
        ),
        sa.CheckConstraint(
            "(source_locator_kind = 'row' AND source_row_ordinal IS NOT NULL AND "
            "source_page IS NULL AND source_line IS NULL) OR "
            "(source_locator_kind = 'page_line' AND source_row_ordinal IS NULL AND "
            "source_page IS NOT NULL AND source_line IS NOT NULL) OR "
            "(source_locator_kind = 'provider_record' AND source_row_ordinal IS NULL AND "
            "source_page IS NULL AND source_line IS NULL AND source_record_id IS NOT NULL)",
            name="ck_cashflow_source_events_locator_shape",
        ),
        sa.CheckConstraint(
            "source_row_ordinal IS NULL OR source_row_ordinal > 0",
            name="ck_cashflow_source_events_row_ordinal_positive",
        ),
        sa.CheckConstraint(
            "source_page IS NULL OR source_page > 0",
            name="ck_cashflow_source_events_page_positive",
        ),
        sa.CheckConstraint(
            "source_line IS NULL OR source_line > 0",
            name="ck_cashflow_source_events_line_positive",
        ),
        sa.ForeignKeyConstraint(
            ["attestation_id"],
            ["cashflow_source_attestations.attestation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("source_event_id"),
        sa.UniqueConstraint(
            "attestation_id",
            "source_locator_kind",
            "source_locator",
            name="uq_cashflow_source_events_attestation_locator",
        ),
    )
    op.create_index(
        "ix_cashflow_source_events_attestation",
        "cashflow_source_events",
        ["attestation_id"],
        unique=False,
    )
    op.create_index(
        "ix_cashflow_source_events_activity_date",
        "cashflow_source_events",
        ["activity_date"],
        unique=False,
    )

    op.create_table(
        "cashflow_reconciliation_decisions",
        sa.Column("decision_key", sa.String(64), nullable=False),
        sa.Column("source_event_id", sa.String(64), nullable=False),
        sa.Column("target_transaction_id", sa.String(), nullable=True),
        sa.Column("resolution_kind", sa.String(48), nullable=False),
        sa.Column("classification", sa.String(32), nullable=True),
        sa.Column("signed_external_amount", sa.Numeric(20, 6), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("effective_date_basis", sa.String(32), nullable=True),
        sa.Column("effective_timezone", sa.String(64), nullable=True),
        sa.Column("decision_authority", sa.String(32), nullable=False),
        sa.Column("confidence", sa.String(16), nullable=False),
        sa.Column("assumption_code", sa.String(64), nullable=True),
        sa.Column("methodology_version", sa.String(16), nullable=False),
        sa.Column("decision_payload_sha256", sa.String(64), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_decision_key", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "length(decision_key) = 64",
            name="ck_cashflow_reconciliation_decisions_key_length",
        ),
        sa.CheckConstraint(
            "length(decision_payload_sha256) = 64",
            name="ck_cashflow_reconciliation_decisions_payload_sha256_length",
        ),
        sa.CheckConstraint(
            "resolution_kind IN ('provider_exact', 'statement_supplement', 'internal', "
            "'excluded', 'unresolved', 'provider_supersedes_supplement')",
            name="ck_cashflow_reconciliation_decisions_resolution_kind",
        ),
        sa.CheckConstraint(
            "classification IS NULL OR classification IN "
            "('external_in', 'external_out', 'internal', 'excluded')",
            name="ck_cashflow_reconciliation_decisions_classification",
        ),
        sa.CheckConstraint(
            "decision_authority IN ('provider', 'brokerage_statement', 'owner_approved')",
            name="ck_cashflow_reconciliation_decisions_authority",
        ),
        sa.CheckConstraint(
            "confidence IN ('exact', 'high', 'provisional')",
            name="ck_cashflow_reconciliation_decisions_confidence",
        ),
        sa.CheckConstraint(
            "effective_date_basis IS NULL OR effective_date_basis IN "
            "('source_activity', 'source_process', 'source_settlement', "
            "'provider_posting', 'owner_resolved')",
            name="ck_cashflow_reconciliation_decisions_date_basis",
        ),
        sa.CheckConstraint(
            "(resolution_kind = 'unresolved' AND classification IS NULL AND "
            "signed_external_amount IS NULL AND effective_date IS NULL AND "
            "effective_date_basis IS NULL AND effective_timezone IS NULL AND "
            "target_transaction_id IS NULL) OR "
            "(resolution_kind != 'unresolved' AND classification IS NOT NULL AND "
            "signed_external_amount IS NOT NULL AND effective_date IS NOT NULL AND "
            "effective_date_basis IS NOT NULL AND effective_timezone IS NOT NULL)",
            name="ck_cashflow_reconciliation_decisions_resolution_fields",
        ),
        sa.CheckConstraint(
            "(classification = 'external_in' AND signed_external_amount > 0) OR "
            "(classification = 'external_out' AND signed_external_amount < 0) OR "
            "(classification IN ('internal', 'excluded') AND signed_external_amount = 0) OR "
            "(classification IS NULL AND signed_external_amount IS NULL)",
            name="ck_cashflow_reconciliation_decisions_amount_direction",
        ),
        sa.CheckConstraint(
            "(resolution_kind IN ('provider_exact', 'statement_supplement', "
            "'provider_supersedes_supplement') AND "
            "classification IN ('external_in', 'external_out') AND "
            "target_transaction_id IS NOT NULL) OR "
            "(resolution_kind = 'internal' AND classification = 'internal' AND "
            "signed_external_amount = 0) OR "
            "(resolution_kind = 'excluded' AND classification = 'excluded' AND "
            "signed_external_amount = 0) OR "
            "(resolution_kind = 'unresolved' AND classification IS NULL AND "
            "signed_external_amount IS NULL AND target_transaction_id IS NULL)",
            name="ck_cashflow_reconciliation_decisions_resolution_semantics",
        ),
        sa.CheckConstraint(
            "(superseded_at IS NULL AND superseded_by_decision_key IS NULL) OR "
            "(superseded_at IS NOT NULL AND superseded_by_decision_key IS NOT NULL)",
            name="ck_cashflow_reconciliation_decisions_supersession_pair",
        ),
        sa.CheckConstraint(
            "superseded_by_decision_key IS NULL OR superseded_by_decision_key != decision_key",
            name="ck_cashflow_reconciliation_decisions_no_self_supersession",
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["cashflow_source_events.source_event_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_transaction_id"],
            ["investment_transactions.plaid_investment_transaction_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id", "superseded_by_decision_key"],
            [
                "cashflow_reconciliation_decisions.source_event_id",
                "cashflow_reconciliation_decisions.decision_key",
            ],
            name="fk_cashflow_reconciliation_decisions_same_event_successor",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("decision_key"),
        sa.UniqueConstraint(
            "source_event_id",
            "decision_key",
            name="uq_cashflow_reconciliation_decisions_event_key",
        ),
    )
    op.create_index(
        "uq_cashflow_reconciliation_decisions_current_event",
        "cashflow_reconciliation_decisions",
        ["source_event_id"],
        unique=True,
        sqlite_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index(
        "ix_cashflow_reconciliation_decisions_target",
        "cashflow_reconciliation_decisions",
        ["target_transaction_id"],
        unique=False,
    )

    op.create_table(
        "cashflow_reconciliation_runs",
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("manifest_set_sha256", sa.String(64), nullable=False),
        sa.Column("software_revision", sa.String(64), nullable=False),
        sa.Column("backup_reference", sa.String(512), nullable=False),
        sa.Column("preview_reference", sa.String(512), nullable=False),
        sa.Column("affected_start", sa.Date(), nullable=False),
        sa.Column("affected_end", sa.Date(), nullable=False),
        sa.Column("affected_account_count", sa.Integer(), nullable=False),
        sa.Column("source_event_count", sa.Integer(), nullable=False),
        sa.Column("planned_mutation_count", sa.Integer(), nullable=False),
        sa.Column("applied_mutation_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(run_id) = 64",
            name="ck_cashflow_reconciliation_runs_id_length",
        ),
        sa.CheckConstraint(
            "length(plan_digest) = 64",
            name="ck_cashflow_reconciliation_runs_plan_digest_length",
        ),
        sa.CheckConstraint(
            "length(manifest_set_sha256) = 64",
            name="ck_cashflow_reconciliation_runs_manifest_sha256_length",
        ),
        sa.CheckConstraint(
            "affected_start <= affected_end",
            name="ck_cashflow_reconciliation_runs_date_order",
        ),
        sa.CheckConstraint(
            "affected_account_count >= 0 AND source_event_count >= 0 AND "
            "planned_mutation_count >= 0 AND applied_mutation_count >= 0 AND "
            "applied_mutation_count <= planned_mutation_count",
            name="ck_cashflow_reconciliation_runs_counts",
        ),
        sa.CheckConstraint(
            "status IN ('previewed', 'applied')",
            name="ck_cashflow_reconciliation_runs_status",
        ),
        sa.CheckConstraint(
            "(status = 'previewed' AND applied_at IS NULL AND applied_mutation_count = 0) OR "
            "(status = 'applied' AND approved_at IS NOT NULL AND applied_at IS NOT NULL AND "
            "applied_at >= approved_at AND applied_mutation_count = planned_mutation_count)",
            name="ck_cashflow_reconciliation_runs_status_fields",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("plan_digest"),
    )
    op.create_index(
        "ix_cashflow_reconciliation_runs_status_created",
        "cashflow_reconciliation_runs",
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        "cashflow_reconciliation_run_decisions",
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("decision_key", sa.String(64), nullable=False),
        sa.Column("membership_kind", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "membership_kind IN ('created', 'superseded', 'verified')",
            name="ck_cashflow_reconciliation_run_decisions_membership_kind",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["cashflow_reconciliation_runs.run_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decision_key"],
            ["cashflow_reconciliation_decisions.decision_key"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "decision_key"),
    )
    op.create_index(
        "ix_cashflow_reconciliation_run_decisions_decision",
        "cashflow_reconciliation_run_decisions",
        ["decision_key"],
        unique=False,
    )

    op.create_table(
        "cashflow_reconciliation_run_transaction_mutations",
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("target_transaction_id", sa.String(), nullable=False),
        sa.Column("mutation_kind", sa.String(32), nullable=False),
        sa.Column("before_payload_sha256", sa.String(64), nullable=True),
        sa.Column("after_payload_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mutation_kind IN ('transaction_insert', 'override_insert', 'override_update')",
            name="ck_cashflow_reconciliation_run_tx_mutations_kind",
        ),
        sa.CheckConstraint(
            "before_payload_sha256 IS NULL OR length(before_payload_sha256) = 64",
            name="ck_cashflow_reconciliation_run_tx_mutations_before_sha256_length",
        ),
        sa.CheckConstraint(
            "length(after_payload_sha256) = 64",
            name="ck_cashflow_reconciliation_run_tx_mutations_after_sha256_length",
        ),
        sa.CheckConstraint(
            "(mutation_kind IN ('transaction_insert', 'override_insert') AND "
            "before_payload_sha256 IS NULL) OR "
            "(mutation_kind = 'override_update' AND before_payload_sha256 IS NOT NULL)",
            name="ck_cashflow_reconciliation_run_tx_mutations_payload_shape",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["cashflow_reconciliation_runs.run_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_transaction_id"],
            ["investment_transactions.plaid_investment_transaction_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "target_transaction_id", "mutation_kind"),
    )
    op.create_index(
        "ix_cashflow_reconciliation_run_tx_mutations_target",
        "cashflow_reconciliation_run_transaction_mutations",
        ["target_transaction_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cashflow_reconciliation_run_tx_mutations_target",
        table_name="cashflow_reconciliation_run_transaction_mutations",
    )
    op.drop_table("cashflow_reconciliation_run_transaction_mutations")
    op.drop_index(
        "ix_cashflow_reconciliation_run_decisions_decision",
        table_name="cashflow_reconciliation_run_decisions",
    )
    op.drop_table("cashflow_reconciliation_run_decisions")
    op.drop_index(
        "ix_cashflow_reconciliation_runs_status_created",
        table_name="cashflow_reconciliation_runs",
    )
    op.drop_table("cashflow_reconciliation_runs")
    op.drop_index(
        "ix_cashflow_reconciliation_decisions_target",
        table_name="cashflow_reconciliation_decisions",
    )
    op.drop_index(
        "uq_cashflow_reconciliation_decisions_current_event",
        table_name="cashflow_reconciliation_decisions",
    )
    op.drop_table("cashflow_reconciliation_decisions")
    op.drop_index(
        "ix_cashflow_source_events_activity_date",
        table_name="cashflow_source_events",
    )
    op.drop_index(
        "ix_cashflow_source_events_attestation",
        table_name="cashflow_source_events",
    )
    op.drop_table("cashflow_source_events")

    with op.batch_alter_table("cashflow_source_attestations") as batch_op:
        batch_op.drop_constraint("ck_cashflow_source_attestations_provenance_bundle", type_="check")
        batch_op.drop_constraint(
            "ck_cashflow_source_attestations_manifest_sha256_length", type_="check"
        )
        batch_op.drop_constraint(
            "ck_cashflow_source_attestations_event_set_sha256_length", type_="check"
        )
        batch_op.drop_constraint(
            "ck_cashflow_source_attestations_candidate_within_rows", type_="check"
        )
        batch_op.drop_constraint("ck_cashflow_source_attestations_candidate_count", type_="check")
        batch_op.drop_constraint("ck_cashflow_source_attestations_source_row_count", type_="check")
        batch_op.drop_constraint(
            "ck_cashflow_source_attestations_mapping_confidence", type_="check"
        )
        batch_op.drop_constraint("ck_cashflow_source_attestations_mapping_basis", type_="check")
        batch_op.drop_constraint(
            "ck_cashflow_source_attestations_account_sha256_length", type_="check"
        )
        batch_op.drop_column("manifest_sha256")
        batch_op.drop_column("source_event_set_sha256")
        batch_op.drop_column("cashflow_candidate_count")
        batch_op.drop_column("source_row_count")
        batch_op.drop_column("source_timezone")
        batch_op.drop_column("parser_version")
        batch_op.drop_column("source_format")
        batch_op.drop_column("account_mapping_confidence")
        batch_op.drop_column("account_mapping_basis")
        batch_op.drop_column("account_identity_sha256")
