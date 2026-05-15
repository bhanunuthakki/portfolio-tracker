"""Import the 2024 Robinhood 1099 for account 945688521 (= acct 15).

Source PDF: C:/Users/Bhanu/Downloads/2024 1099 Robinhood.pdf
Recipient:  Bhanu Nuthakki

Data transcribed from the PDF — every row tagged with import_id so the UI
can attribute each piece of supplementary detail back to this 1099.
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


# ---- CUSIP -> (ticker, name) mapping ----
# Resolved by hand from the 1099 PDF + public sources. Add to this dict as new
# CUSIPs appear in future 1099s. Sticking to a flat dict so future-Elaine
# imports can extend without conflict.
CUSIP_MAP: dict[str, tuple[str, str]] = {
    "01609W102": ("BABA", "Alibaba Group Holding Limited ADR"),
    "02079K107": ("GOOG", "Alphabet Inc. Class C"),
    "11284V105": ("BEPC", "Brookfield Renewable Corporation Class A"),
    "113004105": ("BAM", "Brookfield Asset Management Ltd."),
    "14448C104": ("CARR", "Carrier Global Corporation"),
    "30303M102": ("META", "Meta Platforms, Inc. Class A"),
    "316773100": ("FITB", "Fifth Third Bancorp"),
    "35473P744": ("FLJP", "Franklin FTSE Japan ETF"),
    "35473P769": ("FLIN", "Franklin FTSE India ETF"),
    "35473P835": ("FLBR", "Franklin FTSE Brazil ETF"),
    "43475E105": ("HCMLY", "Holcim Ltd ADR"),
    "46434G822": ("EWJ", "iShares MSCI Japan ETF"),
    "46625H100": ("JPM", "JPMorgan Chase & Co."),
    "47215P106": ("JD", "JD.com Inc. ADR"),
    "496402108": ("KGSPY", "Kingspan Group plc ADR"),
    "52567D107": ("LMND", "Lemonade, Inc."),
    "55305B101": ("MHO", "M/I Homes, Inc."),
    "68268W103": ("OMF", "OneMain Holdings, Inc."),
    "79466L302": ("CRM", "Salesforce, Inc."),
    "81141R100": ("SE", "Sea Limited ADR"),
    "83406F102": ("SOFI", "SoFi Technologies, Inc."),
    "90138F102": ("TWLO", "Twilio Inc."),
    "91912E105": ("VALE", "Vale S.A."),
    "922908744": ("VTV", "Vanguard Value ETF"),
    "922908769": ("VTI", "Vanguard Total Stock Market ETF"),
    "G68707101": ("PAGS", "PagSeguro Digital Ltd."),
    "G89479102": ("TRMD", "TORM plc Class A"),
}


def _get_or_create_security(session: Session, cusip: str | None, symbol: str | None, name: str | None) -> int | None:
    """Return security_id for given symbol; create new Security if missing."""
    if not symbol:
        return None
    # Try by ticker
    sid = session.execute(select(Security.security_id).where(Security.ticker == symbol)).scalar_one_or_none()
    if sid is not None:
        return sid
    # Create new
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
    """Return (symbol, name) given a CUSIP. For options (CUSIP blank), return
    the raw option symbol."""
    if cusip and cusip in CUSIP_MAP:
        return CUSIP_MAP[cusip]
    if option_symbol:
        return (option_symbol, f"Option: {option_symbol}")
    return (None, None)


def _parse_date(s: str | None) -> date | None:
    """Parse MM/DD/YY string to date; return None for 'Various'."""
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


# ---- 1099-B SALES DATA (Short-term, covered, Box A) ----
# (description, cusip, option_symbol, sold_date, qty, proceeds, acquired_date,
#  cost_basis, wash_sale, gain, term, form_type, net_gross, additional_info)
SHORT_TERM_LOTS = [
    ("Brookfield Renewable Corp Class A Subordinate Voting Shares", "11284V105", None,
     "07/10/24", "57.047", "1711.40", "Various", "1591.47", None, "119.93",
     "short", "A", None, "Total of 2 transactions"),
    ("Brookfield Asset Management Ltd.", "113004105", None,
     "07/11/24", "46.990", "1879.57", "Various", "1580.52", None, "299.05",
     "short", "A", None, "Total of 2 transactions"),
    ("Carrier Global Corporation", "14448C104", None,
     "04/29/24", "29.336", "1780.08", "Various", "1201.57", None, "578.51",
     "short", "A", None, "Sale"),
    ("Carrier Global Corporation", "14448C104", None,
     "05/02/24", "0.567", "34.64", "Various", "30.83", None, "3.81",
     "short", "A", None, "Sale"),
    ("Fifth Third Bancorp", "316773100", None,
     "05/17/24", "100.000", "3265.85", "Various", "2761.19", None, "504.66",
     "short", "A", "N", "Sale; proceeds adjusted for option premium $65.95"),
    ("Fifth Third Bancorp", "316773100", None,
     "06/24/24", "51.971", "1897.13", "Various", "1390.06", None, "507.07",
     "short", "A", None, "Total of 2 transactions"),
    ("Franklin FTSE Japan ETF", "35473P744", None,
     "05/03/24", "169.787", "5118.15", "Various", "4950.00", None, "168.15",
     "short", "A", None, "Sale"),
    ("Holcim Ltd ADR", "43475E105", None,
     "06/12/24", "242.000", "4396.97", "Various", "3384.18", None, "1012.79",
     "short", "A", None, "Sale"),
    ("iShares MSCI Japan ETF", "46434G822", None,
     "01/16/24", "45.322", "3003.54", "Various", "3000.00", None, "3.54",
     "short", "A", None, "Sale"),
    ("Kingspan Group plc ADR", "496402108", None,
     "09/25/24", "41.000", "3935.89", "Various", "3389.66", None, "546.23",
     "short", "A", None, "Sale"),
    ("Lemonade, Inc.", "52567D107", None,
     "11/11/24", "200.409", "6863.34", "Various", "3258.36", None, "3604.98",
     "short", "A", None, "Sale"),
    ("OneMain Holdings, Inc.", "68268W103", None,
     "11/12/24", "30.946", "1669.97", "Various", "1449.75", None, "220.22",
     "short", "A", None, "Sale"),
    ("Salesforce, Inc.", "79466L302", None,
     "07/05/24", "0.013", "3.57", "04/12/24", "4.00", None, "-0.43",
     "short", "A", None, "Sale"),
    # Options (CUSIP blank in 1099)
    ("BABA 01/17/2025 CALL $120.00", None, "BABA 01/17/25 C 120.000",
     "01/11/24", "1.000", "204.95", "01/11/24", "211.03", None, "-6.08",
     "short", "A", None, "Option sale"),
    ("BABA 01/17/2025 CALL $120.00", None, "BABA 01/17/25 C 120.000",
     "05/31/24", "1.000", "70.92", "05/30/24", "0.00", None, "70.92",
     "short", "A", None, "Short sale closed- option"),
    ("BAM 10/18/2024 CALL $50.00", None, "BAM 10/18/24 C 50.000",
     "05/31/24", "3.000", "164.80", "05/30/24", "0.00", None, "164.80",
     "short", "A", None, "Short sale closed- option"),
    ("JD 01/17/2025 CALL $40.00", None, "JD 01/17/25 C 40.000",
     "09/17/24", "1.000", "107.92", "09/16/24", "0.00", None, "107.92",
     "short", "A", None, "Short sale closed- option"),
    ("PAGS 01/17/2025 CALL $20.00", None, "PAGS 01/17/25 C 20.000",
     "05/31/24", "4.000", "199.74", "05/30/24", "0.00", None, "199.74",
     "short", "A", None, "Short sale closed- option"),
    ("SOFI 04/19/2024 CALL $10.00", None, "SOFI 04/19/24 C 10.000",
     "01/12/24", "4.000", "-92.26", "01/11/24", "0.00", None, "-92.26",
     "short", "A", None, "Short sale closed- option"),
    ("TWLO 01/19/2024 CALL $90.00", None, "TWLO 01/19/24 C 90.000",
     "01/12/24", "1.000", "322.95", "01/11/24", "0.00", None, "322.95",
     "short", "A", None, "Short sale closed- option"),
    ("TWLO 01/17/2025 CALL $115.00", None, "TWLO 01/17/25 C 115.000",
     "05/31/24", "1.000", "306.92", "05/30/24", "0.00", None, "306.92",
     "short", "A", None, "Short sale closed- option"),
]

# ---- 1099-B SALES DATA (Long-term, covered, Box D) ----
LONG_TERM_LOTS = [
    ("Alibaba Group Holding Ltd ADR", "01609W102", None,
     "05/16/24", "21.748", "1885.85", "Various", "3142.65", None, "-1256.80",
     "long", "D", None, "Sale"),
    ("Alibaba Group Holding Ltd ADR", "01609W102", None,
     "07/15/24", "2.093", "164.17", "11/12/18", "293.00", "128.83", "0.00",
     "long", "D", None, "Sale"),
    ("Brookfield Renewable Corp Class A Subordinate Voting Shares", "11284V105", None,
     "07/10/24", "152.414", "4572.26", "Various", "4613.77", None, "-41.51",
     "long", "D", None, "Sale"),
    ("Carrier Global Corporation", "14448C104", None,
     "04/29/24", "64.664", "3923.77", "Various", "2660.43", None, "1263.34",
     "long", "D", None, "Sale"),
    ("JPMorgan Chase & Co.", "46625H100", None,
     "11/07/24", "26.850", "6384.60", "Various", "4069.54", None, "2315.06",
     "long", "D", None, "Sale"),
    ("M/I Homes, Inc.", "55305B101", None,
     "11/12/24", "32.500", "5320.78", "Various", "2947.14", None, "2373.64",
     "long", "D", None, "Sale"),
    ("OneMain Holdings, Inc.", "68268W103", None,
     "11/12/24", "110.966", "5988.09", "Various", "4055.08", None, "1933.01",
     "long", "D", None, "Sale"),
    ("Salesforce, Inc.", "79466L302", None,
     "07/05/24", "10.000", "2649.92", "10/05/16", "682.50", None, "1967.42",
     "long", "D", None, "Sale"),
    ("Sea Limited ADR", "81141R100", None,
     "06/06/24", "13.000", "935.97", "05/11/21", "2871.51", None, "-1935.54",
     "long", "D", None, "Sale"),
    ("SoFi Technologies, Inc.", "83406F102", None,
     "11/07/24", "399.997", "4833.75", "Various", "2081.83", None, "2751.92",
     "long", "D", None, "Sale"),
    ("Twilio Inc.", "90138F102", None,
     "09/20/24", "100.000", "6509.55", "Various", "9320.90", None, "-2811.35",
     "long", "D", "N", "Sale; proceeds adjusted for option premium $259.95"),
    ("PagSeguro Digital Ltd.", "G68707101", None,
     "05/14/24", "470.000", "5893.67", "Various", "12327.48", None, "-6433.81",
     "long", "D", None, "Sale"),
]

# ---- 1099-DIV DETAIL ----
# (security_description, cusip, payment_date, amount, transaction_type, country)
DIVIDENDS = [
    ("Alibaba Group Holding Ltd ADR", "01609W102", "01/18/24", "120.00", "Qualified dividend", "CH"),
    ("Alibaba Group Holding Ltd ADR", "01609W102", "07/12/24", "100.00", "Qualified dividend", "CH"),
    ("Alibaba Group Holding Ltd ADR", "01609W102", "07/12/24", "66.00", "Qualified dividend", "CH"),
    ("Alphabet Inc. Class C", "02079K107", "06/17/24", "6.42", "Qualified dividend", None),
    ("Alphabet Inc. Class C", "02079K107", "09/16/24", "6.43", "Qualified dividend", None),
    ("Alphabet Inc. Class C", "02079K107", "12/16/24", "6.44", "Qualified dividend", None),
    ("Brookfield Renewable Corp Class A", "11284V105", "03/28/24", "73.45", "Qualified dividend", None),
    ("Brookfield Renewable Corp Class A", "11284V105", "03/28/24", "-11.02", "Foreign tax paid", None),
    ("Brookfield Renewable Corp Class A", "11284V105", "06/28/24", "74.36", "Qualified dividend", None),
    ("Brookfield Renewable Corp Class A", "11284V105", "06/28/24", "-11.15", "Foreign tax paid", None),
    ("Brookfield Asset Management Ltd.", "113004105", "03/28/24", "168.55", "Qualified dividend", "CA"),
    ("Brookfield Asset Management Ltd.", "113004105", "03/28/24", "-25.28", "Foreign tax paid", "CA"),
    ("Brookfield Asset Management Ltd.", "113004105", "06/28/24", "169.86", "Qualified dividend", "CA"),
    ("Brookfield Asset Management Ltd.", "113004105", "06/28/24", "-25.48", "Foreign tax paid", "CA"),
    ("Brookfield Asset Management Ltd.", "113004105", "09/27/24", "152.00", "Qualified dividend", "CA"),
    ("Brookfield Asset Management Ltd.", "113004105", "09/27/24", "-22.80", "Foreign tax paid", "CA"),
    ("Brookfield Asset Management Ltd.", "113004105", "12/31/24", "152.00", "Qualified dividend", "CA"),
    ("Brookfield Asset Management Ltd.", "113004105", "12/31/24", "-22.80", "Foreign tax paid", "CA"),
    ("Carrier Global Corporation", "14448C104", "02/09/24", "17.91", "Qualified dividend", None),
    ("Carrier Global Corporation", "14448C104", "05/22/24", "0.11", "Qualified dividend", None),
    ("Meta Platforms, Inc. Class A", "30303M102", "03/26/24", "5.26", "Qualified dividend", None),
    ("Meta Platforms, Inc. Class A", "30303M102", "06/26/24", "7.59", "Qualified dividend", None),
    ("Meta Platforms, Inc. Class A", "30303M102", "09/26/24", "7.86", "Qualified dividend", None),
    ("Meta Platforms, Inc. Class A", "30303M102", "12/27/24", "12.86", "Qualified dividend", None),
    ("Fifth Third Bancorp", "316773100", "01/16/24", "52.11", "Qualified dividend", None),
    ("Fifth Third Bancorp", "316773100", "04/15/24", "52.65", "Qualified dividend", None),
    ("Franklin FTSE India ETF", "35473P769", "06/28/24", "12.21", "Qualified dividend", None),
    ("Franklin FTSE India ETF", "35473P769", "06/28/24", "9.95", "Nonqualified dividend", None),
    ("Franklin FTSE India ETF", "35473P769", "06/28/24", "-3.83", "Foreign tax paid", None),
    ("Franklin FTSE India ETF", "35473P769", "12/30/24", "13.63", "Qualified dividend", None),
    ("Franklin FTSE India ETF", "35473P769", "12/30/24", "11.09", "Nonqualified dividend", None),
    ("Franklin FTSE Brazil ETF", "35473P835", "06/28/24", "89.53", "Nonqualified dividend", None),
    ("Franklin FTSE Brazil ETF", "35473P835", "06/28/24", "-11.11", "Foreign tax paid", None),
    ("Franklin FTSE Brazil ETF", "35473P835", "12/30/24", "87.11", "Nonqualified dividend", None),
    ("Holcim Ltd ADR", "43475E105", "05/31/24", "149.60", "Nonqualified dividend", "SZ"),
    ("JPMorgan Chase & Co.", "46625H100", "01/31/24", "63.39", "Qualified dividend", None),
    ("JPMorgan Chase & Co.", "46625H100", "04/30/24", "76.00", "Qualified dividend", None),
    ("JPMorgan Chase & Co.", "46625H100", "07/31/24", "76.46", "Qualified dividend", None),
    ("JPMorgan Chase & Co.", "46625H100", "10/31/24", "83.56", "Qualified dividend", None),
    ("JD.com Inc. ADR", "47215P106", "04/29/24", "76.00", "Qualified dividend", "CH"),
    ("Kingspan Group plc ADR", "496402108", "06/04/24", "11.82", "Qualified dividend", "EI"),
    ("Kingspan Group plc ADR", "496402108", "06/04/24", "-2.96", "Foreign tax paid", "EI"),
    ("Kingspan Group plc ADR", "496402108", "10/28/24", "11.77", "Qualified dividend", "EI"),
    ("Kingspan Group plc ADR", "496402108", "10/28/24", "-2.94", "Foreign tax paid", "EI"),
    ("OneMain Holdings, Inc.", "68268W103", "02/23/24", "113.95", "Qualified dividend", None),
    ("OneMain Holdings, Inc.", "68268W103", "05/17/24", "121.05", "Qualified dividend", None),
    ("OneMain Holdings, Inc.", "68268W103", "08/16/24", "144.38", "Qualified dividend", None),
    ("OneMain Holdings, Inc.", "68268W103", "11/18/24", "147.59", "Qualified dividend", None),
    ("Salesforce, Inc.", "79466L302", "04/11/24", "4.00", "Qualified dividend", None),
    ("Vale S.A.", "91912E105", "03/26/24", "27.99", "Qualified dividend", "BR"),
    ("Vale S.A.", "91912E105", "09/11/24", "86.33", "Qualified dividend", "BR"),
    ("Vale S.A.", "91912E105", "09/11/24", "-12.95", "Foreign tax paid", "BR"),
    ("Vanguard Value ETF", "922908744", "12/26/24", "50.26", "Qualified dividend", None),
    ("Vanguard Value ETF", "922908744", "12/26/24", "2.10", "Section 199A dividend", None),
    ("Vanguard Value ETF", "922908744", "12/26/24", "0.44", "Nonqualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "03/27/24", "44.29", "Qualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "03/27/24", "3.02", "Section 199A dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "03/27/24", "1.40", "Nonqualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "07/02/24", "48.09", "Qualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "07/02/24", "3.29", "Section 199A dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "07/02/24", "1.52", "Nonqualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "10/01/24", "83.26", "Qualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "10/01/24", "5.69", "Section 199A dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "10/01/24", "2.63", "Nonqualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "12/26/24", "90.28", "Qualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "12/26/24", "6.17", "Section 199A dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "12/26/24", "2.85", "Nonqualified dividend", None),
    ("TORM plc Class A", "G89479102", "04/24/24", "142.77", "Qualified dividend", "UK"),
    ("TORM plc Class A", "G89479102", "06/04/24", "224.97", "Qualified dividend", "UK"),
    ("TORM plc Class A", "G89479102", "09/11/24", "282.25", "Qualified dividend", "UK"),
    ("TORM plc Class A", "G89479102", "12/04/24", "428.17", "Qualified dividend", "UK"),
]

# ---- 1099-INT INTEREST DETAIL ----
# Total reported: $266.78 (line 1)
# Detail aggregated by description from the PDF.
INTEREST = [
    ("Gold Deposit Boost Payment", "09/03/24", "0.16"),
    ("Interest payment", "01/31/24", "8.43"),
    ("Interest payment", "02/29/24", "4.51"),
    ("Interest payment", "03/28/24", "1.96"),
    ("Interest payment", "04/04/24", "0.40"),
    ("Interest payment", "04/30/24", "4.16"),
    ("Interest payment", "05/31/24", "33.56"),
    ("Interest payment", "06/28/24", "37.09"),
    ("Interest payment", "07/31/24", "35.17"),
    ("Interest payment", "08/30/24", "4.21"),
    ("Interest payment", "09/30/24", "7.82"),
    ("Interest payment", "10/31/24", "21.22"),
    ("Interest payment", "11/29/24", "55.13"),
    ("Interest payment", "12/19/24", "28.79"),
    ("Interest payment", "12/31/24", "5.88"),
    # Security Lending Income aggregated to a single monthly summary row to
    # keep the detail manageable. The $18.29 total is the sum of ~100 tiny
    # daily/monthly accruals reported on pages 11-14 of the PDF.
    ("Security Lending Income (12-month aggregate)", "12/31/24", "18.29"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    with Session(engine) as session:
        # Resolve acct 15 (mask 945688521)
        acct_id = session.execute(
            select(Account.account_id)
            .join(Item, Item.item_id == Account.item_id)
            .where(Account.mask == "945688521")
        ).scalar_one_or_none()
        if acct_id is None:
            # Fall back to acct 15 (Bhanu RH Individual via SnapTrade)
            acct_id = 15

        # Idempotency: skip if this (broker, year, account_mask) already imported
        existing = session.execute(
            select(TaxFormImport).where(
                TaxFormImport.broker == "Robinhood",
                TaxFormImport.tax_year == 2024,
                TaxFormImport.account_mask == "945688521",
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Already imported (import_id={existing.import_id}). Re-run by deleting that row first.")
            return

        print(f"Importing 2024 RH 1099 for acct 945688521 (acct_id={acct_id})")
        if not args.commit:
            print("(dry-run; re-run with --commit)\n")

        imp = TaxFormImport(
            broker="Robinhood",
            tax_year=2024,
            account_mask="945688521",
            account_id=acct_id,
            form_types="1099-B,1099-DIV,1099-INT",
            file_path="C:/Users/Bhanu/Downloads/2024 1099 Robinhood.pdf",
            document_id="B1F0N6QQ6B1",
            statement_date=date(2025, 1, 31),
            recipient_name="Bhanu Nuthakki",
            notes="Robinhood Securities LLC. Short-term gain $8,643.42; long-term gain $125.38; total ordinary div $4,092.72; foreign tax paid $152.32; interest $266.78.",
        )
        if args.commit:
            session.add(imp)
            session.flush()
        else:
            imp.import_id = -1  # placeholder for dry-run

        # Insert realized lots
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

        # Insert dividends
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

        # Insert interest
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
