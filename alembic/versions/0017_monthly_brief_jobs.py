"""Monthly brief async generation jobs.

The Opus brief call takes 30-60s — too long for a sync HTTP request to
remain healthy across navigations / flaky networks. `monthly_brief_jobs`
backs the async-spawn + poll pattern: POST inserts a row + spawns a
BackgroundTask, GET /jobs/{job_id} returns status. Lifecycle:
pending → running → (succeeded | failed).

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monthly_brief_jobs",
        sa.Column("job_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("period_yyyymm", sa.String(length=7), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "brief_id",
            sa.Integer(),
            sa.ForeignKey("monthly_briefs.brief_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_monthly_brief_jobs_status", "monthly_brief_jobs", ["status"]
    )
    op.create_index(
        "ix_monthly_brief_jobs_period",
        "monthly_brief_jobs",
        ["period_yyyymm", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_monthly_brief_jobs_period", table_name="monthly_brief_jobs")
    op.drop_index("ix_monthly_brief_jobs_status", table_name="monthly_brief_jobs")
    op.drop_table("monthly_brief_jobs")
