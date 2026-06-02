"""Add tax form import schema (1099-B/DIV/INT/R augmentation tables).

These tables store authoritative tax-record data from broker-issued 1099 PDFs.
Kept SEPARATE from `investment_transactions` and `holdings_snapshots` so that
broker-sourced live data and tax-record data don't conflict, and so the UI
can clearly attribute every piece of supplementary detail to its source 1099.

Owner attribution is via `recipient_name` (free-text from the PDF header) on
`tax_form_imports` — supports adding a second household member's future 1099s
under the same schema without any change.

Tables:
  * tax_form_imports — one row per imported 1099 PDF (parent record)
  * tax_form_realized_lots — 1099-B sales detail (each closed tax lot)
  * tax_form_dividends — 1099-DIV payment-level detail (qualified, foreign
    tax paid, section 199A, etc.)
  * tax_form_interest — 1099-INT detail (interest payments, security lending)
  * tax_form_retirement_distributions — 1099-R retirement distribution detail

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tax_form_imports",
        sa.Column("import_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("broker", sa.String(length=32), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("account_mask", sa.String(length=32), nullable=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.account_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("form_types", sa.String(length=128), nullable=True),
        sa.Column("file_path", sa.String(length=512), nullable=True),
        sa.Column("document_id", sa.String(length=64), nullable=True),
        sa.Column("statement_date", sa.Date(), nullable=True),
        sa.Column("recipient_name", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_tax_form_imports_broker_year",
        "tax_form_imports",
        ["broker", "tax_year"],
    )

    op.create_table(
        "tax_form_realized_lots",
        sa.Column("lot_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "import_id",
            sa.Integer(),
            sa.ForeignKey("tax_form_imports.import_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("description", sa.String(length=256), nullable=True),
        sa.Column("symbol", sa.String(length=16), nullable=True),
        sa.Column("cusip", sa.String(length=16), nullable=True),
        sa.Column(
            "security_id",
            sa.Integer(),
            sa.ForeignKey("securities.security_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("acquired_date", sa.Date(), nullable=True),
        sa.Column(
            "acquired_various",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("disposed_date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Numeric(28, 10), nullable=False),
        sa.Column("proceeds", sa.Numeric(20, 6), nullable=False),
        sa.Column("cost_basis", sa.Numeric(20, 6), nullable=False),
        sa.Column("wash_sale_loss_disallowed", sa.Numeric(20, 6), nullable=True),
        sa.Column("net_gain_loss", sa.Numeric(20, 6), nullable=False),
        sa.Column("term", sa.String(length=16), nullable=False),
        sa.Column("form_8949_type", sa.String(length=8), nullable=True),
        sa.Column("proceeds_net_or_gross", sa.String(length=8), nullable=True),
        sa.Column("additional_info", sa.String(length=256), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_tax_form_realized_lots_import_id",
        "tax_form_realized_lots",
        ["import_id"],
    )
    op.create_index(
        "ix_tax_form_realized_lots_security_id",
        "tax_form_realized_lots",
        ["security_id"],
    )

    op.create_table(
        "tax_form_dividends",
        sa.Column("div_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "import_id",
            sa.Integer(),
            sa.ForeignKey("tax_form_imports.import_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("security_description", sa.String(length=256), nullable=True),
        sa.Column("symbol", sa.String(length=16), nullable=True),
        sa.Column("cusip", sa.String(length=16), nullable=True),
        sa.Column(
            "security_id",
            sa.Integer(),
            sa.ForeignKey("securities.security_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column("transaction_type", sa.String(length=64), nullable=False),
        sa.Column("country", sa.String(length=8), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_tax_form_dividends_import_id",
        "tax_form_dividends",
        ["import_id"],
    )

    op.create_table(
        "tax_form_interest",
        sa.Column("int_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "import_id",
            sa.Integer(),
            sa.ForeignKey("tax_form_imports.import_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("description", sa.String(length=256), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(20, 6), nullable=False),
        sa.Column(
            "transaction_type",
            sa.String(length=32),
            server_default="Interest",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_tax_form_interest_import_id",
        "tax_form_interest",
        ["import_id"],
    )

    op.create_table(
        "tax_form_retirement_distributions",
        sa.Column("dist_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "import_id",
            sa.Integer(),
            sa.ForeignKey("tax_form_imports.import_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("gross_distribution", sa.Numeric(20, 6), nullable=True),
        sa.Column("taxable_amount", sa.Numeric(20, 6), nullable=True),
        sa.Column("federal_tax_withheld", sa.Numeric(20, 6), nullable=True),
        sa.Column("state_tax_withheld", sa.Numeric(20, 6), nullable=True),
        sa.Column("distribution_code", sa.String(length=8), nullable=True),
        sa.Column("payer", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_tax_form_retirement_distributions_import_id",
        "tax_form_retirement_distributions",
        ["import_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tax_form_retirement_distributions_import_id",
        table_name="tax_form_retirement_distributions",
    )
    op.drop_table("tax_form_retirement_distributions")
    op.drop_index("ix_tax_form_interest_import_id", table_name="tax_form_interest")
    op.drop_table("tax_form_interest")
    op.drop_index("ix_tax_form_dividends_import_id", table_name="tax_form_dividends")
    op.drop_table("tax_form_dividends")
    op.drop_index(
        "ix_tax_form_realized_lots_security_id", table_name="tax_form_realized_lots"
    )
    op.drop_index(
        "ix_tax_form_realized_lots_import_id", table_name="tax_form_realized_lots"
    )
    op.drop_table("tax_form_realized_lots")
    op.drop_index("ix_tax_form_imports_broker_year", table_name="tax_form_imports")
    op.drop_table("tax_form_imports")
