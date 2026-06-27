"""Split-factor lookup for walk-back quantity normalization.

`prices` stores split-adjusted (back-adjusted) closes — a continuous series in
TODAY's share units. But transaction and snapshot quantities are recorded in
the share units that were in effect on their own date. When the walk-back
reconstructs a historical quantity and values it against the adjusted price
series, any split between then and now introduces a doubled/halved V.

`factor_after(sid, d)` returns the product of every split ratio for security
`sid` with `split_date > d` — i.e. multiply an as-of-`d` share count by this
to express it in today's split-adjusted units. With no splits after `d` it is
1, so books without splits are entirely unaffected.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import StockSplit


class SplitFactors:
    """Per-security split ratios, queried as a cumulative factor after a date."""

    def __init__(self, by_sid: dict[int, list[tuple[date, Decimal]]]) -> None:
        # Each list is (split_date, ratio); order doesn't matter for the product.
        self._by_sid = by_sid

    def factor_after(self, security_id: int | None, after: date) -> Decimal:
        """Product of ratios with split_date strictly after `after` (1 if none)."""
        if security_id is None:
            return Decimal(1)
        events = self._by_sid.get(security_id)
        if not events:
            return Decimal(1)
        factor = Decimal(1)
        for split_date, ratio in events:
            if split_date > after:
                factor *= ratio
        return factor

    @property
    def is_empty(self) -> bool:
        return not self._by_sid


def load_split_factors(session: Session, security_ids: Iterable[int]) -> SplitFactors:
    """Load split events for the given securities into a `SplitFactors`.

    Returns an empty (identity) lookup when no rows match — the common case for
    a book with no recently-split holdings, so the walk-back is unchanged.
    """
    sids = list(security_ids)
    if not sids:
        return SplitFactors({})
    by_sid: dict[int, list[tuple[date, Decimal]]] = {}
    for sid, split_date, ratio in session.execute(
        select(StockSplit.security_id, StockSplit.split_date, StockSplit.ratio).where(
            StockSplit.security_id.in_(sids)
        )
    ):
        by_sid.setdefault(sid, []).append((split_date, Decimal(ratio)))
    return SplitFactors(by_sid)
