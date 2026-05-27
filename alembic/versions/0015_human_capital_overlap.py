"""Add human_capital_overlap table + bootstrap from prior frozensets.

The CIO coaching engine used to gate the `concentration_human_capital`
tip on a binary frozenset membership check (`META`, `GOOGL`, `AMZN`,
... were "in Big-Tech/Ads"; everything else wasn't). That missed the
aggregate case — twenty 2%-weight Big-Tech-adjacent positions silently
passed because no single name was over the 8% single-name cap.

This table lets each ticker carry a vector of 0–100% bucket weights
so the engine can sum portfolio-wide exposure per bucket and fire on
the aggregate. Bootstrap mirrors the prior frozensets at weight 100
so behavior is unchanged on first migration; a few weighted examples
(GOOGL, AMZN, META) showcase the split-bucket case and the new
`ai_infra` bucket.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Bucket names — keep aligned with the cap constants in services/coaching.py
# (_BUCKET_CAPS). Add buckets via this seed + a cap entry in coaching.py;
# new buckets without a cap are simply ignored by the aggregate tip.
_BIG_TECH_ADS = "big_tech_ads"
_STARTUP_VC_FINTECH = "startup_vc_fintech"
_AI_INFRA = "ai_infra"


# Mirror of the prior `_BIG_TECH_ADS_OVERLAP` frozenset. Weight 100 means
# "treat this ticker's full position as exposure to the bucket" — i.e. the
# same behavior the binary check produced.
_BIG_TECH_ADS_SEED: list[tuple[str, str]] = [
    # (ticker, notes). All weight 100 unless overridden below.
    ("META", "employer overlap — full exposure."),
    ("GOOG", "Alphabet — split with ai_infra below; see GOOGL row."),
    ("MSFT", "Microsoft — ads + cloud + AI; treated as 100% Big-Tech bucket here."),
    ("AAPL", "Apple — services/ads exposure."),
    ("NFLX", "Netflix — ads + consumer Internet."),
    ("SNAP", "Snap — ads-funded consumer Internet."),
    ("PINS", "Pinterest — ads-funded consumer Internet."),
    ("TTD", "The Trade Desk — programmatic ads."),
    ("RDDT", "Reddit — ads-funded consumer Internet."),
    ("APP", "AppLovin — mobile ads."),
    ("ROKU", "Roku — connected-TV ads."),
]

# Tickers/themes correlated with a startup/VC/fintech employer (startup/VC ecosystem).
_STARTUP_VC_FINTECH_SEED: list[tuple[str, str]] = [
    ("COIN", "Crypto exchange — VC-cycle sensitive."),
    ("AFRM", "BNPL fintech — ZIRP/VC-cycle sensitive."),
    ("UPST", "Consumer-lending fintech."),
    ("HOOD", "Retail-broker — VC-era fintech."),
    ("SOFI", "Consumer fintech — VC-era."),
    ("MQ", "Marqeta — fintech infra."),
    ("MNDY", "Monday.com — SaaS for VC-funded startups."),
    ("DDOG", "Datadog — SaaS observability for VC-funded co's."),
    ("NET", "Cloudflare — infra for VC-funded co's."),
    ("CRWD", "CrowdStrike — security SaaS for VC-funded co's."),
    ("SNOW", "Snowflake — VC-funded data stack."),
    ("RBLX", "Roblox — VC-era gaming platform."),
]

# Split-bucket and AI-Infra example seeds. These override the default 100%
# weight for tickers that also appear in _BIG_TECH_ADS_SEED above. Order
# matters: the loop in `upgrade()` upserts these after the bulk seed so the
# explicit weights win.
_WEIGHTED_OVERRIDES: list[tuple[str, str, float, str | None]] = [
    # (ticker, bucket, weight_pct, notes)
    # GOOGL — primarily ads, meaningful AI-infra exposure via Cloud + Gemini.
    ("GOOGL", _BIG_TECH_ADS, 80.0, "Search + YouTube ads; majority of revenue."),
    ("GOOGL", _AI_INFRA, 20.0, "Google Cloud + Gemini — AI-infra exposure."),
    # AMZN — predominantly retail, with AWS (cloud/AI) and ads.
    ("AMZN", _BIG_TECH_ADS, 10.0, "Sponsored-products ads layer."),
    ("AMZN", "cloud", 70.0, "AWS — core revenue + margin engine."),
    ("AMZN", "retail", 20.0, "First-party + 3P marketplace."),
    # AI-infra examples — pure-play picks the user is bullish on per
    # CIO_CONTEXT.md. Looser cap (25%) since AI is the macro thesis.
    ("NVDA", _AI_INFRA, 100.0, "Pure-play AI accelerator silicon."),
    ("AVGO", _AI_INFRA, 70.0, "Custom AI silicon + networking; rest is broader semis."),
]


def upgrade() -> None:
    op.create_table(
        "human_capital_overlap",
        sa.Column("ticker", sa.String(length=16), primary_key=True),
        sa.Column("bucket", sa.String(length=64), primary_key=True),
        sa.Column("weight_pct", sa.Numeric(6, 3), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    table = sa.table(
        "human_capital_overlap",
        sa.column("ticker", sa.String),
        sa.column("bucket", sa.String),
        sa.column("weight_pct", sa.Numeric),
        sa.column("notes", sa.String),
    )

    # Seed default 100% rows for tickers that were in the prior frozensets.
    # Tickers later overridden in _WEIGHTED_OVERRIDES get their per-bucket
    # row(s) added below; the bulk seed sits underneath as the fallback
    # full-exposure row.
    overridden = {(t, b) for t, b, _, _ in _WEIGHTED_OVERRIDES}

    rows: list[dict[str, object]] = []
    for ticker, notes in _BIG_TECH_ADS_SEED:
        if (ticker, _BIG_TECH_ADS) in overridden:
            continue
        rows.append(
            {
                "ticker": ticker,
                "bucket": _BIG_TECH_ADS,
                "weight_pct": 100.0,
                "notes": notes,
            }
        )
    for ticker, notes in _STARTUP_VC_FINTECH_SEED:
        if (ticker, _STARTUP_VC_FINTECH) in overridden:
            continue
        rows.append(
            {
                "ticker": ticker,
                "bucket": _STARTUP_VC_FINTECH,
                "weight_pct": 100.0,
                "notes": notes,
            }
        )
    for ticker, bucket, weight_pct, notes in _WEIGHTED_OVERRIDES:
        rows.append(
            {
                "ticker": ticker,
                "bucket": bucket,
                "weight_pct": weight_pct,
                "notes": notes,
            }
        )

    op.bulk_insert(table, rows)


def downgrade() -> None:
    op.drop_table("human_capital_overlap")
