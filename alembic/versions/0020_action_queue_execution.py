"""Action-queue execution reconciliation.

Adds the execution-tracking columns that let the daily refresh reconcile an
accepted commitment against the real position change — i.e. did the trade
actually happen? `execution_status` flows `pending` -> `executed` | `partial` |
`not_executed`, or `n/a` for non-position actions (hold/review/audit/research).

`execution_overridden` marks a MANUAL correction at the schema level so the
fuzzy auto-matcher (which re-runs on every daily refresh) won't clobber the
owner's call the next morning. The heuristic only touches rows that haven't
been overridden — mirroring how `refresh_queue` already preserves the
accept/dismiss/snooze lifecycle across regenerations.

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "action_queue",
        sa.Column(
            "execution_status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "action_queue",
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "action_queue",
        sa.Column("executed_weight_pct", sa.Numeric(9, 4), nullable=True),
    )
    op.add_column(
        "action_queue",
        sa.Column("execution_notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "action_queue",
        sa.Column(
            "execution_overridden",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("action_queue", "execution_overridden")
    op.drop_column("action_queue", "execution_notes")
    op.drop_column("action_queue", "executed_weight_pct")
    op.drop_column("action_queue", "executed_at")
    op.drop_column("action_queue", "execution_status")
