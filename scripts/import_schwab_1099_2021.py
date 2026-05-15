"""Import the 2021 Schwab 1099 Composite for account 1369-2708 (= acct 18 placeholder).

Source PDF: tax_forms/source_pdfs/Schwab 2021 1099.pdf
Recipient:  Bhanu Nuthakki
Address at the time: 10 Balmy St, San Francisco

This account is INACTIVE in our database (Item 7 / Account 18). It only exists
to attach historical 1099 data so we can show pre-Plaid trades.

Summary:
  Short A: proceeds 16,183.23 / cost 20,017.40 / gain -3,834.17
  Short B: proceeds  1,453.64 / cost  1,483.62 / gain    -29.98
  Long  D: proceeds 13,502.94 / cost  8,693.53 / gain  4,809.41
  Grand total proceeds $31,139.81; net realized gain $945.26.
  Total ordinary div $86.53 (qual $39.71); foreign tax paid $6.20.
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
    "00214Q302": ("ARKG", "ARK Genomic Revolution ETF"),
    "00326A104": ("SGOL", "Aberdeen Standard Physical Swiss Gold ETF"),
    "007903107": ("AMD", "Advanced Micro Devices, Inc."),
    "01609W102": ("BABA", "Alibaba Group Holding Limited ADR"),
    "056752108": ("BIDU", "Baidu Inc. ADR"),
    "20337X109": ("COMM", "CommScope Holding Co."),
    "46428Q109": ("SLV", "iShares Silver Trust"),
    "512807108": ("LRCX", "Lam Research Corp."),
    "55024U109": ("LITE", "Lumentum Holdings Inc."),
    "62548D100": ("MCHOY", "Multichoice Group Ltd. ADR"),
    "631512209": ("NPSNY", "Naspers Ltd. ADR"),
    "647581107": ("EDU", "New Oriental Education ADR"),
    "67066G104": ("NVDA", "Nvidia Corp."),
    "73739W104": ("POSH", "Poshmark Inc. Class A"),
    "74365P108": ("PROSY", "Prosus N.V. ADR"),
    "80810D103": ("SDGR", "Schrodinger Inc."),
    "83404D109": ("SFTBY", "Softbank Group Corp. ADR"),
    "88032Q109": ("TCEHY", "Tencent Holdings Ltd. ADR"),
    "90138F102": ("TWLO", "Twilio Inc."),
    "921943858": ("VEA", "Vanguard FTSE Developed Markets ETF"),
    "922042858": ("VWO", "Vanguard FTSE Emerging Markets ETF"),
    "G037AX101": ("AMBA", "Ambarella Inc."),
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
# Aggregated rows match the 1099-B as filed with IRS (year-end summary subtotals
# matching what was actually transmitted).
# (description, cusip, option_symbol, sold, qty, proceeds, acquired, cost, wash, gain, info)
SHORT_TERM_LOTS_A = [
    ("Alibaba Group Holding Ltd ADR", "01609W102", None,
     "04/28/21", "9.000", "2143.49", "11/10/20", "2422.37", None, "-278.88",
     "Sale; basis reported"),
    ("Ambarella Inc.", "G037AX101", None,
     "05/11/21", "22.000", "1829.51", "Various", "1088.08", None, "741.43",
     "Total of 2 transactions; FIFO"),
    ("ARK Genomic Revolution ETF", "00214Q302", None,
     "12/15/21", "73.000", "4315.98", "Various", "7223.41", None, "-2907.43",
     "Total of 4 transactions; FIFO"),
    ("BABA 03/18/2022 CALL $225.00", None, "BABA 03/18/22 C 225.000",
     "12/07/21", "1.000", "531.35", "12/07/21", "68.65", None, "462.70",
     "Short sale closed; option BC"),
    ("SLV 04/16/2021 CALL $25.00", None, "SLV 04/16/21 C 25.000",
     "04/16/21", "2.000", "0.00", "01/28/21", "489.30", None, "-489.30",
     "Option expired (loss = 2 lots at $244.65 basis)"),
    ("EDU 01/21/2022 CALL $2.50", None, "EDU 01/21/22 C 2.500",
     "12/07/21", "1.000", "30.35", "12/07/21", "14.65", None, "15.70",
     "Short sale closed; option BC"),
    ("EDU 09/17/2021 CALL $2.50", None, "EDU 09/17/21 C 2.500",
     "09/17/21", "1.000", "9.35", "08/19/21", "0.00", None, "9.35",
     "Short sale closed; option expired"),
    ("New Oriental Education ADR", "647581107", None,
     "12/06/21", "128.000", "237.44", "Various", "1684.02", None, "-1446.58",
     "Total of 4 transactions; FIFO"),
    ("Poshmark Inc. Class A", "73739W104", None,
     "08/19/21", "18.000", "475.12", "04/28/21", "801.00", None, "-325.88",
     "Sale"),
    ("Poshmark Inc. Class A", "73739W104", None,
     "09/02/21", "30.000", "867.06", "04/28/21", "1335.00", None, "-467.94",
     "Sale"),
    ("Schrodinger Inc.", "80810D103", None,
     "05/11/21", "10.000", "590.00", "10/30/20", "481.59", None, "108.41",
     "Total of 2 transactions"),
    ("Vanguard FTSE Developed Markets ETF", "921943858", None,
     "04/16/21", "50.000", "2567.12", "08/20/20", "2077.40", None, "489.72",
     "Sale"),
    ("Vanguard FTSE Emerging Markets ETF", "922042858", None,
     "04/16/21", "48.890", "2586.46", "Various", "2331.93", None, "254.53",
     "Total of 3 transactions"),
]

# ---- 1099-B SHORT-TERM, Box B (noncovered, basis NOT reported to IRS) ----
SHORT_TERM_LOTS_B = [
    ("iShares Silver Trust", "46428Q109", None,
     "06/28/21", "60.000", "1453.64", "01/28/21", "1483.62", None, "-29.98",
     "Noncovered; basis not reported"),
]

# ---- 1099-B LONG-TERM, Box D (covered, basis reported) ----
LONG_TERM_LOTS_D = [
    ("Aberdeen Standard Physical Swiss Gold ETF", "00326A104", None,
     "06/28/21", "199.000", "3402.28", "02/12/20", "2997.94", None, "404.34",
     "Sale"),
    ("Advanced Micro Devices, Inc.", "007903107", None,
     "12/15/21", "10.000", "1376.01", "10/27/16", "72.00", None, "1304.01",
     "Sale; long-held"),
    ("Ambarella Inc.", "G037AX101", None,
     "05/11/21", "5.000", "415.80", "Various", "191.38", None, "224.42",
     "Total of 2 transactions"),
    ("Baidu Inc. ADR", "056752108", None,
     "10/14/21", "5.000", "809.40", "11/08/18", "950.00", None, "-140.60",
     "Sale"),
    ("CommScope Holding Co.", "20337X109", None,
     "12/06/21", "30.000", "312.09", "03/12/20", "207.15", None, "104.94",
     "Sale"),
    ("Lumentum Holdings Inc.", "55024U109", None,
     "05/12/21", "10.000", "716.00", "Various", "486.69", None, "229.31",
     "Total of 4 transactions"),
    ("Multichoice Group Ltd. ADR", "62548D100", None,
     "05/25/21", "2.000", "18.92", "Various", "10.89", None, "8.03",
     "Total of 2 transactions"),
    ("Naspers Ltd. ADR", "631512209", None,
     "05/25/21", "11.000", "483.67", "Various", "400.17", None, "83.50",
     "Total of 2 transactions"),
    ("Prosus N.V. ADR", "74365P108", None,
     "05/25/21", "11.000", "228.80", "09/17/19", "168.58", None, "60.22",
     "Sale"),
    ("Schrodinger Inc.", "80810D103", None,
     "05/11/21", "2.000", "118.00", "02/14/20", "53.52", None, "64.48",
     "Sale"),
    ("Softbank Group Corp. ADR", "83404D109", None,
     "05/13/21", "34.000", "1334.83", "Various", "659.45", None, "675.38",
     "Total of 3 transactions"),
    ("Twilio Inc. Class A", "90138F102", None,
     "03/25/21", "1.000", "317.00", "03/18/20", "73.21", None, "243.79",
     "Sale"),
    ("Twilio Inc. Class A", "90138F102", None,
     "03/25/21", "4.000", "1268.39", "03/18/20", "292.85", None, "975.54",
     "Sale"),
    ("Vanguard FTSE Emerging Markets ETF", "922042858", None,
     "04/16/21", "51.090", "2701.75", "Various", "2129.70", None, "572.05",
     "Total of 4 transactions"),
]

# ---- 1099-DIV DETAIL (year-end aggregated by security) ----
# Schwab only reports totals on the 1099-DIV itself; the per-security breakdown
# is in the Year-End Summary. Each row here is the FINAL amount after issuer
# reclassification.
# (description, cusip, payment_date, amount, transaction_type, country)
DIVIDENDS = [
    ("Tencent Holdings Ltd. ADR", "88032Q109", "12/31/21", "9.28", "Nonqualified dividend", None),
    ("Vanguard FTSE Developed Markets ETF", "921943858", "12/31/21", "3.46", "Nonqualified dividend", None),
    ("Vanguard FTSE Developed Markets ETF", "921943858", "12/31/21", "8.84", "Qualified dividend", None),
    ("Vanguard FTSE Developed Markets ETF", "921943858", "12/31/21", "-0.74", "Foreign tax paid", None),
    ("Vanguard FTSE Emerging Markets ETF", "922042858", "12/31/21", "34.08", "Nonqualified dividend", None),
    ("Vanguard FTSE Emerging Markets ETF", "922042858", "12/31/21", "20.82", "Qualified dividend", None),
    ("Vanguard FTSE Emerging Markets ETF", "922042858", "12/31/21", "-5.12", "Foreign tax paid", None),
    ("Lam Research Corp.", "512807108", "12/31/21", "5.40", "Qualified dividend", None),
    ("Nvidia Corp.", "67066G104", "12/31/21", "1.28", "Qualified dividend", None),
    ("Softbank Group Corp. ADR", "83404D109", "12/31/21", "3.37", "Qualified dividend", "Japan"),
    ("Softbank Group Corp. ADR", "83404D109", "12/31/21", "-0.34", "Foreign tax paid", "Japan"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    with Session(engine) as session:
        # Always attach to the placeholder inactive Schwab account
        acct_id = 18

        existing = session.execute(
            select(TaxFormImport).where(
                TaxFormImport.broker == "Schwab",
                TaxFormImport.tax_year == 2021,
                TaxFormImport.account_mask == "1369-2708",
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Already imported (import_id={existing.import_id}). Delete that row first to re-run.")
            return

        print(f"Importing 2021 Schwab 1099 for acct 1369-2708 (placeholder acct_id={acct_id})")
        if not args.commit:
            print("(dry-run; re-run with --commit)\n")

        imp = TaxFormImport(
            broker="Schwab",
            tax_year=2021,
            account_mask="1369-2708",
            account_id=acct_id,
            form_types="1099-B,1099-DIV",
            file_path="tax_forms/source_pdfs/Schwab 2021 1099.pdf",
            document_id=None,
            statement_date=date(2022, 2, 11),
            recipient_name="Bhanu Nuthakki",
            notes="Charles Schwab & Co. Schwab One account 1369-2708. Net realized $945.26 (ST loss -$3,864.15 + LT gain $4,809.41). Ordinary div $86.53 (qual $39.71). Foreign tax paid $6.20. AMD long held since 2016 produced $1,304 LT gain.",
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
