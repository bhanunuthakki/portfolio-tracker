"""Import the 2025 SoFi (Apex Clearing) 1099 for account 2FT74943 (= acct 6).

Source PDF: tax_forms/source_pdfs/Sofi 1099 Consolidated.pdf
Recipient:  Bhanu Nuthakki
Clearing:   Apex Clearing (carries SoFi Securities LLC)

Summary:
  Short-term covered: proceeds 48,601.98 / cost 42,127.98 / wash 21.78 / gain 6,495.78
  Long-term: zero
  1099-DIV total ordinary $582.48 (qualified $116.36, 199A $1.91)
  1099-INT, 1099-MISC, 1256 contracts: all zero
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
    Security,
    TaxFormDividend,
    TaxFormImport,
    TaxFormRealizedLot,
)


CUSIP_MAP: dict[str, tuple[str, str]] = {
    "02079K305": ("GOOGL", "Alphabet Inc. Class A Common Stock"),
    "192826501": ("FMYXX", "Fidelity Money Market Portfolio Select Class"),
    "46436E718": ("SGOV", "iShares 0-3 Month Treasury Bond ETF"),
    "922906300": ("VMFXX", "Vanguard Federal Money Market Fund"),
    "922908363": ("VOO", "Vanguard S&P 500 ETF"),
    "92206C573": ("VTC", "Vanguard Total Corporate Bond ETF"),
}


def _get_or_create_security(session: Session, cusip: str | None, symbol: str | None, name: str | None) -> int | None:
    if not symbol:
        return None
    sid = session.execute(select(Security.security_id).where(Security.ticker == symbol)).scalar_one_or_none()
    if sid is not None:
        return sid
    plaid_id = f"manual:1099-cusip-{cusip}" if cusip else f"manual:1099-sym-{symbol}"
    sec = Security(
        plaid_security_id=plaid_id,
        ticker=symbol,
        cusip=cusip,
        name=name,
        type="stock",
        currency="USD",
    )
    session.add(sec)
    session.flush()
    return sec.security_id


def _resolve_symbol(cusip: str) -> tuple[str | None, str | None]:
    if cusip and cusip in CUSIP_MAP:
        return CUSIP_MAP[cusip]
    return (None, None)


def _parse_date(s: str | None) -> date | None:
    """Parse YYYY-MM-DD or MM/DD/YY; return None for 'Various' or empty."""
    if not s or s.lower() == "various":
        return None
    if "-" in s:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    parts = s.split("/")
    if len(parts) != 3:
        return None
    mm, dd, yy = parts
    year = int(yy)
    if year < 100:
        year = 2000 + year if year < 50 else 1900 + year
    return date(year, int(mm), int(dd))


# ---- 1099-B SALES (Short-term, covered, Box A) ----
# (description, cusip, sold, qty, proceeds, acquired, cost, wash, gain, info)
SHORT_TERM_LOTS = [
    ("Alphabet Inc Class A Common Stock", "02079K305",
     "2025-11-19", "55", "15951.09", "Various", "9480.53", None, "6470.56",
     "Sale; basis reported"),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718",
     "2025-09-08", "114.49621", "11502.27", "Various", "11524.88", "0.56", "-22.05",
     "Sale; wash sale loss disallowed"),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718",
     "2025-11-03", "48.27164", "4846.46", "Various", "4856.69", "10.23", "0.00",
     "Sale; wash sale loss disallowed (full)"),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718",
     "2025-11-14", "49.75124", "5001.48", "Various", "5008.02", "6.54", "0.00",
     "Sale; wash sale loss disallowed (full)"),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718",
     "2025-11-20", "50", "5028.49", "2025-11-19", "5036.52", "4.45", "-3.58",
     "Sale; wash sale loss disallowed"),
    ("Vanguard Total Corporate Bond ETF", "92206C573",
     "2025-06-20", "82", "6272.19", "2025-06-09", "6221.34", None, "50.85",
     "Sale"),
]

# ---- 1099-DIV DETAIL ----
# (description, cusip, payment_date, amount, transaction_type, country)
# All money-market and bond ETF distributions are classed Total/Ordinary; only
# GOOGL + VOO have qualified portions and VOO has the $1.91 199A.
DIVIDENDS = [
    ("Alphabet Inc Class A Common Stock", "02079K305", "2025-06-16", "15.52", "Qualified dividend", None),
    ("Alphabet Inc Class A Common Stock", "02079K305", "2025-09-15", "23.42", "Qualified dividend", None),
    ("Alphabet Inc Class A Common Stock", "02079K305", "2025-12-15", "11.89", "Qualified dividend", None),
    ("Fidelity Money Market Portfolio Select Class", "192826501", "2025-05-30", "5.12", "Nonqualified dividend", None),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", "2025-07-07", "79.87", "Nonqualified dividend", None),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", "2025-08-06", "84.82", "Nonqualified dividend", None),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", "2025-09-05", "84.79", "Nonqualified dividend", None),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", "2025-10-06", "42.40", "Nonqualified dividend", None),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", "2025-11-06", "42.66", "Nonqualified dividend", None),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", "2025-12-04", "53.22", "Nonqualified dividend", None),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", "2025-12-24", "63.17", "Nonqualified dividend", None),
    ("Vanguard Federal Money Market Fund", "922906300", "2025-06-30", "8.16", "Nonqualified dividend", None),
    ("Vanguard S&P 500 ETF", "922908363", "2025-07-02", "8.58", "Qualified dividend", None),
    ("Vanguard S&P 500 ETF", "922908363", "2025-07-02", "0.25", "Section 199A dividend", None),
    ("Vanguard S&P 500 ETF", "922908363", "2025-10-01", "27.77", "Qualified dividend", None),
    ("Vanguard S&P 500 ETF", "922908363", "2025-10-01", "0.81", "Section 199A dividend", None),
    ("Vanguard S&P 500 ETF", "922908363", "2025-12-24", "29.18", "Qualified dividend", None),
    ("Vanguard S&P 500 ETF", "922908363", "2025-12-24", "0.85", "Section 199A dividend", None),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    with Session(engine) as session:
        # SoFi Bhanu account — mask suffix 4943 (DB) vs 2FT74943 (statement)
        acct_id = session.execute(
            select(Account.account_id)
            .join(Item, Item.item_id == Account.item_id)
            .where(Account.mask == "4943")
            .where(Item.institution_name.ilike("%SoFi%"))
            .where(Item.is_data_active.is_(True))
        ).scalar_one_or_none()
        if acct_id is None:
            acct_id = 6

        existing = session.execute(
            select(TaxFormImport).where(
                TaxFormImport.broker == "SoFi",
                TaxFormImport.tax_year == 2025,
                TaxFormImport.account_mask == "2FT74943",
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Already imported (import_id={existing.import_id}). Delete that row first to re-run.")
            return

        print(f"Importing 2025 SoFi 1099 for acct 2FT74943 (acct_id={acct_id})")
        if not args.commit:
            print("(dry-run; re-run with --commit)\n")

        imp = TaxFormImport(
            broker="SoFi",
            tax_year=2025,
            account_mask="2FT74943",
            account_id=acct_id,
            form_types="1099-B,1099-DIV",
            file_path="tax_forms/source_pdfs/Sofi 1099 Consolidated.pdf",
            document_id=None,
            statement_date=date(2026, 2, 12),
            recipient_name="Bhanu Nuthakki",
            notes="Apex Clearing carrying SoFi Securities LLC. Short-term gain $6,495.78; long-term $0. Ordinary div $582.48 (qualified $116.36, 199A $1.91). Wash sale disallowed $21.78 across SGOV lots. Margin interest expense $3.50 (informational, not reportable).",
        )
        if args.commit:
            session.add(imp)
            session.flush()
        else:
            imp.import_id = -1

        n_lots = 0
        for lot_data in SHORT_TERM_LOTS:
            (description, cusip, sold, qty, proceeds, acquired,
             cost, wash, gain, info) = lot_data
            symbol, name = _resolve_symbol(cusip)
            sid = _get_or_create_security(session, cusip, symbol, name or description) if args.commit else None
            acquired_dt = _parse_date(acquired)
            row = TaxFormRealizedLot(
                import_id=imp.import_id,
                description=description,
                symbol=symbol,
                cusip=cusip,
                security_id=sid,
                acquired_date=acquired_dt,
                acquired_various=(acquired_dt is None),
                disposed_date=_parse_date(sold),
                quantity=Decimal(qty),
                proceeds=Decimal(proceeds),
                cost_basis=Decimal(cost),
                wash_sale_loss_disallowed=Decimal(wash) if wash else None,
                net_gain_loss=Decimal(gain),
                term="short",
                form_8949_type="A",
                proceeds_net_or_gross=None,
                additional_info=info,
            )
            if args.commit:
                session.add(row)
            n_lots += 1

        n_divs = 0
        for div_data in DIVIDENDS:
            (description, cusip, pmt_date, amount, tx_type, country) = div_data
            symbol, name = _resolve_symbol(cusip)
            sid = _get_or_create_security(session, cusip, symbol, name or description) if args.commit else None
            row = TaxFormDividend(
                import_id=imp.import_id,
                security_description=description,
                symbol=symbol,
                cusip=cusip,
                security_id=sid,
                payment_date=_parse_date(pmt_date),
                amount=Decimal(amount),
                transaction_type=tx_type,
                country=country,
            )
            if args.commit:
                session.add(row)
            n_divs += 1

        print(f"  {n_lots} realized lots (all short-term)")
        print(f"  {n_divs} dividend detail rows")

        if args.commit:
            session.commit()
            print(f"\nCOMMITTED (import_id={imp.import_id})")


if __name__ == "__main__":
    main()
