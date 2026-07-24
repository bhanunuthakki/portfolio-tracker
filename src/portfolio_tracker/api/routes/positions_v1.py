"""Additive `/api/v1` positions endpoint.

A first-class, server-derived view of consolidated positions: each carries a
server-computed ``percent_of_portfolio`` and per-account lots tagged with a
five-way ``tax_treatment``. The companion `earnings-summary` client used to derive
both locally by joining ``/api/portfolio/holdings`` with ``/api/plaid/items``;
this endpoint moves that onto the server so the tracker owns the contract.

Single-user, localhost tool: ETag/conditional-GET and pagination are
intentionally omitted (see `docs/api/positions-v1.md`). The route is thin — it
reuses the consolidation in `routes/portfolio.py` and the pure assembler in
`services/positions_v1.py`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from portfolio_tracker.api.routes.portfolio import (
    _consolidate_holdings,  # pyright: ignore[reportPrivateUsage]
    _latest_holding_rows,  # pyright: ignore[reportPrivateUsage]
    _load_cost_basis_overrides,  # pyright: ignore[reportPrivateUsage]
)
from portfolio_tracker.db import get_session
from portfolio_tracker.services.positions_v1 import (
    PositionsV1Result,
    build_positions_result,
    tax_treatment,
)

router = APIRouter(prefix="/api/v1/portfolio", tags=["positions-v1"])


@router.get("/positions", response_model=PositionsV1Result)
def positions(
    session: Annotated[Session, Depends(get_session)],
) -> PositionsV1Result:
    """Consolidated positions with server-computed weights + per-lot tax bucket.

    Each position is rolled up across every account holding the security (same
    consolidation the Holdings view uses), with ``percent_of_portfolio`` and a
    five-way ``tax_treatment`` per account lot computed server-side. Returns an
    empty-but-valid payload when there are no active holdings.
    """
    rows = _latest_holding_rows(session)
    if not rows:
        return build_positions_result(None, [], {})
    snapshot_date = rows[0][0].snapshot_date
    overrides = _load_cost_basis_overrides(session)
    consolidated = _consolidate_holdings(snapshot_date, rows, overrides)
    # account_id → five-way tax treatment, from the Account type/subtype on each row.
    account_tax = {a.account_id: tax_treatment(a.type, a.subtype, a.name) for _, a, _ in rows}
    return build_positions_result(snapshot_date, consolidated, account_tax)
