"""Import the 2023 Robinhood 1099 for account 945688521 (= acct 15).

Source PDF: tax_forms/source_pdfs/Robinhood 1099_2023.pdf
Recipient:  Bhanu Nuthakki

Summary (per cover page):
  Short A: proceeds 16,167.40 / cost 15,878.28 / wash 2.27 / gain   291.39
  Long  D: proceeds  2,216.71 / cost  2,848.30 / wash 0.00 / gain  -631.59
  Grand total: -340.20
  Total ordinary div: $1,026.54 (qual $862.82, 199A $9.79, foreign tax $58.54)
  Interest: $324.69 (note: $312.19 single payment on 11/24/23 = treasury maturity)
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
    TaxFormInterest,
    TaxFormRealizedLot,
)


CUSIP_MAP: dict[str, tuple[str, str]] = {
    "02079K107": ("GOOG", "Alphabet Inc. Class C"),
    "11284V105": ("BEPC", "Brookfield Renewable Corporation Class A"),
    "113004105": ("BAM", "Brookfield Asset Management Ltd."),
    "14448C104": ("CARR", "Carrier Global Corporation"),
    "30303M102": ("META", "Meta Platforms, Inc. Class A"),
    "316773100": ("FITB", "Fifth Third Bancorp"),
    "37954Y830": ("COPX", "Global X Copper Miners ETF"),
    "46625H100": ("JPM", "JPMorgan Chase & Co."),
    "52603A208": ("LC", "LendingClub Corporation"),
    "68268W103": ("OMF", "OneMain Holdings, Inc."),
    "78464A755": ("XME", "SPDR S&P Metals & Mining ETF"),
    "78468R853": ("SPSM", "SPDR Portfolio S&P 600 Small Cap ETF"),
    "81730H109": ("S", "SentinelOne, Inc."),
    "83406F102": ("SOFI", "SoFi Technologies, Inc."),
    "922908769": ("VTI", "Vanguard Total Stock Market ETF"),
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


def _resolve_symbol(cusip: str, option_symbol: str | None = None) -> tuple[str | None, str | None]:
    if cusip and cusip in CUSIP_MAP:
        return CUSIP_MAP[cusip]
    if option_symbol:
        return (option_symbol, f"Option: {option_symbol}")
    return (None, None)


def _parse_date(s: str | None) -> date | None:
    if not s or s.lower() == "various":
        return None
    parts = s.split("/")
    if len(parts) != 3:
        return None
    mm, dd, yy = parts
    year = int(yy)
    if year < 100:
        year = 2000 + year if year < 50 else 1900 + year
    return date(year, int(mm), int(dd))


# ---- 1099-B SALES (Short-term, covered, Box A) ----
# (description, cusip, option_symbol, sold, qty, proceeds, acquired, cost, wash, gain,
#  term, form_type, net_gross, info)
SHORT_TERM_LOTS = [
    # Options (CUSIP blank, option_symbol set)
    ("PAGS 08/18/2023 CALL $12.50", None, "PAGS 08/18/23 C 12.500",
     "08/18/23", "2.000", "61.96", "Various", "0.00", None, "61.96",
     "short", "A", None, "Total of 2 transactions"),
    ("PAGS 11/17/2023 CALL $15.00", None, "PAGS 11/17/23 C 15.000",
     "11/17/23", "3.000", "44.98", "11/17/23", "0.00", None, "44.98",
     "short", "A", None, "Short sale closed- call expired; option written 07/28/23"),
    ("SOFI 10/20/2023 CALL $13.00", None, "SOFI 10/20/23 C 13.000",
     "10/20/23", "6.000", "125.90", "Various", "0.00", None, "125.90",
     "short", "A", None, "Total of 5 transactions"),
    ("TWLO 07/21/2023 CALL $80.00", None, "TWLO 07/21/23 C 80.000",
     "07/21/23", "1.000", "334.98", "07/21/23", "0.00", None, "334.98",
     "short", "A", None, "Short sale closed- call expired; option written 01/24/23"),
    # Equity
    ("Alphabet Inc. Class C Capital Stock", "02079K107", None,
     "03/23/23", "12.701", "1333.55", "Various", "1631.71", None, "-298.16",
     "short", "A", None, "Total of 3 transactions"),
    ("Alphabet Inc. Class C Capital Stock", "02079K107", None,
     "04/25/23", "28.193", "3007.01", "Various", "2989.29", None, "17.72",
     "short", "A", None, "Total of 4 transactions"),
    ("Meta Platforms, Inc. Class A Common Stock", "30303M102", None,
     "07/20/23", "4.000", "1227.59", "10/24/22", "515.54", None, "712.05",
     "short", "A", None, "Sale; FIFO"),
    ("Global X Copper Miners ETF (New)", "37954Y830", None,
     "09/28/23", "144.000", "5185.73", "Various", "5410.07", None, "-224.34",
     "short", "A", None, "Total of 13 transactions"),
    ("Global X Copper Miners ETF (New)", "37954Y830", None,
     "09/28/23", "0.397", "14.30", "01/24/23", "16.37", "2.07", "0.00",
     "short", "A", None, "Sale; FIFO; wash sale"),
    ("LendingClub Corporation", "52603A208", None,
     "09/07/23", "75.000", "508.86", "09/13/22", "1008.75", None, "-499.89",
     "short", "A", None, "Sale; FIFO"),
    ("SPDR S&P Metals & Mining ETF", "78464A755", None,
     "05/30/23", "10.573", "474.86", "Various", "470.22", None, "4.64",
     "short", "A", None, "Total of 4 transactions"),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", None,
     "09/28/23", "41.000", "1522.43", "Various", "1611.22", None, "-88.79",
     "short", "A", None, "Total of 8 transactions"),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", None,
     "09/28/23", "0.413", "15.32", "02/21/23", "16.58", "0.20", "-1.06",
     "short", "A", None, "Sale; FIFO; wash sale"),
    ("SentinelOne, Inc.", "81730H109", None,
     "07/20/23", "77.000", "1143.53", "Various", "1086.18", None, "57.35",
     "short", "A", None, "Total of 4 transactions"),
    ("SoFi Technologies, Inc. Common Stock", "83406F102", None,
     "01/23/23", "200.000", "1166.40", "Various", "1122.35", None, "44.05",
     "short", "A", None, "Total of 5 transactions"),
]

# ---- 1099-B SALES (Long-term, covered, Box D) ----
LONG_TERM_LOTS = [
    ("Alphabet Inc. Class C Capital Stock", "02079K107", None,
     "03/23/23", "7.299", "766.43", "Various", "1000.00", None, "-233.57",
     "long", "D", None, "Total of 2 transactions"),
    ("Alphabet Inc. Class C Capital Stock", "02079K107", None,
     "04/25/23", "1.807", "192.76", "04/22/22", "218.29", None, "-25.53",
     "long", "D", None, "Sale; FIFO"),
    ("SPDR S&P Metals & Mining ETF", "78464A755", None,
     "05/30/23", "28.000", "1257.52", "Various", "1630.01", None, "-372.49",
     "long", "D", None, "Total of 4 transactions"),
]

# ---- 1099-DIV DETAIL ----
# (description, cusip, payment_date, amount, transaction_type, country)
DIVIDENDS = [
    ("Brookfield Renewable Corporation Class A", "11284V105", "03/31/23", "39.61", "Qualified dividend", None),
    ("Brookfield Renewable Corporation Class A", "11284V105", "03/31/23", "-5.94", "Foreign tax paid", None),
    ("Brookfield Renewable Corporation Class A", "11284V105", "06/30/23", "39.94", "Qualified dividend", None),
    ("Brookfield Renewable Corporation Class A", "11284V105", "06/30/23", "-5.99", "Foreign tax paid", None),
    ("Brookfield Renewable Corporation Class A", "11284V105", "09/29/23", "68.31", "Qualified dividend", None),
    ("Brookfield Renewable Corporation Class A", "11284V105", "10/02/23", "-10.25", "Foreign tax paid", None),
    ("Brookfield Renewable Corporation Class A", "11284V105", "12/29/23", "69.15", "Qualified dividend", None),
    ("Brookfield Renewable Corporation Class A", "11284V105", "12/29/23", "-10.37", "Foreign tax paid", None),
    ("Brookfield Asset Management Ltd.", "113004105", "12/29/23", "139.00", "Nonqualified dividend", "CA"),
    ("Brookfield Asset Management Ltd.", "113004105", "12/29/23", "-20.85", "Foreign tax paid", "CA"),
    ("Carrier Global Corporation", "14448C104", "02/10/23", "3.70", "Qualified dividend", None),
    ("Carrier Global Corporation", "14448C104", "05/24/23", "12.64", "Qualified dividend", None),
    ("Carrier Global Corporation", "14448C104", "08/10/23", "17.32", "Qualified dividend", None),
    ("Carrier Global Corporation", "14448C104", "11/20/23", "17.37", "Qualified dividend", None),
    ("Fifth Third Bancorp Common Stock", "316773100", "10/16/23", "51.39", "Qualified dividend", None),
    ("Global X Copper Miners ETF (New)", "37954Y830", "07/10/23", "61.98", "Qualified dividend", None),
    ("Global X Copper Miners ETF (New)", "37954Y830", "07/10/23", "10.73", "Nonqualified dividend", None),
    ("Global X Copper Miners ETF (New)", "37954Y830", "07/10/23", "-5.14", "Foreign tax paid", None),
    ("JPMorgan Chase & Co.", "46625H100", "10/31/23", "52.50", "Qualified dividend", None),
    ("OneMain Holdings, Inc.", "68268W103", "05/12/23", "80.00", "Qualified dividend", None),
    ("OneMain Holdings, Inc.", "68268W103", "08/11/23", "84.23", "Qualified dividend", None),
    ("OneMain Holdings, Inc.", "68268W103", "11/10/23", "110.97", "Qualified dividend", None),
    ("SPDR S&P Metals & Mining ETF", "78464A755", "03/23/23", "6.50", "Qualified dividend", None),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", "03/23/23", "3.11", "Qualified dividend", None),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", "03/23/23", "1.43", "Nonqualified dividend", None),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", "03/23/23", "0.69", "Section 199A dividend", None),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", "06/08/23", "3.94", "Qualified dividend", None),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", "06/08/23", "1.80", "Nonqualified dividend", None),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", "06/08/23", "0.87", "Section 199A dividend", None),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", "06/23/23", "0.68", "Qualified dividend", None),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", "06/23/23", "0.31", "Nonqualified dividend", None),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", "06/23/23", "0.15", "Section 199A dividend", None),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", "09/21/23", "1.45", "Qualified dividend", None),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", "09/21/23", "0.66", "Nonqualified dividend", None),
    ("SPDR Portfolio S&P 600 Small Cap ETF", "78468R853", "09/21/23", "0.32", "Section 199A dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "03/28/23", "23.29", "Qualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "03/28/23", "1.31", "Section 199A dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "06/28/23", "30.02", "Qualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "06/28/23", "1.69", "Section 199A dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "09/26/23", "34.19", "Qualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "09/26/23", "1.92", "Section 199A dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "12/27/23", "50.53", "Qualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "12/27/23", "2.84", "Section 199A dividend", None),
]

# ---- 1099-INT DETAIL ----
# 2023 had ~75 micro-payments totaling $324.69. The $312.19 on 11/24/23 is the
# single material entry (treasury maturity); the rest are aggregated by month.
INTEREST = [
    ("Interest payment (Jan aggregate)", "01/31/23", "0.25"),
    ("Interest payment (Feb aggregate)", "02/28/23", "0.41"),
    ("Interest payment (Mar aggregate)", "03/31/23", "0.60"),
    ("Interest payment (Apr aggregate)", "04/30/23", "0.36"),
    ("Interest payment (May aggregate)", "05/31/23", "0.74"),
    ("Interest payment (Jun aggregate)", "06/30/23", "0.75"),
    ("Interest payment (Jul aggregate)", "07/31/23", "2.81"),
    ("Interest payment (Aug aggregate)", "08/31/23", "1.03"),
    ("Interest payment (Sep aggregate)", "09/30/23", "1.42"),
    ("Interest payment (Oct aggregate)", "10/31/23", "1.78"),
    ("Interest payment (Nov micro aggregate)", "11/30/23", "1.14"),
    ("Interest payment (Treasury / large)", "11/24/23", "312.19"),
    ("Interest payment (Dec aggregate)", "12/31/23", "1.21"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    with Session(engine) as session:
        acct_id = session.execute(
            select(Account.account_id)
            .join(Item, Item.item_id == Account.item_id)
            .where(Account.mask == "945688521")
        ).scalar_one_or_none()
        if acct_id is None:
            acct_id = 15

        existing = session.execute(
            select(TaxFormImport).where(
                TaxFormImport.broker == "Robinhood",
                TaxFormImport.tax_year == 2023,
                TaxFormImport.account_mask == "945688521",
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Already imported (import_id={existing.import_id}). Delete that row first to re-run.")
            return

        print(f"Importing 2023 RH 1099 for acct 945688521 (acct_id={acct_id})")
        if not args.commit:
            print("(dry-run; re-run with --commit)\n")

        imp = TaxFormImport(
            broker="Robinhood",
            tax_year=2023,
            account_mask="945688521",
            account_id=acct_id,
            form_types="1099-B,1099-DIV,1099-INT",
            file_path="tax_forms/source_pdfs/Robinhood 1099_2023.pdf",
            document_id="B1F0N6QQ6B1",
            statement_date=date(2024, 2, 12),
            recipient_name="Bhanu Nuthakki",
            notes="Robinhood Securities LLC. Short-term gain $291.39; long-term loss -$631.59; net realized -$340.20. Ordinary div $1,026.54; qualified $862.82; foreign tax paid $58.54; interest $324.69.",
        )
        if args.commit:
            session.add(imp)
            session.flush()
        else:
            imp.import_id = -1

        n_lots = 0
        for lot_data in SHORT_TERM_LOTS + LONG_TERM_LOTS:
            (description, cusip, option_symbol, sold, qty, proceeds, acquired,
             cost, wash, gain, term, form_type, net_gross, info) = lot_data
            symbol, name = _resolve_symbol(cusip or "", option_symbol)
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
                term=term,
                form_8949_type=form_type,
                proceeds_net_or_gross=net_gross,
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

        n_int = 0
        for int_data in INTEREST:
            (desc, pmt_date, amount) = int_data
            row = TaxFormInterest(
                import_id=imp.import_id,
                description=desc,
                payment_date=_parse_date(pmt_date),
                amount=Decimal(amount),
            )
            if args.commit:
                session.add(row)
            n_int += 1

        print(f"  {n_lots} realized lots (short-term + long-term)")
        print(f"  {n_divs} dividend detail rows")
        print(f"  {n_int} interest detail rows")

        if args.commit:
            session.commit()
            print(f"\nCOMMITTED (import_id={imp.import_id})")


if __name__ == "__main__":
    main()
