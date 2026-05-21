"""CIO advisor: chat sessions + turns + monthly briefs.

Three tables backing the LLM-powered CIO advisor surface on the
dashboard: `chat_sessions` (one per conversation thread, with the
title + freshness marker for context refresh), `chat_turns` (the
per-message log of user/assistant exchanges), and `monthly_briefs`
(one HTML brief per calendar month, regeneration overwrites by
period).

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("session_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
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
        sa.Column("last_context_refresh_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "chat_turns",
        sa.Column("turn_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("chat_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("model_used", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_chat_turns_session", "chat_turns", ["session_id", "turn_id"]
    )

    op.create_table(
        "monthly_briefs",
        sa.Column("brief_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("period_yyyymm", sa.String(length=7), nullable=False),
        sa.Column("html", sa.Text(), nullable=False),
        sa.Column("model_used", sa.String(length=64), nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("period_yyyymm", name="uq_monthly_briefs_period"),
    )


def downgrade() -> None:
    op.drop_table("monthly_briefs")
    op.drop_index("ix_chat_turns_session", table_name="chat_turns")
    op.drop_table("chat_turns")
    op.drop_table("chat_sessions")
