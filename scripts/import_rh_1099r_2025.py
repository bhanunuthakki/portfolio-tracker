"""Import the 2025 Robinhood 1099-R for account 703798470 (= acct 14, RH Roth IRA).

Source PDF: tax_forms/source_pdfs/RH 1099R.pdf
Recipient:  Bhanu Nuthakki

Single recharacterization:
  Gross distribution: $2,500.00
  Taxable amount:     $0.00
  Distribution code:  R (recharacterized IRA contribution made for 2024 or a
                      previous year and recharacterized in 2025)
  IRA/SEP/SIMPLE:     X (Traditional IRA, SEP, or SIMPLE)
  Federal/state withholding: none
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.db import engine
from portfolio_tracker.models import (
    Account,
    Item,
    TaxFormImport,
    TaxFormRetirementDistribution,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    with Session(engine) as session:
        acct_id = session.execute(
            select(Account.account_id)
            .join(Item, Item.item_id == Account.item_id)
            .where(Account.mask == "703798470")
        ).scalar_one_or_none()
        if acct_id is None:
            acct_id = 14

        existing = session.execute(
            select(TaxFormImport).where(
                TaxFormImport.broker == "Robinhood",
                TaxFormImport.tax_year == 2025,
                TaxFormImport.account_mask == "703798470",
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Already imported (import_id={existing.import_id}). Delete that row first to re-run.")
            return

        print(f"Importing 2025 RH 1099-R for acct 703798470 (acct_id={acct_id})")
        if not args.commit:
            print("(dry-run; re-run with --commit)\n")

        imp = TaxFormImport(
            broker="Robinhood",
            tax_year=2025,
            account_mask="703798470",
            account_id=acct_id,
            form_types="1099-R",
            file_path="tax_forms/source_pdfs/RH 1099R.pdf",
            document_id="4F8F47Q6J3G",
            statement_date=date(2026, 1, 22),
            recipient_name="Bhanu Nuthakki",
            notes="Robinhood Securities LLC. Recharacterized IRA contribution (code R). 2024 contribution recharacterized in 2025. Gross $2,500.00, taxable $0.00, IRA/SEP/SIMPLE box checked.",
        )
        if args.commit:
            session.add(imp)
            session.flush()
        else:
            imp.import_id = -1

        dist = TaxFormRetirementDistribution(
            import_id=imp.import_id,
            gross_distribution=Decimal("2500.00"),
            taxable_amount=Decimal("0.00"),
            federal_tax_withheld=None,
            state_tax_withheld=None,
            distribution_code="R",
            payer="Robinhood Securities LLC",
            notes="IRA/SEP/SIMPLE box checked. Code R = recharacterized IRA contribution made for 2024 or a previous year and recharacterized in 2025.",
        )
        if args.commit:
            session.add(dist)

        print(f"  1 retirement distribution row ($2,500.00 gross / $0 taxable / code R)")

        if args.commit:
            session.commit()
            print(f"\nCOMMITTED (import_id={imp.import_id})")


if __name__ == "__main__":
    main()
