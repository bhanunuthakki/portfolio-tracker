"""Add tables backing the decision-support surfaces.

Three new tables:

  * `trade_decisions` — pre-trade thesis log. The user fills in a thesis,
    invalidation triggers, sizing, and confidence BEFORE making a trade,
    so the friction of writing it filters low-conviction ideas. Outcomes
    (validated / invalidated) get filled in later when the user reviews.
  * `trade_tags` — post-hoc labels on closed positions for pattern
    learning. Tags like `panic_sold`, `held_too_long`, `thesis_validated`
    let the user mine their own behavioral patterns over time.
  * `earnings_calendar` — upcoming earnings dates per ticker, refreshed
    daily by a yfinance job. The Dashboard surfaces dates for held
    positions so the user sees catalyst exposure in advance.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-09

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_decisions",
        sa.Column("decision_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("decision_date", sa.Date(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column("expected_outcome", sa.Text(), nullable=True),
        sa.Column("invalidation_triggers", sa.Text(), nullable=True),
        sa.Column("position_size_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("time_horizon_months", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.String(length=16), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("outcome_date", sa.Date(), nullable=True),
        sa.Column("outcome_notes", sa.Text(), nullable=True),
        sa.Column("outcome_status", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_trade_decisions_ticker_date",
        "trade_decisions",
        ["ticker", "decision_date"],
    )

    op.create_table(
        "trade_tags",
        sa.Column("tag_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        # `period_start` / `period_end` are intentionally loose — the user
        # tags a holding window without our needing to match an exact lot.
        # NULL `period_end` means the tag applies to the open/current
        # window for that ticker.
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("tag", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_trade_tags_ticker", "trade_tags", ["ticker"])

    op.create_table(
        "earnings_calendar",
        sa.Column("ticker", sa.String(length=16), primary_key=True),
        sa.Column("earnings_date", sa.Date(), primary_key=True),
        sa.Column("earnings_time", sa.String(length=16), nullable=True),
        sa.Column("eps_estimate", sa.Numeric(12, 4), nullable=True),
        sa.Column("eps_actual", sa.Numeric(12, 4), nullable=True),
        sa.Column("revenue_estimate", sa.Numeric(20, 2), nullable=True),
        sa.Column("revenue_actual", sa.Numeric(20, 2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("earnings_calendar")
    op.drop_index("ix_trade_tags_ticker", table_name="trade_tags")
    op.drop_table("trade_tags")
    op.drop_index("ix_trade_decisions_ticker_date", table_name="trade_decisions")
    op.drop_table("trade_decisions")
