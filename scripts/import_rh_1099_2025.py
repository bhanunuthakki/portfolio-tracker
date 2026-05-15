"""Import the 2025 Robinhood 1099 for account 945688521 (= acct 15).

Source PDF: C:/Users/Bhanu/Downloads/RH 1099 Consolidated.pdf  (2025 tax year)
Recipient:  Bhanu Nuthakki
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

# Reuse the 2024 CUSIP map and extend with 2025 additions
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from import_rh_1099_2024 import CUSIP_MAP as CUSIP_MAP_2024

CUSIP_MAP = {**CUSIP_MAP_2024}
CUSIP_MAP.update({
    "023135106": ("AMZN", "Amazon.com, Inc."),
    "11271J107": ("BN", "Brookfield Corporation"),
    "336433107": ("FSLR", "First Solar, Inc."),
    "37637K108": ("GTLB", "GitLab Inc. Class A"),
    "46436E718": ("SGOV", "iShares 0-3 Month Treasury Bond ETF"),
    "49845K101": ("KVYO", "Klaviyo, Inc."),
    "595112103": ("MU", "Micron Technology, Inc."),
    "70450Y103": ("PYPL", "PayPal Holdings, Inc."),
    "81730H109": ("S", "SentinelOne, Inc."),
    "86771W105": ("RUN", "Sunrun Inc."),
    "874039100": ("TSM", "Taiwan Semiconductor Manufacturing Co. Ltd."),
    "921946794": ("VYMI", "Vanguard International High Dividend Yield ETF"),
    "92204A702": ("VGT", "Vanguard Information Technology ETF"),
    "922908363": ("VOO", "Vanguard S&P 500 ETF"),
    "G6683N103": ("NU", "Nu Holdings Ltd."),
})


def _get_or_create_security(session: Session, cusip: str | None, symbol: str | None, name: str | None) -> int | None:
    if not symbol:
        return None
    sid = session.execute(select(Security.security_id).where(Security.ticker == symbol)).scalar_one_or_none()
    if sid is not None:
        return sid
    plaid_id = f"manual:1099-cusip-{cusip}" if cusip else f"manual:1099-sym-{symbol}"
    sec = Security(
        plaid_security_id=plaid_id, ticker=symbol, cusip=cusip, name=name,
        type="stock", currency="USD",
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


# ---- 1099-B SHORT-TERM (Box A, basis reported) ----
SHORT_TERM_LOTS = [
    ("Alphabet Inc. Class C", "02079K107", None,
     "11/13/25", "99.691", "27809.85", "Various", "16811.41", None, "10998.44",
     "short", "A", None, "Sale"),
    ("Brookfield Corporation", "11271J107", None,
     "04/29/25", "17.975", "958.41", "01/28/25", "1079.28", "120.87", "0.00",
     "short", "A", None, "Sale"),
    ("Brookfield Asset Management Ltd.", "113004105", None,
     "01/17/25", "9.604", "444.16", "Various", "393.27", None, "50.89",
     "short", "A", "N", "Sale; proceeds adjusted for option premium $12.01"),
    ("Meta Platforms, Inc. Class A", "30303M102", None,
     "06/02/25", "16.942", "11234.00", "Various", "9602.13", None, "1631.87",
     "short", "A", None, "Sale"),
    ("First Solar, Inc.", "336433107", None,
     "05/13/25", "50.000", "9599.73", "01/21/25", "9190.00", None, "409.73",
     "short", "A", None, "Sale"),
    ("First Solar, Inc.", "336433107", None,
     "06/06/25", "100.000", "16570.45", "Various", "16253.08", None, "317.37",
     "short", "A", None, "Total of 2 transactions"),
    ("Franklin FTSE India ETF", "35473P769", None,
     "01/02/25", "101.424", "3905.70", "Various", "3868.33", None, "37.37",
     "short", "A", None, "Sale"),
    ("Franklin FTSE Brazil ETF", "35473P835", None,
     "01/02/25", "154.673", "2178.45", "Various", "2655.42", None, "-476.97",
     "short", "A", None, "Sale"),
    ("GitLab Inc. Class A", "37637K108", None,
     "05/30/25", "2.000", "91.16", "05/15/25", "103.12", "11.96", "0.00",
     "short", "A", None, "Sale"),
    ("GitLab Inc. Class A", "37637K108", None,
     "08/22/25", "200.000", "9224.92", "Various", "10314.90", None, "-1089.98",
     "short", "A", None, "Sale"),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", None,
     "06/13/25", "50.000", "5026.50", "06/11/25", "5023.50", None, "3.00",
     "short", "A", None, "Sale"),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", None,
     "06/16/25", "64.657", "6499.99", "Various", "6496.12", None, "3.87",
     "short", "A", None, "Total of 2 transactions"),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", None,
     "06/20/25", "51.079", "5139.03", "Various", "5131.93", None, "7.10",
     "short", "A", None, "Sale"),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", None,
     "07/03/25", "11.169", "1121.64", "07/01/25", "1121.00", None, "0.64",
     "short", "A", None, "Sale"),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", None,
     "07/14/25", "16.000", "1608.32", "07/03/25", "1606.86", None, "1.46",
     "short", "A", None, "Sale"),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", None,
     "07/24/25", "20.000", "2012.80", "07/03/25", "2008.58", None, "4.22",
     "short", "A", None, "Sale"),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", None,
     "08/20/25", "40.570", "4081.38", "Various", "4074.56", None, "6.82",
     "short", "A", None, "Sale"),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", None,
     "12/29/25", "20.000", "2007.00", "12/08/25", "2009.20", "2.20", "0.00",
     "short", "A", None, "Sale"),
    ("JPMorgan Chase & Co.", "46625H100", None,
     "04/03/25", "1.765", "408.38", "Various", "402.46", None, "5.92",
     "short", "A", None, "Sale"),
    ("Klaviyo, Inc.", "49845K101", None,
     "05/15/25", "350.000", "12424.94", "Various", "12418.22", None, "6.72",
     "short", "A", None, "Sale"),
    ("Micron Technology, Inc.", "595112103", None,
     "12/05/25", "35.000", "8400.00", "09/29/25", "5777.45", None, "2622.55",
     "short", "A", None, "Sale"),
    ("Micron Technology, Inc.", "595112103", None,
     "12/10/25", "85.289", "22309.97", "Various", "14075.70", None, "8234.27",
     "short", "A", None, "Total of 2 transactions"),
    ("PayPal Holdings, Inc.", "70450Y103", None,
     "09/22/25", "261.428", "17707.76", "Various", "17892.00", None, "-184.24",
     "short", "A", None, "Sale"),
    ("SentinelOne, Inc.", "81730H109", None,
     "04/03/25", "175.000", "3181.08", "Various", "3939.46", None, "-758.38",
     "short", "A", None, "Sale"),
    ("SoFi Technologies, Inc.", "83406F102", None,
     "01/17/25", "700.000", "8909.39", "Various", "4872.25", None, "4037.14",
     "short", "A", None, "Total of 2 transactions"),
    ("SoFi Technologies, Inc.", "83406F102", None,
     "04/29/25", "58.371", "796.73", "07/08/24", "374.86", None, "421.87",
     "short", "A", None, "Sale"),
    ("SoFi Technologies, Inc.", "83406F102", None,
     "06/02/25", "400.000", "5408.39", "Various", "3301.77", None, "2106.62",
     "short", "A", None, "Sale"),
    ("Sunrun Inc.", "86771W105", None,
     "06/26/25", "75.000", "581.62", "06/16/25", "722.20", None, "-140.58",
     "short", "A", None, "Sale"),
    ("Sunrun Inc.", "86771W105", None,
     "07/03/25", "600.023", "6326.45", "Various", "5777.79", None, "548.66",
     "short", "A", None, "Total of 2 transactions"),
    ("Taiwan Semiconductor Manufacturing Co. Ltd.", "874039100", None,
     "09/29/25", "64.639", "17945.52", "09/22/25", "17708.00", None, "237.52",
     "short", "A", None, "Sale"),
    ("Vale S.A.", "91912E105", None,
     "04/03/25", "402.166", "3995.28", "Various", "4010.00", None, "-14.72",
     "short", "A", None, "Sale"),
    ("Vanguard International High Dividend Yield ETF", "921946794", None,
     "09/29/25", "129.813", "10956.80", "Various", "10255.23", None, "701.57",
     "short", "A", None, "Sale"),
    ("Vanguard Information Technology ETF", "92204A702", None,
     "04/28/25", "16.000", "8646.80", "04/10/25", "8065.44", None, "581.36",
     "short", "A", None, "Sale"),
    ("Vanguard Information Technology ETF", "92204A702", None,
     "04/29/25", "0.763", "417.94", "04/10/25", "384.56", None, "33.38",
     "short", "A", None, "Sale"),
    ("Vanguard S&P 500 ETF", "922908363", None,
     "11/10/25", "53.853", "33657.58", "Various", "29186.85", None, "4470.73",
     "short", "A", None, "Sale"),
    ("Vanguard Value ETF", "922908744", None,
     "01/02/25", "54.181", "9229.42", "Various", "9594.45", None, "-365.03",
     "short", "A", None, "Sale"),
    ("Vanguard Total Stock Market ETF", "922908769", None,
     "04/29/25", "6.074", "1652.90", "Various", "1644.50", "6.62", "15.02",
     "short", "A", None, "Sale"),
    ("Vanguard Total Stock Market ETF", "922908769", None,
     "05/29/25", "100.000", "28997.54", "Various", "26844.42", None, "2153.12",
     "short", "A", None, "Sale"),
    # Options
    ("AMZN 03/21/2025 CALL $250.00", None, "AMZN 03/21/25 C 250.000",
     "03/24/25", "1.000", "504.93", "03/21/25", "0.00", None, "504.93",
     "short", "A", None, "Option expiration short position"),
    ("AMZN 07/18/2025 CALL $210.00", None, "AMZN 07/18/25 C 210.000",
     "07/17/25", "1.000", "-900.11", "07/16/25", "0.00", None, "-900.11",
     "short", "A", None, "Short sale closed- option"),
    ("AMZN 11/21/2025 CALL $255.00", None, "AMZN 11/21/25 C 255.000",
     "11/24/25", "1.000", "397.95", "11/21/25", "0.00", None, "397.95",
     "short", "A", None, "Option expiration short position"),
    ("BABA 01/17/2025 CALL $110.00", None, "BABA 01/17/25 C 110.000",
     "01/21/25", "1.000", "127.95", "01/17/25", "0.00", None, "127.95",
     "short", "A", None, "Option expiration short position"),
    ("BN 07/18/2025 CALL $60.00", None, "BN 07/18/25 C 60.000",
     "05/12/25", "4.000", "-160.37", "05/09/25", "0.00", None, "-160.37",
     "short", "A", None, "Short sale closed- option"),
    ("BN 12/19/2025 CALL $75.00", None, "BN 12/19/25 C 75.000",
     "08/19/25", "4.000", "399.67", "08/18/25", "0.00", None, "399.67",
     "short", "A", None, "Short sale closed- option"),
    ("FSLR 07/18/2025 CALL $185.00", None, "FSLR 07/18/25 C 185.000",
     "06/03/25", "1.000", "-85.10", "06/02/25", "0.00", None, "-85.10",
     "short", "A", None, "Short sale closed- option"),
    ("Nu Holdings Ltd.", "G6683N103", None,
     "05/30/25", "365.000", "4374.45", "Various", "4578.72", "3.71", "-200.56",
     "short", "A", None, "Sale; specified lot"),
    ("TORM plc Class A", "G89479102", None,
     "04/10/25", "451.825", "6858.41", "Various", "10802.65", None, "-3944.24",
     "short", "A", None, "Sale"),
    ("GOOG 07/18/2025 CALL $175.00", None, "GOOG 07/18/25 C 175.000",
     "06/02/25", "1.000", "-65.11", "05/30/25", "0.00", None, "-65.11",
     "short", "A", None, "Short sale closed- option"),
    ("GOOG 07/18/2025 CALL $180.00", None, "GOOG 07/18/25 C 180.000",
     "05/30/25", "1.000", "354.95", "05/27/25", "525.04", None, "-170.09",
     "short", "A", None, "Option sale"),
    ("GOOG 11/21/2025 CALL $230.00", None, "GOOG 11/21/25 C 230.000",
     "09/03/25", "1.000", "-55.09", "09/02/25", "0.00", None, "-55.09",
     "short", "A", None, "Short sale closed- option"),
    ("JD 01/17/2025 CALL $50.00", None, "JD 01/17/25 C 50.000",
     "01/21/25", "1.000", "135.95", "01/17/25", "0.00", None, "135.95",
     "short", "A", None, "Option expiration short position"),
    ("NU 10/17/2025 CALL $14.00", None, "NU 10/17/25 C 14.000",
     "08/15/25", "8.000", "495.64", "07/24/25", "488.34", None, "7.30",
     "short", "A", None, "Option sale"),
    ("QQQ 06/20/2025 PUT $460.00", None, "QQQ 06/20/25 P 460.000",
     "05/07/25", "1.000", "964.92", "05/02/25", "716.04", None, "248.88",
     "short", "A", None, "Option sale"),
    ("QQQ 06/20/2025 PUT $460.00", None, "QQQ 06/20/25 P 460.000",
     "05/08/25", "5.000", "3329.66", "Various", "3232.22", None, "97.44",
     "short", "A", None, "Total of 2 transactions"),
    ("RUN 07/25/2025 CALL $9.50", None, "RUN 07/25/25 C 9.500",
     "07/07/25", "6.000", "-585.52", "Various", "0.00", None, "-585.52",
     "short", "A", None, "Total of 2 transactions"),
    ("S 02/21/2025 CALL $25.00", None, "S 02/21/25 C 25.000",
     "01/31/25", "1.000", "19.90", "01/30/25", "0.00", None, "19.90",
     "short", "A", None, "Short sale closed- option"),
    ("SOFI 03/21/2025 CALL $20.00", None, "SOFI 03/21/25 C 20.000",
     "03/24/25", "3.000", "410.82", "03/21/25", "0.00", None, "410.82",
     "short", "A", None, "Option expiration short position"),
    ("SOFI 07/18/2025 CALL $15.00", None, "SOFI 07/18/25 C 15.000",
     "05/16/25", "4.000", "155.62", "05/15/25", "0.00", None, "155.62",
     "short", "A", None, "Short sale closed- option"),
    ("VOO 09/19/2025 PUT $550.00", None, "VOO 09/19/25 P 550.000",
     "09/02/25", "3.000", "404.87", "07/24/25", "1200.12", None, "-795.25",
     "short", "A", None, "Option sale"),
    ("VTI 06/20/2025 CALL $285.00", None, "VTI 06/20/25 C 285.000",
     "05/12/25", "1.000", "-80.10", "05/09/25", "0.00", None, "-80.10",
     "short", "A", None, "Short sale closed- option"),
]

# ---- 1099-B LONG-TERM (Box D) ----
LONG_TERM_LOTS = [
    ("Alibaba Group Holding Ltd ADR", "01609W102", None,
     "03/21/25", "100.000", "12929.56", "Various", "21092.69", None, "-8163.13",
     "long", "D", "N", "Sale; proceeds adjusted for option premium $429.93"),
    ("Alphabet Inc. Class C", "02079K107", None,
     "04/29/25", "6.630", "1070.91", "10/10/22", "652.34", None, "418.57",
     "long", "D", None, "Sale"),
    ("Alphabet Inc. Class C", "02079K107", None,
     "09/10/25", "25.486", "6131.65", "Various", "3116.43", None, "3015.22",
     "long", "D", None, "Sale; specified lot"),
    ("Alphabet Inc. Class C", "02079K107", None,
     "11/13/25", "0.076", "21.23", "Various", "12.85", None, "8.38",
     "long", "D", None, "Sale"),
    ("Amazon.com, Inc.", "023135106", None,
     "04/29/25", "29.244", "5476.93", "Various", "1036.80", None, "4440.13",
     "long", "D", None, "Sale"),
    ("Brookfield Asset Management Ltd.", "113004105", None,
     "01/17/25", "390.396", "18055.11", "Various", "12907.35", None, "5147.76",
     "long", "D", "N", "Sale; proceeds adjusted for option premium $487.84"),
    ("Meta Platforms, Inc. Class A", "30303M102", None,
     "05/30/25", "0.130", "83.69", "10/24/22", "16.73", None, "66.96",
     "long", "D", None, "Sale"),
    ("Meta Platforms, Inc. Class A", "30303M102", None,
     "06/02/25", "15.058", "9984.69", "Various", "3418.70", None, "6565.99",
     "long", "D", None, "Sale"),
    ("JPMorgan Chase & Co.", "46625H100", None,
     "04/03/25", "39.240", "9078.69", "Various", "5989.06", None, "3089.63",
     "long", "D", None, "Sale"),
    ("JD.com Inc. ADR", "47215P106", None,
     "04/03/25", "100.000", "3999.28", "Various", "2607.81", None, "1391.47",
     "long", "D", None, "Sale"),
    ("SoFi Technologies, Inc.", "83406F102", None,
     "01/17/25", "300.000", "3122.76", "Various", "1487.82", None, "1634.94",
     "long", "D", "N", "Sale; proceeds adjusted for option premium $122.90"),
    ("Vale S.A.", "91912E105", None,
     "04/03/25", "53.784", "534.31", "Various", "777.73", None, "-243.42",
     "long", "D", None, "Sale"),
    ("Vanguard Total Stock Market ETF", "922908769", None,
     "04/29/25", "53.686", "14610.17", "Various", "11100.15", None, "3510.02",
     "long", "D", None, "Sale"),
    ("PagSeguro Digital Ltd.", "G68707101", None,
     "04/03/25", "400.000", "3339.01", "Various", "3484.04", None, "-145.03",
     "long", "D", None, "Sale"),
    ("TORM plc Class A", "G89479102", None,
     "04/10/25", "104.979", "1593.52", "Various", "3500.00", None, "-1906.48",
     "long", "D", None, "Sale"),
]

# ---- 1099-DIV detail ----
DIVIDENDS = [
    ("Alphabet Inc. Class C", "02079K107", "03/17/25", "11.44", "Qualified dividend", None),
    ("Alphabet Inc. Class C", "02079K107", "06/16/25", "26.25", "Qualified dividend", None),
    ("Alphabet Inc. Class C", "02079K107", "09/15/25", "26.28", "Qualified dividend", None),
    ("Brookfield Corporation", "11271J107", "03/31/25", "25.00", "Qualified dividend", "CA"),
    ("Brookfield Corporation", "11271J107", "03/31/25", "-3.75", "Foreign tax paid", "CA"),
    ("Brookfield Corporation", "11271J107", "06/30/25", "42.30", "Qualified dividend", "CA"),
    ("Brookfield Corporation", "11271J107", "06/30/25", "-6.35", "Foreign tax paid", "CA"),
    ("Brookfield Corporation", "11271J107", "09/29/25", "42.35", "Qualified dividend", "CA"),
    ("Brookfield Corporation", "11271J107", "09/29/25", "-6.35", "Foreign tax paid", "CA"),
    ("Brookfield Corporation", "11271J107", "12/31/25", "42.40", "Qualified dividend", "CA"),
    ("Brookfield Corporation", "11271J107", "12/31/25", "-6.36", "Foreign tax paid", "CA"),
    ("Meta Platforms, Inc. Class A", "30303M102", "03/26/25", "13.52", "Qualified dividend", None),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", "08/06/25", "10.65", "Nonqualified dividend", None),
    ("iShares 0-3 Month Treasury Bond ETF", "46436E718", "12/24/25", "81.89", "Nonqualified dividend", None),
    ("JPMorgan Chase & Co.", "46625H100", "01/31/25", "50.00", "Qualified dividend", None),
    ("Micron Technology, Inc.", "595112103", "10/21/25", "20.15", "Qualified dividend", None),
    ("Vale S.A.", "91912E105", "03/21/25", "169.87", "Qualified dividend", "BR"),
    ("Vale S.A.", "91912E105", "03/21/25", "21.05", "Qualified dividend", "BR"),
    ("Vale S.A.", "91912E105", "03/21/25", "-3.16", "Foreign tax paid", "BR"),
    ("Vanguard International High Dividend Yield ETF", "921946794", "06/24/25", "53.04", "Qualified dividend", None),
    ("Vanguard International High Dividend Yield ETF", "921946794", "06/24/25", "19.34", "Nonqualified dividend", None),
    ("Vanguard International High Dividend Yield ETF", "921946794", "06/24/25", "-5.66", "Foreign tax paid", None),
    ("Vanguard International High Dividend Yield ETF", "921946794", "09/23/25", "71.65", "Qualified dividend", None),
    ("Vanguard International High Dividend Yield ETF", "921946794", "09/23/25", "26.13", "Nonqualified dividend", None),
    ("Vanguard International High Dividend Yield ETF", "921946794", "09/23/25", "-7.65", "Foreign tax paid", None),
    ("Vanguard S&P 500 ETF", "922908363", "07/02/25", "90.76", "Qualified dividend", None),
    ("Vanguard S&P 500 ETF", "922908363", "07/02/25", "2.65", "Section 199A dividend", None),
    ("Vanguard S&P 500 ETF", "922908363", "10/01/25", "90.79", "Qualified dividend", None),
    ("Vanguard S&P 500 ETF", "922908363", "10/01/25", "2.65", "Section 199A dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "03/31/25", "106.70", "Qualified dividend", None),
    ("Vanguard Total Stock Market ETF", "922908769", "03/31/25", "7.32", "Section 199A dividend", None),
    ("TORM plc Class A", "G89479102", "04/02/25", "334.08", "Qualified dividend", "UK"),
]

# ---- 1099-INT interest detail ----
INTEREST = [
    ("Interest payment", "01/06/25", "0.01"),
    ("Interest payment", "01/31/25", "62.20"),
    ("Interest payment", "02/28/25", "12.98"),
    ("Interest payment", "03/31/25", "12.45"),
    ("Interest payment", "04/30/25", "4.89"),
    ("Interest payment", "05/30/25", "66.84"),
    ("Interest payment", "06/30/25", "0.01"),
    ("Security Lending Income (annual aggregate)", "12/31/25", "1.31"),
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
                TaxFormImport.tax_year == 2025,
                TaxFormImport.account_mask == "945688521",
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Already imported (import_id={existing.import_id}).")
            return

        print(f"Importing 2025 RH 1099 for acct 945688521 (acct_id={acct_id})")
        if not args.commit:
            print("(dry-run)")

        imp = TaxFormImport(
            broker="Robinhood",
            tax_year=2025,
            account_mask="945688521",
            account_id=acct_id,
            form_types="1099-B,1099-DIV,1099-INT",
            file_path="C:/Users/Bhanu/Downloads/RH 1099 Consolidated.pdf",
            document_id="B1F0N6QQ6B1",
            statement_date=date(2026, 2, 11),
            recipient_name="Bhanu Nuthakki",
            notes="Short-term gain $32,084.20; long-term gain $18,831.01; ordinary div $1,388.26; foreign tax paid $39.28; interest $160.69. Wash sale $145.36.",
        )
        if args.commit:
            session.add(imp)
            session.flush()
        else:
            imp.import_id = -1

        n_lots = 0
        for d in SHORT_TERM_LOTS + LONG_TERM_LOTS:
            (description, cusip, option_symbol, sold, qty, proceeds, acquired,
             cost, wash, gain, term, form_type, net_gross, info) = d
            symbol, name = _resolve_symbol(cusip or "", option_symbol)
            sid = _get_or_create_security(session, cusip, symbol, name or description) if args.commit else None
            acquired_dt = _parse_date(acquired)
            row = TaxFormRealizedLot(
                import_id=imp.import_id,
                description=description, symbol=symbol, cusip=cusip, security_id=sid,
                acquired_date=acquired_dt, acquired_various=(acquired_dt is None),
                disposed_date=_parse_date(sold), quantity=Decimal(qty),
                proceeds=Decimal(proceeds), cost_basis=Decimal(cost),
                wash_sale_loss_disallowed=Decimal(wash) if wash else None,
                net_gain_loss=Decimal(gain), term=term, form_8949_type=form_type,
                proceeds_net_or_gross=net_gross, additional_info=info,
            )
            if args.commit:
                session.add(row)
            n_lots += 1

        n_divs = 0
        for d in DIVIDENDS:
            (description, cusip, pmt_date, amount, tx_type, country) = d
            symbol, name = _resolve_symbol(cusip)
            sid = _get_or_create_security(session, cusip, symbol, name or description) if args.commit else None
            row = TaxFormDividend(
                import_id=imp.import_id, security_description=description,
                symbol=symbol, cusip=cusip, security_id=sid,
                payment_date=_parse_date(pmt_date), amount=Decimal(amount),
                transaction_type=tx_type, country=country,
            )
            if args.commit:
                session.add(row)
            n_divs += 1

        n_int = 0
        for d in INTEREST:
            (desc, pmt_date, amount) = d
            row = TaxFormInterest(
                import_id=imp.import_id, description=desc,
                payment_date=_parse_date(pmt_date), amount=Decimal(amount),
            )
            if args.commit:
                session.add(row)
            n_int += 1

        print(f"  {n_lots} realized lots; {n_divs} dividends; {n_int} interest rows")
        if args.commit:
            session.commit()
            print(f"\nCOMMITTED (import_id={imp.import_id})")


if __name__ == "__main__":
    main()
