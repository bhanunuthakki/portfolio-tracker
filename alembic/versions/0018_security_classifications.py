"""Security sector/region classifications for the positioning view.

Caches a GICS-style sector + coarse region (US / International) per
security. yfinance-sourced rows are `source='auto'`; user edits are
`source='manual'` and the enrichment job never clobbers them. Backs the
Holdings positioning section.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "security_classifications",
        sa.Column(
            "security_id",
            sa.Integer(),
            sa.ForeignKey("securities.security_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sector", sa.String(length=64), nullable=True),
        sa.Column("region", sa.String(length=32), nullable=True),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="auto",
        ),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("security_classifications")
