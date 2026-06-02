"""Cockpit action queue.

Persists the Opus-ranked, grounded action queue so the accept / dismiss /
snooze lifecycle survives regeneration. `signature` is the stable identity
(ticker + the concern's signal kinds, hashed), unique so a refresh updates the
same row rather than duplicating it.

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "action_queue",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("tier", sa.String(length=8), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("suggested_action", sa.Text(), nullable=False),
        sa.Column("weight_pct", sa.Numeric(9, 4), nullable=False, server_default="0"),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("signal_kinds_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("llm_ranked", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("snooze_until", sa.Date(), nullable=True),
        sa.Column("commitment_json", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("signature", name="uq_action_queue_signature"),
    )
    op.create_index("ix_action_queue_status", "action_queue", ["status"])


def downgrade() -> None:
    op.drop_index("ix_action_queue_status", table_name="action_queue")
    op.drop_table("action_queue")
