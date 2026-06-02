"""Single source of truth for the user's policy-benchmark weights.

The policy benchmark is a user-defined target allocation (ticker → weight)
stored in `policy_weights`. Both the legacy performance pipeline and the
position-alpha pipeline build a synthetic "what your intended allocation
would have done" series from it, so the loader lives here to avoid drift.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import PolicyWeight


def load_policy_weights(session: Session) -> dict[str, Decimal]:
    """Return ticker → fraction (0..1) for the user's policy mix.

    Empty result means no policy is set; the caller should skip the
    synthetic policy series. Total fraction may not exactly sum to 1.0
    if the user's weights aren't fully balanced — we keep the raw
    fractions and let the synthetic portfolio scale accordingly.
    """
    rows = session.execute(select(PolicyWeight)).scalars().all()
    return {r.ticker: Decimal(r.weight_bps) / Decimal(10000) for r in rows if r.weight_bps > 0}
