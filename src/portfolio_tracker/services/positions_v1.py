"""Consolidated positions for the additive `/api/v1` namespace.

Moves two derivations the companion `earnings-summary` client used to compute
locally — by joining `GET /api/portfolio/holdings` with `GET /api/plaid/items`
— onto the server, so the tracker owns the contract:

  * each position's ``percent_of_portfolio`` (market value / total book), and
  * a per-account-lot ``tax_treatment`` in the ratified five-way detailed enum
    (``taxable`` / ``pretax`` / ``roth`` / ``hsa`` / ``unknown``) inferred from
    the account ``type`` + ``subtype``.

The five-way enum is the Phase 0 SC-1 ruling
(`docs/design/phase0_decision_addendum.md`): Roth and HSA stay distinct because
wealthplan models their cash-flow and withdrawal behavior separately. This is
intentionally NOT the coarser ``services.positioning.classify_tax_treatment``
(one collapsed tax-advantaged display slice) — the v1 contract needs the finer
split, with evidence + confidence on every inference tier (subtype high; cash
subtype / account-name / type medium; bare individual-joint low).

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

# Detailed tax-treatment values, in display order. The SC-1 five-way enum used
# by every `/api/v1` resource; consumers (wealthplan `TaxBucket`, the
# earnings-summary client) map from these values and never from account names.
TAX_TREATMENTS: tuple[str, ...] = ("taxable", "pretax", "roth", "hsa", "unknown")

# Traditional / pretax subtypes checked by exact match (the ``401k`` / ``ira``
# tokens are matched as substrings before this set).
_PRETAX_SUBTYPES: frozenset[str] = frozenset(
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


# Cash-account subtypes some providers attach to brokerage cash-management
# sleeves (e.g. Robinhood "Cash Management"/"Spending" arrive as CHECKING via
# SnapTrade). Outside any tax shelter -> taxable, at medium confidence.
_CASH_ACCOUNT_SUBTYPES: frozenset[str] = frozenset(
    {"checking", "savings", "cash management", "cash"}
)


class TaxTreatmentDetail(BaseModel):
    """The SC-1 detailed tax treatment plus its evidence trail.

    ``evidence`` names the account field + value that decided the treatment
    (e.g. ``subtype:roth ira``, ``name:brokeragelink``); ``confidence`` is
    ``high`` for an explicit subtype match, ``medium`` for a name/type/cash
    fallback tier, ``low`` for the weakest tier (bare individual/joint) or an
    ``unknown`` treatment.
    """

    treatment: str
    evidence: str | None
    confidence: str


def _name_tier(name: str | None) -> TaxTreatmentDetail | None:
    """Provider-owned account-NAME heuristics (confidence ``medium``).

    Centralizes the fallbacks both consumers used to hand-roll — needed
    because SnapTrade omits ``subtype`` entirely for some institutions, so the
    name is the only evidence (e.g. Fidelity "BrokerageLink Roth", "Health
    Savings Account", "META PLATFORMS, INC. 401(K) PLAN"). Rules:

      * ``roth`` anywhere -> roth (checked first, so "BrokerageLink Roth" wins);
      * ``hsa`` / ``health savings`` -> hsa;
      * ``401k`` / ``401(k)`` / word-ish `` ira `` / ``retirement`` /
        ``brokeragelink`` -> pretax — a bare BrokerageLink is the self-directed
        401(k) window (owner-confirmed, carried over from the wealthplan
        adapter this replaces);
      * ``brokerage`` / ``self-directed`` / ``taxable`` -> taxable.
    """
    n = (name or "").strip().lower()
    if not n:
        return None
    if "roth" in n:
        return TaxTreatmentDetail(treatment="roth", evidence=f"name:{n}", confidence="medium")
    if "hsa" in n or "health savings" in n:
        return TaxTreatmentDetail(treatment="hsa", evidence=f"name:{n}", confidence="medium")
    padded = f" {n} "
    if (
        "401k" in n
        or "401(k)" in n
        or " ira " in padded
        or "retirement" in n
        or "brokeragelink" in n
        or "brokerage link" in n
    ):
        return TaxTreatmentDetail(treatment="pretax", evidence=f"name:{n}", confidence="medium")
    if "brokerage" in n or "self-directed" in n or "taxable" in n:
        return TaxTreatmentDetail(treatment="taxable", evidence=f"name:{n}", confidence="medium")
    return None


def tax_treatment_detail(
    account_type: str | None, subtype: str | None, name: str | None = None
) -> TaxTreatmentDetail:
    """Map an account to the detailed five-way treatment, with evidence.

    Precedence: explicit subtype (high) -> cash-account subtype (medium) ->
    account-name heuristics (medium) -> brokerage type / individual-joint
    subtype (medium/low) -> unknown (low). The ``roth`` check runs first at
    every tier so "roth ira" / "BrokerageLink Roth" land in ``roth``.
    """
    s = (subtype or "").strip().lower()
    t = (account_type or "").strip().lower()
    if "roth" in s:
        return TaxTreatmentDetail(treatment="roth", evidence=f"subtype:{s}", confidence="high")
    if s == "hsa":
        return TaxTreatmentDetail(treatment="hsa", evidence=f"subtype:{s}", confidence="high")
    if "401k" in s or "ira" in s or s in _PRETAX_SUBTYPES:
        return TaxTreatmentDetail(treatment="pretax", evidence=f"subtype:{s}", confidence="high")
    if "brokerage" in s:
        return TaxTreatmentDetail(treatment="taxable", evidence=f"subtype:{s}", confidence="high")
    if s in _CASH_ACCOUNT_SUBTYPES:
        return TaxTreatmentDetail(treatment="taxable", evidence=f"subtype:{s}", confidence="medium")
    named = _name_tier(name)
    if named is not None:
        return named
    if t == "brokerage":
        return TaxTreatmentDetail(treatment="taxable", evidence=f"type:{t}", confidence="medium")
    if s in ("individual", "joint"):
        # Weakest positive signal: an individual/joint sleeve with no shelter
        # marker is a taxable account in practice (both consumers' legacy
        # heuristics agreed). Low confidence so operators can review.
        return TaxTreatmentDetail(treatment="taxable", evidence=f"subtype:{s}", confidence="low")
    evidence = f"subtype:{s}" if s else (f"type:{t}" if t else None)
    return TaxTreatmentDetail(treatment="unknown", evidence=evidence, confidence="low")


def tax_treatment(account_type: str | None, subtype: str | None, name: str | None = None) -> str:
    """The five-way treatment value alone (see :func:`tax_treatment_detail`)."""
    return tax_treatment_detail(account_type, subtype, name).treatment


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
    # Market value summed into each treatment, bucketed at the LOT level so a
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
    contributing ``account_id`` to its five-way :func:`tax_treatment`. The
    total book is the sum of position market values; ``percent_of_portfolio``
    is ``market_value / total × 100`` (matching the earnings-summary client's
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
        "tax_treatment is the detailed five-way value (taxable / pretax / roth "
        "/ hsa / unknown) inferred from each account's type + subtype; a bare "
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
