"""Show imported 1099 status: per-broker/per-year totals, top P&L per ticker,
and per-ticker augmentation columns."""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from portfolio_tracker.db import engine
from portfolio_tracker.models import (
    TaxFormDividend,
    TaxFormImport,
    TaxFormInterest,
    TaxFormRealizedLot,
)


def main() -> None:
    with Session(engine) as session:
        # === 1. Import inventory ===
        print("=" * 100)
        print("IMPORTED 1099 INVENTORY")
        print("=" * 100)
        imports = session.execute(
            select(TaxFormImport).order_by(TaxFormImport.broker, TaxFormImport.tax_year)
        ).scalars().all()
        if not imports:
            print("\n  (no imports)")
        else:
            print(f"\n  {'id':<4} {'broker':<14} {'year':<6} {'acct mask':<14} {'recipient':<20} {'form types':<28} {'imported'}")
            for imp in imports:
                lots_n = session.execute(
                    select(func.count()).select_from(TaxFormRealizedLot).where(TaxFormRealizedLot.import_id == imp.import_id)
                ).scalar() or 0
                divs_n = session.execute(
                    select(func.count()).select_from(TaxFormDividend).where(TaxFormDividend.import_id == imp.import_id)
                ).scalar() or 0
                int_n = session.execute(
                    select(func.count()).select_from(TaxFormInterest).where(TaxFormInterest.import_id == imp.import_id)
                ).scalar() or 0
                print(f"  {imp.import_id:<4} {imp.broker:<14} {imp.tax_year:<6} {imp.account_mask or '':<14} {(imp.recipient_name or '')[:19]:<20} {(imp.form_types or '')[:27]:<28} {str(imp.imported_at)[:19]}")
                print(f"       {lots_n} realized lots, {divs_n} dividend rows, {int_n} interest rows")

        # === 2. Realized P&L summary per year ===
        print("\n" + "=" * 100)
        print("REALIZED P&L SUMMARY (from 1099-B detail)")
        print("=" * 100)
        rows = session.execute(
            select(
                TaxFormImport.tax_year,
                TaxFormImport.broker,
                TaxFormRealizedLot.term,
                func.count().label("n"),
                func.sum(TaxFormRealizedLot.proceeds).label("proceeds"),
                func.sum(TaxFormRealizedLot.cost_basis).label("cost"),
                func.sum(TaxFormRealizedLot.net_gain_loss).label("gain"),
            )
            .join(TaxFormImport, TaxFormImport.import_id == TaxFormRealizedLot.import_id)
            .group_by(TaxFormImport.tax_year, TaxFormImport.broker, TaxFormRealizedLot.term)
            .order_by(TaxFormImport.tax_year, TaxFormImport.broker, TaxFormRealizedLot.term)
        ).all()
        print(f"\n  {'year':<6} {'broker':<14} {'term':<8} {'lots':>5} {'proceeds':>14} {'cost':>14} {'gain/loss':>14}")
        for r in rows:
            print(f"  {r.tax_year:<6} {r.broker:<14} {r.term:<8} {r.n:>5} {Decimal(r.proceeds or 0):>14,.2f} {Decimal(r.cost or 0):>14,.2f} {Decimal(r.gain or 0):>14,.2f}")

        # === 3. Top winners + losers per year ===
        for yr in sorted({i.tax_year for i in imports}):
            print(f"\n--- TOP WINNERS + LOSERS {yr} (across all imports) ---")
            symbol_gain = session.execute(
                select(
                    TaxFormRealizedLot.symbol,
                    func.count().label("n"),
                    func.sum(TaxFormRealizedLot.net_gain_loss).label("gain"),
                )
                .join(TaxFormImport, TaxFormImport.import_id == TaxFormRealizedLot.import_id)
                .where(TaxFormImport.tax_year == yr)
                .group_by(TaxFormRealizedLot.symbol)
                .order_by(func.sum(TaxFormRealizedLot.net_gain_loss).desc())
            ).all()
            print(f"  Winners (top 10):")
            for r in symbol_gain[:10]:
                g = Decimal(r.gain or 0)
                if g <= 0:
                    break
                print(f"    {(r.symbol or '?'):<32} {r.n:>3} lot(s)  net gain ${g:>12,.2f}")
            print(f"  Losers (bottom 5):")
            for r in reversed(symbol_gain):
                g = Decimal(r.gain or 0)
                if g >= 0:
                    break
                print(f"    {(r.symbol or '?'):<32} {r.n:>3} lot(s)  net loss ${g:>12,.2f}")

        # === 4. Dividends per year + per security ===
        print("\n" + "=" * 100)
        print("DIVIDEND SUMMARY BY YEAR + TICKER (from 1099-DIV detail)")
        print("=" * 100)
        rows = session.execute(
            select(
                TaxFormImport.tax_year,
                TaxFormDividend.symbol,
                func.count().label("n"),
                func.sum(TaxFormDividend.amount).label("total"),
            )
            .join(TaxFormImport, TaxFormImport.import_id == TaxFormDividend.import_id)
            .group_by(TaxFormImport.tax_year, TaxFormDividend.symbol)
            .having(func.sum(TaxFormDividend.amount) != 0)
            .order_by(TaxFormImport.tax_year, func.sum(TaxFormDividend.amount).desc())
        ).all()
        cur_year = None
        for r in rows:
            if r.tax_year != cur_year:
                print(f"\n  {r.tax_year}:")
                cur_year = r.tax_year
            print(f"    {(r.symbol or '?'):<8} {r.n:>3} payment(s)  total ${Decimal(r.total or 0):>10,.2f}")


if __name__ == "__main__":
    main()
