"""After-tax realized return from imported 1099-B tax-lot data.

The 1099-B realized lots (`tax_form_realized_lots`) carry proceeds, cost
basis, wash-sale-disallowed amounts, net gain/loss, and short/long term — but
were only ever read PRE-TAX. For a taxable account holding long-term winners,
the pre-tax realized gain materially overstates the realizable benefit of
selling. This applies the owner's marginal short- and long-term rates to the
wash-sale-adjusted gains by term.

Deliberately simple (and noted as such): flat ST/LT marginal rates (no income
brackets / NIIT / state unless folded into the rate), no loss carryforward
across years, and `undetermined`-term gains taxed at the conservative ST rate.
A loss is treated as a tax shield at the same rate (negative tax). For exact
filing figures, the 1099-B and a tax professional remain authoritative.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import TaxFormImport, TaxFormRealizedLot

_DEFAULT_ST_RATE = Decimal("0.37")  # top federal ordinary bracket
_DEFAULT_LT_RATE = Decimal("0.20")  # top federal long-term capital-gains bracket


class TermSummary(BaseModel):
    term: str  # 'short' | 'long' | 'undetermined'
    taxable_gain: Decimal  # wash-sale-adjusted net gain/loss
    rate: Decimal
    tax: Decimal  # taxable_gain × rate (negative = loss shield)


class AfterTaxResult(BaseModel):
    tax_year: int | None  # None = all imported years
    st_rate: Decimal
    lt_rate: Decimal
    realized_gain_pretax: Decimal
    wash_sale_disallowed: Decimal
    total_tax: Decimal
    realized_gain_aftertax: Decimal
    effective_tax_rate_pct: Decimal | None
    by_term: list[TermSummary]
    lots_count: int
    notes: list[str]


def _empty(tax_year: int | None, st_rate: Decimal, lt_rate: Decimal) -> AfterTaxResult:
    return AfterTaxResult(
        tax_year=tax_year,
        st_rate=st_rate,
        lt_rate=lt_rate,
        realized_gain_pretax=Decimal(0),
        wash_sale_disallowed=Decimal(0),
        total_tax=Decimal(0),
        realized_gain_aftertax=Decimal(0),
        effective_tax_rate_pct=None,
        by_term=[],
        lots_count=0,
        notes=["No imported 1099-B realized lots for the requested scope."],
    )


def compute_after_tax(
    session: Session,
    tax_year: int | None = None,
    st_rate: Decimal = _DEFAULT_ST_RATE,
    lt_rate: Decimal = _DEFAULT_LT_RATE,
) -> AfterTaxResult:
    stmt = select(TaxFormRealizedLot)
    if tax_year is not None:
        stmt = stmt.join(
            TaxFormImport, TaxFormImport.import_id == TaxFormRealizedLot.import_id
        ).where(TaxFormImport.tax_year == tax_year)
    lots = list(session.execute(stmt).scalars().all())
    if not lots:
        return _empty(tax_year, st_rate, lt_rate)

    rate_for = {"short": st_rate, "long": lt_rate, "undetermined": st_rate}
    taxable_by_term: dict[str, Decimal] = {
        "short": Decimal(0),
        "long": Decimal(0),
        "undetermined": Decimal(0),
    }
    pretax = Decimal(0)
    wash = Decimal(0)
    for lot in lots:
        term = lot.term if lot.term in taxable_by_term else "undetermined"
        disallowed = lot.wash_sale_loss_disallowed or Decimal(0)
        # The disallowed portion of a loss can't be recognized, so it's added
        # back to the taxable gain/loss.
        taxable_by_term[term] += Decimal(lot.net_gain_loss) + Decimal(disallowed)
        pretax += Decimal(lot.net_gain_loss)
        wash += Decimal(disallowed)

    by_term: list[TermSummary] = []
    total_tax = Decimal(0)
    for term in ("short", "long", "undetermined"):
        taxable = taxable_by_term[term]
        if taxable == 0 and term == "undetermined":
            continue
        rate = rate_for[term]
        tax = (taxable * rate).quantize(Decimal("0.01"))
        total_tax += tax
        by_term.append(
            TermSummary(
                term=term, taxable_gain=taxable.quantize(Decimal("0.01")), rate=rate, tax=tax
            )
        )

    aftertax = pretax - total_tax
    eff_rate = (
        (total_tax / pretax * Decimal(100)).quantize(Decimal("0.01")) if pretax != 0 else None
    )
    notes = [
        "Flat marginal ST/LT rates; no income-bracket tiers, NIIT, state tax, "
        "or loss carryforward. 'undetermined'-term lots are taxed at the ST rate. "
        "The 1099-B and a tax professional are authoritative for filing.",
    ]
    if any(lot.term == "undetermined" for lot in lots):
        notes.append("Some lots have an undetermined holding term (taxed conservatively at ST).")

    return AfterTaxResult(
        tax_year=tax_year,
        st_rate=st_rate,
        lt_rate=lt_rate,
        realized_gain_pretax=pretax.quantize(Decimal("0.01")),
        wash_sale_disallowed=wash.quantize(Decimal("0.01")),
        total_tax=total_tax.quantize(Decimal("0.01")),
        realized_gain_aftertax=aftertax.quantize(Decimal("0.01")),
        effective_tax_rate_pct=eff_rate,
        by_term=by_term,
        lots_count=len(lots),
        notes=notes,
    )
