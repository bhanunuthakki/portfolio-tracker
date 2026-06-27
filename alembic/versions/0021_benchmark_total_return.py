"""Add benchmarks.total_return_close (dividend-reinvested adjusted close).

The return/counterfactual math compares the user's positions to "if this
money had been in SPY/QQQ/policy instead." A real index investment reinvests
its dividends, so the counterfactual must use a total-return series. Storing
only the raw price `close` dropped the benchmark's ~1.3 %/yr dividend yield
and systematically over-credited the user's alpha vs SPY. This column holds
the dividend-reinvested adjusted close; `jobs.benchmarks` populates it, and
consumers `coalesce(total_return_close, close)` so historical rows keep
working until a re-fetch backfills them.

Backfill after upgrading:
    python -m portfolio_tracker.jobs.benchmarks --start <earliest benchmark date>

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "benchmarks",
        sa.Column("total_return_close", sa.Numeric(20, 6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("benchmarks", "total_return_close")
