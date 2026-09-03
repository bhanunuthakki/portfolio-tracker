from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio_tracker.jobs.daily_values import (
    apply_modeled_cache_rebuild,
    preview_modeled_cache_rebuild,
)
from portfolio_tracker.models import PortfolioValueDaily


def test_modeled_cache_rebuild_is_preview_only_until_applied(session):
    modeled_date = date(2025, 1, 1)
    observed_date = date(2025, 1, 2)
    session.add_all(
        [
            PortfolioValueDaily(
                date=modeled_date,
                total_value=Decimal(100),
                total_cost_basis=None,
                source="backfill",
            ),
            PortfolioValueDaily(
                date=observed_date,
                total_value=Decimal(200),
                total_cost_basis=None,
                source="snapshot",
            ),
        ]
    )
    session.commit()

    preview = preview_modeled_cache_rebuild(session, modeled_date, observed_date)

    assert preview.modeled_rows_to_replace == 1
    assert preview.observed_rows_preserved == 1
    assert session.get(PortfolioValueDaily, modeled_date) is not None

    deleted = apply_modeled_cache_rebuild(session, preview)

    assert deleted == 1
    assert session.get(PortfolioValueDaily, modeled_date) is None
    assert session.get(PortfolioValueDaily, observed_date) is not None
