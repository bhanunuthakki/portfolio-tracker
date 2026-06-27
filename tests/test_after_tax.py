"""Tests for the after-tax realized-gain overlay."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio_tracker.models import TaxFormImport, TaxFormRealizedLot
from portfolio_tracker.services.after_tax import compute_after_tax


def _lot(import_id: int, term: str, gl: str, wash: str | None = None) -> TaxFormRealizedLot:
    return TaxFormRealizedLot(
        import_id=import_id,
        disposed_date=date(2024, 6, 1),
        quantity=Decimal(10),
        proceeds=Decimal(0),
        cost_basis=Decimal(0),
        net_gain_loss=Decimal(gl),
        wash_sale_loss_disallowed=Decimal(wash) if wash is not None else None,
        term=term,
    )


def _seed(session) -> int:
    imp = TaxFormImport(broker="Fidelity", tax_year=2024)
    session.add(imp)
    session.flush()
    session.add_all(
        [
            _lot(imp.import_id, "long", "10000"),
            _lot(imp.import_id, "short", "5000"),
            _lot(imp.import_id, "short", "-2000", wash="500"),  # taxable -1500
        ]
    )
    session.commit()
    return imp.import_id


def test_after_tax_applies_term_rates_and_wash_addback(session):
    _seed(session)
    res = compute_after_tax(
        session, tax_year=2024, st_rate=Decimal("0.37"), lt_rate=Decimal("0.20")
    )
    assert res.realized_gain_pretax == Decimal("13000.00")  # 10000 + 5000 - 2000
    assert res.wash_sale_disallowed == Decimal("500.00")
    by_term = {t.term: t for t in res.by_term}
    assert by_term["short"].taxable_gain == Decimal("3500.00")  # 5000 + (-2000 + 500)
    assert by_term["short"].tax == Decimal("1295.00")  # 3500 × 0.37
    assert by_term["long"].taxable_gain == Decimal("10000.00")
    assert by_term["long"].tax == Decimal("2000.00")  # 10000 × 0.20
    assert res.total_tax == Decimal("3295.00")
    assert res.realized_gain_aftertax == Decimal("9705.00")  # 13000 - 3295
    assert res.effective_tax_rate_pct == Decimal("25.35")


def test_after_tax_year_filter_excludes_other_years(session):
    _seed(session)
    res = compute_after_tax(session, tax_year=2023)
    assert res.lots_count == 0
    assert res.realized_gain_aftertax == Decimal(0)


def test_after_tax_empty_when_no_lots(session):
    res = compute_after_tax(session)
    assert res.lots_count == 0
    assert res.by_term == []
