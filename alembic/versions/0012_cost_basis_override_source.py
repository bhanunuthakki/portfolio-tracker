"""Add `source` column to cost_basis_overrides.

Distinguishes "user manually entered this value" from
"system inferred this value from ACATS pair-matching" (or future
inference sources). Per the user's standing instruction: synthetic /
system-set rows must carry an origin marker on the row itself, so any
downstream consumer can filter by it.

Values:
  * `manual`         — user-entered via API/UI (default)
  * `inferred_acats` — derived by infer_acats_cost_basis.py from the
                       source account's buy history at transfer date
  * `inferred_1099`  — reserved for future inference from 1099-B data

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cost_basis_overrides") as batch_op:
        batch_op.add_column(
            sa.Column(
                "source",
                sa.String(length=32),
                nullable=False,
                server_default="manual",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("cost_basis_overrides") as batch_op:
        batch_op.drop_column("source")
