"""Add `linked_brief_path` to trade_decisions for earnings-summary backlink.

When a user makes a trade decision against an earnings event, they can
now link the decision row to the specific quarterly research brief
artifact (HTML file in earnings-summary's output/research/{ticker}/).
This closes the loop: the brief lists trades made against it, and the
decision log shows the brief at decision time.

Value is a relative path under earnings-summary's output dir (e.g.
`research/NU/2026-05-13_report.html`) so it survives moving the
companion project, and so the portfolio-tracker's
`/api/earnings-summary/brief/{ticker}` route can resolve it.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("trade_decisions") as batch_op:
        batch_op.add_column(
            sa.Column("linked_brief_path", sa.String(length=512), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("trade_decisions") as batch_op:
        batch_op.drop_column("linked_brief_path")
