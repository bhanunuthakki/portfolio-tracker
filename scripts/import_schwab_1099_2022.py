"""Import the 2022 Schwab 1099 Composite for account 1369-2708 (= acct 18 placeholder).

Source PDF: tax_forms/source_pdfs/1099 Composite Schwab 2022.PDF
Recipient:  Bhanu Nuthakki (now at 938 Carolina St)

Summary:
  Short A: proceeds 2,939.62 / cost 2,457.47 / wash 1.16 / gain   483.31
  Short B: proceeds    61.20 / cost MISSING            / gain      --
  Long  D: proceeds 6,022.77 / cost 4,811.81           / gain 1,210.96
  Grand total: $9,023.59 proceeds; net realized $1,694.27 (incl. incomplete basis).
  Total ordinary div $255.36 (qual $64.49). Foreign tax paid $13.40.
  NVDA: $2,102.57 LT gain (held since 2016). LRCX: $527.92 LT.
  TCEHY: $1,723.45 LT loss (sold Dec 2022 after Chinese stock collapse).
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


CUSIP_MAP: dict[str, tuple[str, str]] = {
    "02079K107": ("GOOG", "Alphabet Inc. Class C"),
    "30303M102": ("META", "Meta Platforms, Inc. Class A"),
    "47215P106": ("JD", "JD.com Inc. ADR"),
    "512807108": ("LRCX", "Lam Research Corp."),
    "67066G104": ("NVDA", "Nvidia Corp."),
    "78467J100": ("SSNC", "SS&C Technologies Holdings, Inc."),
    "844741108": ("LUV", "Southwest Airlines Co."),
    "88032Q109": ("TCEHY", "Tencent Holdings Ltd. ADR"),
    "922042858": ("VWO", "Vanguard FTSE Emerging Markets ETF"),
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


# ---- 1099-B SHORT-TERM, Box A (covered, basis reported) ----
# (description, cusip, option_symbol, sold, qty, proceeds, acquired, cost, wash, gain, info)
SHORT_TERM_LOTS_A = [
    ("Alphabet Inc. Class C", "02079K107", None,
     "07/18/22", "0.000", "0.21", "07/11/22", "0.22", "0.01", "0.00",
     "Sale; tiny fractional"),
    ("BABA 10/21/2022 CALL $150.00", None, "BABA 10/21/22 C 150.000",
     "10/21/22", "1.000", "309.35", "04/12/22", "0.00", None, "309.35",
     "Short sale closed; option expired"),
    ("BABA 09/16/2022 CALL $165.00", None, "BABA 09/16/22 C 165.000",
     "03/03/22", "1.000", "619.35", "03/03/22", "235.65", None, "383.70",
     "Short sale closed; option BC"),
    ("BABA 09/16/2022 CALL $200.00", None, "BABA 09/16/22 C 200.000",
     "01/18/22", "1.000", "473.35", "01/18/22", "385.65", None, "87.70",
     "Short sale closed; option BC"),
    ("Meta Platforms Inc. Class A", "30303M102", None,
     "03/25/22", "3.000", "668.27", "02/16/22", "637.32", None, "30.95",
     "Sale"),
    ("SS&C Technologies Holdings", "78467J100", None,
     "06/22/22", "0.090", "5.39", "Various", "7.16", None, "-1.77",
     "Sale; fractional from DRIP"),
    ("SS&C Technologies Holdings", "78467J100", None,
     "06/22/22", "15.000", "863.70", "Various", "1191.47", "1.15", "-326.62",
     "Total of 5 transactions; FIFO; wash sale"),
]

# ---- 1099-B SHORT-TERM, Box B (noncovered, basis MISSING) ----
SHORT_TERM_LOTS_B = [
    ("Tencent Holdings Ltd. ADR", "88032Q109", None,
     "04/13/22", "0.010", "61.20", "04/13/22", "0", None, "0",
     "Noncovered; basis MISSING from broker; not reported to IRS"),
]

# ---- 1099-B LONG-TERM, Box D (covered, basis reported) ----
LONG_TERM_LOTS_D = [
    ("Lam Research Corp.", "512807108", None,
     "01/14/22", "1.000", "705.00", "02/07/18", "177.08", None, "527.92",
     "Sale; held 4 years"),
    ("Nvidia Corp.", "67066G104", None,
     "03/25/22", "8.000", "2245.57", "10/27/16", "143.00", None, "2102.57",
     "Sale; held 5.5 years"),
    ("Southwest Airlines Co.", "844741108", None,
     "03/25/22", "30.000", "1324.19", "08/18/20", "1020.27", None, "303.92",
     "Sale"),
    ("Tencent Holdings Ltd. ADR", "88032Q109", None,
     "12/02/22", "45.000", "1748.01", "Various", "3471.46", None, "-1723.45",
     "Total of 3 transactions; Chinese tech sell-off losses"),
]

# ---- 1099-DIV DETAIL (year-end aggregated by security) ----
DIVIDENDS = [
    ("Tencent Holdings Ltd. ADR", "88032Q109", "12/31/22", "70.38", "Nonqualified dividend", None),
    ("Vanguard FTSE Emerging Markets ETF", "922042858", "12/31/22", "120.49", "Nonqualified dividend", None),
    ("Vanguard FTSE Emerging Markets ETF", "922042858", "12/31/22", "37.76", "Qualified dividend", None),
    ("Vanguard FTSE Emerging Markets ETF", "922042858", "12/31/22", "-13.40", "Foreign tax paid", None),
    ("JD.com Inc. ADR", "47215P106", "12/31/22", "18.90", "Qualified dividend", None),
    ("Lam Research Corp.", "512807108", "12/31/22", "1.50", "Qualified dividend", None),
    ("Nvidia Corp.", "67066G104", "12/31/22", "0.32", "Qualified dividend", None),
    ("SS&C Technologies Holdings", "78467J100", "12/31/22", "6.01", "Qualified dividend", None),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    with Session(engine) as session:
        acct_id = 18  # placeholder Schwab Historical

        existing = session.execute(
            select(TaxFormImport).where(
                TaxFormImport.broker == "Schwab",
                TaxFormImport.tax_year == 2022,
                TaxFormImport.account_mask == "1369-2708",
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Already imported (import_id={existing.import_id}). Delete that row first to re-run.")
            return

        print(f"Importing 2022 Schwab 1099 for acct 1369-2708 (placeholder acct_id={acct_id})")
        if not args.commit:
            print("(dry-run; re-run with --commit)\n")

        imp = TaxFormImport(
            broker="Schwab",
            tax_year=2022,
            account_mask="1369-2708",
            account_id=acct_id,
            form_types="1099-B,1099-DIV",
            file_path="tax_forms/source_pdfs/1099 Composite Schwab 2022.PDF",
            document_id=None,
            statement_date=date(2023, 2, 10),
            recipient_name="Bhanu Nuthakki",
            notes="Charles Schwab & Co. Schwab One account 1369-2708. Net realized $1,694.27 (ST +$483.31, LT +$1,210.96). NVDA +$2,102 LT (16-22). TCEHY -$1,723 LT (Chinese tech sell-off). Ordinary div $255.36 (qual $64.49). Foreign tax $13.40. One TCEHY lot 0.01sh has MISSING basis (noncovered).",
        )
        if args.commit:
            session.add(imp)
            session.flush()
        else:
            imp.import_id = -1

        n_lots = 0
        all_lots = (
            [(*r, "short", "A") for r in SHORT_TERM_LOTS_A]
            + [(*r, "short", "B") for r in SHORT_TERM_LOTS_B]
            + [(*r, "long", "D") for r in LONG_TERM_LOTS_D]
        )
        for lot_data in all_lots:
            (description, cusip, option_symbol, sold, qty, proceeds, acquired,
             cost, wash, gain, info, term, form_type) = lot_data
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

        print(f"  {n_lots} realized lots (short A + short B + long D)")
        print(f"  {n_divs} dividend detail rows")

        if args.commit:
            session.commit()
            print(f"\nCOMMITTED (import_id={imp.import_id})")


if __name__ == "__main__":
    main()
