"""Consolidated positions for the additive `/api/v1` namespace.

Moves two derivations the companion `earnings-summary` client used to compute
locally — by joining `GET /api/portfolio/holdings` with `GET /api/plaid/items`
— onto the server, so the tracker owns the contract:

  * each position's ``percent_of_portfolio`` (market value / total book), and
  * a per-account-lot ``tax_treatment`` (``taxable`` / ``tax_deferred`` /
    ``tax_free`` / ``unknown``) inferred from the account ``type`` + ``subtype``.

The 4-way :func:`tax_treatment` mapping mirrors that client's function
*exactly* (see `earnings-summary/src/integrations/portfolio_tracker_client.py`)
so switching the client to ``GET /api/v1/portfolio/positions`` produces the
same buckets it derived by hand. This is intentionally NOT the coarser 3-way
``services.positioning.classify_tax_treatment`` (which collapses HSA/Roth into
one "tax-advantaged" slice and maps a bare ``individual`` subtype to taxable) —
the v1 contract needs the finer split, and a bare ``individual`` is ``unknown``
here, not ``taxable``.

This module is split so the math is testable without a database: the pure
:func:`tax_treatment` mapping and :func:`build_positions_result` assembler know
nothing about SQLAlchemy; the thin DB-facing route (`api/routes/positions_v1.py`)
fetches + consolidates and calls the assembler.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from portfolio_tracker.schemas import ConsolidatedHoldingOut

# Tax-treatment buckets, in display order. Mirrors the earnings-summary
# client's ``TAX_BUCKETS`` so the two projects agree on the bucket set.
TAX_TREATMENTS: tuple[str, ...] = ("taxable", "tax_deferred", "tax_free", "unknown")

# Traditional / tax-deferred subtypes checked by exact match (the ``401k`` /
# ``ira`` tokens are matched as substrings before this set). Kept verbatim from
# the earnings-summary client.
_TAX_DEFERRED_SUBTYPES: frozenset[str] = frozenset(
    {
        "403b",
        "457b",
        "sep",
        "simple",
        "pension",
        "keogh",
        "retirement",
        "rrsp",
        "sarsep",
        "profit sharing plan",
    }
)


def tax_treatment(account_type: str | None, subtype: str | None) -> str:
    """Map an account ``type`` + ``subtype`` to a 4-way tax bucket.

    Roth (incl. Roth 401k/IRA) + HSA grow tax-free; traditional 401k/IRA/SEP/etc.
    are tax-deferred; an ordinary brokerage is taxable. The ``roth`` check runs
    first so "roth ira" / "roth 401k" land in ``tax_free``, not ``tax_deferred``.
    A bare ``individual`` / ``joint`` (no ``brokerage`` token) is ``unknown`` —
    the finer contract doesn't guess taxable from those alone.

    Mirrors the earnings-summary client's ``tax_treatment`` exactly so the
    client can drop its local derivation and consume this verbatim.
    """
    s = (subtype or "").strip().lower()
    t = (account_type or "").strip().lower()
    if "roth" in s or s == "hsa":
        return "tax_free"
    if "401k" in s or "ira" in s or s in _TAX_DEFERRED_SUBTYPES:
        return "tax_deferred"
    if "brokerage" in s or t == "brokerage":
        return "taxable"
    return "unknown"


class PositionLotV1(BaseModel):
    """One account's slice of a consolidated position, tagged with its tax
    treatment. One position can span accounts with different treatments (e.g.
    the same name held in a Roth IRA and a taxable brokerage)."""

    account_id: int
    account_name: str
    quantity: Decimal
    market_value: Decimal | None
    cost_basis: Decimal | None
    cost_basis_source: str | None
    tax_treatment: str


class PositionV1(BaseModel):
    """A position rolled up across every account that holds the security."""

    security_id: int
    ticker: str | None
    name: str | None
    quantity: Decimal
    market_value: Decimal | None
    cost_basis: Decimal | None
    unrealized_pnl: Decimal | None
    # Share of the total book by market value, in PERCENT (0–100). None when the
    # position has no market value, or the book total is 0.
    percent_of_portfolio: Decimal | None
    accounts: list[PositionLotV1]


class PositionsV1Result(BaseModel):
    """``GET /api/v1/portfolio/positions`` — consolidated positions with
    server-computed weights + per-lot tax treatment."""

    snapshot_date: date | None
    total_market_value: Decimal
    positions: list[PositionV1]
    # Market value summed into each tax bucket, bucketed at the LOT level so a
    # position split across a Roth and a taxable account contributes to both.
    by_tax_treatment: dict[str, Decimal]
    notes: list[str]


_PERCENT_QUANT = Decimal("0.0001")


def build_positions_result(
    snapshot_date: date | None,
    consolidated: list[ConsolidatedHoldingOut],
    account_tax: Mapping[int, str],
) -> PositionsV1Result:
    """Assemble the v1 payload from already-consolidated holdings.

    Pure (no DB): ``consolidated`` is the output of
    `api/routes/portfolio.py:_consolidate_holdings`; ``account_tax`` maps each
    contributing ``account_id`` to its 4-way :func:`tax_treatment`. The total
    book is the sum of position market values; ``percent_of_portfolio`` is
    ``market_value / total × 100`` (matching the earnings-summary client's
    100× percent convention and the codebase's ``weight_pct`` cuts).
    """
    total = sum((c.total_value or Decimal(0) for c in consolidated), Decimal(0))
    by_tax: dict[str, Decimal] = {bucket: Decimal(0) for bucket in TAX_TREATMENTS}

    positions: list[PositionV1] = []
    for c in consolidated:
        lots: list[PositionLotV1] = []
        for a in c.accounts:
            treatment = account_tax.get(a.account_id, "unknown")
            by_tax[treatment] = by_tax.get(treatment, Decimal(0)) + (
                a.institution_value or Decimal(0)
            )
            lots.append(
                PositionLotV1(
                    account_id=a.account_id,
                    account_name=a.account_name,
                    quantity=a.quantity,
                    market_value=a.institution_value,
                    cost_basis=a.cost_basis,
                    cost_basis_source=a.cost_basis_source,
                    tax_treatment=treatment,
                )
            )
        percent = (
            (c.total_value / total * Decimal(100)).quantize(_PERCENT_QUANT)
            if c.total_value is not None and total > 0
            else None
        )
        positions.append(
            PositionV1(
                security_id=c.security_id,
                ticker=c.ticker,
                name=c.name,
                quantity=c.total_quantity,
                market_value=c.total_value,
                cost_basis=c.total_cost_basis,
                unrealized_pnl=c.unrealized_pnl,
                percent_of_portfolio=percent,
                accounts=lots,
            )
        )

    notes = [
        "percent_of_portfolio is market_value / total book × 100 (percent), "
        "over the latest snapshot's positions; a position with no market value "
        "is omitted from the total and reports null.",
        "tax_treatment is a 4-way bucket (taxable / tax_deferred / tax_free / "
        "unknown) inferred from each account's type + subtype; a bare "
        "'individual' / 'joint' subtype with no 'brokerage' token is 'unknown'.",
    ]
    if not consolidated:
        notes = ["No active holdings in the latest snapshot."]

    return PositionsV1Result(
        snapshot_date=snapshot_date,
        total_market_value=total,
        positions=positions,
        by_tax_treatment=by_tax,
        notes=notes,
    )
