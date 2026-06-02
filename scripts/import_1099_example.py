"""TEMPLATE: import a broker 1099 (B/DIV/INT) into the tax-form tables.

This is a generic, runnable example showing the import pattern. All data
below is **fake** (a fictional recipient, a fake broker, made-up CUSIPs and
amounts). Copy this file into the gitignored `scripts/private/` directory,
fill in your own 1099 detail, and run it there — keep real names, account
numbers, and tax figures out of version control.

    cp scripts/import_1099_example.py scripts/private/import_mybroker_2025.py
    # edit the copy with your real (private) data, then:
    python scripts/private/import_mybroker_2025.py            # dry-run
    python scripts/private/import_mybroker_2025.py --commit   # write rows

The script is idempotent: it refuses to re-import the same
(broker, tax_year, account_mask) twice. Delete that `tax_form_imports` row
first to re-run.

Note: 9-digit CUSIPs are public security identifiers (not PII). Account
numbers, recipient names, and addresses ARE sensitive — never commit them.
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
    Security,
    TaxFormDividend,
    TaxFormImport,
    TaxFormRealizedLot,
)

# --- Fill these in from your 1099 (example values are fictional) ---
BROKER = "ExampleBroker"
TAX_YEAR = 2025
ACCOUNT_MASK = "0000"  # last 4 of the account, or a placeholder
ACCOUNT_ID = 1  # the `accounts.account_id` this 1099 belongs to
RECIPIENT_NAME = "Jane Q. Investor"

# CUSIP -> (ticker, name). CUSIPs are public; safe to keep in the repo.
CUSIP_MAP: dict[str, tuple[str, str]] = {
    "922908769": ("VTI", "Vanguard Total Stock Market ETF"),
    "67066G104": ("NVDA", "Nvidia Corp."),
}

# 1099-B realized lots: (description, cusip, sold, qty, proceeds, acquired, cost, gain, term, box)
REALIZED_LOTS = [
    (
        "Vanguard Total Stock Market ETF",
        "922908769",
        "06/15/25",
        "10.000",
        "2500.00",
        "01/10/24",
        "2100.00",
        "400.00",
        "long",
        "D",
    ),
    (
        "Nvidia Corp.",
        "67066G104",
        "08/01/25",
        "5.000",
        "600.00",
        "03/01/25",
        "550.00",
        "50.00",
        "short",
        "A",
    ),
]

# 1099-DIV detail: (security_description, cusip, payment_date, amount, transaction_type)
DIVIDENDS = [
    ("Vanguard Total Stock Market ETF", "922908769", "03/28/25", "12.34", "Qualified dividend"),
]


def _get_or_create_security(session: Session, cusip: str, symbol: str, name: str) -> int:
    sid = session.execute(
        select(Security.security_id).where(Security.ticker == symbol)
    ).scalar_one_or_none()
    if sid is not None:
        return sid
    sec = Security(
        plaid_security_id=f"manual:1099-cusip-{cusip}",
        ticker=symbol,
        cusip=cusip,
        name=name,
        type="stock",
        currency="USD",
    )
    session.add(sec)
    session.flush()
    return sec.security_id


def _parse_date(s: str) -> date | None:
    mm, dd, yy = s.split("/")
    year = int(yy)
    year = 2000 + year if year < 100 else year
    return date(year, int(mm), int(dd))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="Write rows (default: dry-run).")
    args = parser.parse_args()

    with Session(engine) as session:
        existing = session.execute(
            select(TaxFormImport).where(
                TaxFormImport.broker == BROKER,
                TaxFormImport.tax_year == TAX_YEAR,
                TaxFormImport.account_mask == ACCOUNT_MASK,
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Already imported (import_id={existing.import_id}). Delete that row to re-run.")
            return

        print(f"Importing {TAX_YEAR} {BROKER} 1099 for acct mask {ACCOUNT_MASK}")
        if not args.commit:
            print("(dry-run; re-run with --commit)\n")

        imp = TaxFormImport(
            broker=BROKER,
            tax_year=TAX_YEAR,
            account_mask=ACCOUNT_MASK,
            account_id=ACCOUNT_ID,
            form_types="1099-B,1099-DIV",
            recipient_name=RECIPIENT_NAME,
        )
        if args.commit:
            session.add(imp)
            session.flush()
        else:
            imp.import_id = -1

        for desc, cusip, sold, qty, proceeds, acquired, cost, gain, term, box in REALIZED_LOTS:
            symbol, name = CUSIP_MAP.get(cusip, (None, desc))
            sid = (
                _get_or_create_security(session, cusip, symbol, name)
                if args.commit and symbol
                else None
            )
            row = TaxFormRealizedLot(
                import_id=imp.import_id,
                description=desc,
                symbol=symbol,
                cusip=cusip,
                security_id=sid,
                acquired_date=_parse_date(acquired),
                disposed_date=_parse_date(sold),
                quantity=Decimal(qty),
                proceeds=Decimal(proceeds),
                cost_basis=Decimal(cost),
                net_gain_loss=Decimal(gain),
                term=term,
                form_8949_type=box,
            )
            if args.commit:
                session.add(row)

        for desc, cusip, pmt_date, amount, tx_type in DIVIDENDS:
            symbol, name = CUSIP_MAP.get(cusip, (None, desc))
            sid = (
                _get_or_create_security(session, cusip, symbol, name)
                if args.commit and symbol
                else None
            )
            row = TaxFormDividend(
                import_id=imp.import_id,
                security_description=desc,
                symbol=symbol,
                cusip=cusip,
                security_id=sid,
                payment_date=_parse_date(pmt_date),
                amount=Decimal(amount),
                transaction_type=tx_type,
            )
            if args.commit:
                session.add(row)

        print(f"  {len(REALIZED_LOTS)} realized lots, {len(DIVIDENDS)} dividend rows")
        if args.commit:
            session.commit()
            print(f"\nCOMMITTED (import_id={imp.import_id})")


if __name__ == "__main__":
    main()
