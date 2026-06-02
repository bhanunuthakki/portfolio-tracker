"""Find likely internal-transfer pairs across linked accounts and propose
classification overrides.

The TWR / contributions pipeline treats a Plaid `cash/contribution` from
account A as a real external inflow. But when the user moves money
between two of their own accounts (A → B), Plaid sees the deposit at B
as a contribution and the withdrawal at A as a separate withdrawal —
neither feed knows about the other.

This script scans all cashflow transactions and groups by absolute
amount + date proximity. When a (deposit, withdrawal) pair on different
accounts within `--window` days matches on amount within `--tolerance`,
it's a likely internal transfer and we recommend tagging both legs as
`internal`.

Use:
    python -m scripts.find_transfer_pairs               # dry-run report
    python -m scripts.find_transfer_pairs --commit      # apply overrides
    python -m scripts.find_transfer_pairs --commit --window 7 --tolerance 1.00
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

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import (
    Account,
    InvestmentTransaction,
    TransactionOverride,
)
from portfolio_tracker.services.performance import (
    effective_classification,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--window",
        type=int,
        default=5,
        help="Max days between the two legs of a candidate pair (default: 5).",
    )
    parser.add_argument(
        "--tolerance",
        type=Decimal,
        default=Decimal("1.00"),
        help="Max absolute $ difference between leg amounts (default: $1.00).",
    )
    args = parser.parse_args()

    with SessionLocal() as session:
        legs = _load_classified_legs(session)
        pairs = _find_pairs(legs, args.window, args.tolerance)

        if not pairs:
            print("No candidate pairs found.")
            return

        # Existing overrides — never quietly overwrite a user choice.
        existing = {
            ov.plaid_investment_transaction_id: ov
            for ov in session.execute(select(TransactionOverride)).scalars()
        }

        print(f"Found {len(pairs)} candidate pair(s) within {args.window}d / ${args.tolerance}:")
        for p in pairs:
            print()
            print(f"  [{p.diff_days}d apart, $-diff={p.amt_diff}]")
            for leg in (p.inflow, p.outflow):
                ovr = existing.get(leg.tx_id)
                marker = f"  [override={ovr.classification}]" if ovr else ""
                print(
                    f"    {leg.date} acct[{leg.account_id}] {leg.account_name[:25]:<25} "
                    f"{leg.effective:<14} ${float(leg.amount):>11,.2f}  {leg.name[:50]}{marker}"
                )

        if not args.commit:
            print()
            print("(dry-run; re-run with --commit to tag all legs internal)")
            return

        n = 0
        for p in pairs:
            for leg in (p.inflow, p.outflow):
                ovr = existing.get(leg.tx_id)
                if ovr is not None and ovr.classification != "internal":
                    # Don't quietly stomp on a different user choice.
                    print(
                        f"  SKIP {leg.tx_id} — already overridden to "
                        f"{ovr.classification!r}; leaving alone."
                    )
                    continue
                if ovr is None:
                    session.add(
                        TransactionOverride(
                            plaid_investment_transaction_id=leg.tx_id,
                            classification="internal",
                            notes=(
                                f"Auto-paired with leg on {p.other_leg(leg).date} "
                                f"in acct[{p.other_leg(leg).account_id}] "
                                f"({p.other_leg(leg).account_name})"
                            ),
                        )
                    )
                else:
                    ovr.classification = "internal"
                n += 1
        session.commit()
        print()
        print(f"WROTE {n} overrides")


class _Leg:
    __slots__ = (
        "account_id",
        "account_name",
        "amount",  # always positive (magnitude)
        "date",
        "effective",
        "name",
        "signed_in",  # True if cash flowed INTO this account
        "tx_id",
    )

    def __init__(
        self,
        tx_id: str,
        d: date,
        account_id: int,
        account_name: str,
        amount: Decimal,
        signed_in: bool,
        effective: str,
        name: str,
    ) -> None:
        self.tx_id = tx_id
        self.date = d
        self.account_id = account_id
        self.account_name = account_name
        self.amount = amount
        self.signed_in = signed_in
        self.effective = effective
        self.name = name


class _Pair:
    __slots__ = ("amt_diff", "diff_days", "inflow", "outflow")

    def __init__(self, inflow: _Leg, outflow: _Leg) -> None:
        self.inflow = inflow
        self.outflow = outflow
        self.diff_days = abs((inflow.date - outflow.date).days)
        self.amt_diff = abs(inflow.amount - outflow.amount)

    def other_leg(self, leg: _Leg) -> _Leg:
        return self.outflow if leg is self.inflow else self.inflow


def _load_classified_legs(session: Session) -> list[_Leg]:
    rows = session.execute(
        select(InvestmentTransaction, Account).join(
            Account, Account.account_id == InvestmentTransaction.account_id
        )
    ).all()
    out: list[_Leg] = []
    for tx, a in rows:
        eff = effective_classification(
            tx.type,
            tx.subtype,
            None,
            amount=Decimal(tx.amount or 0),
            name=tx.name,
        )
        if eff not in ("external_in", "external_out"):
            continue
        out.append(
            _Leg(
                tx_id=tx.plaid_investment_transaction_id,
                d=tx.date,
                account_id=a.account_id,
                account_name=a.name,
                amount=abs(Decimal(tx.amount or 0)),
                signed_in=(eff == "external_in"),
                effective=eff,
                name=tx.name or "",
            )
        )
    return out


def _find_pairs(legs: list[_Leg], window_days: int, tolerance: Decimal) -> list[_Pair]:
    """Greedy nearest-amount-within-window match between inflows and outflows
    across DIFFERENT accounts. Each leg can only pair with one other leg."""
    inflows = [leg for leg in legs if leg.signed_in]
    outflows = [leg for leg in legs if not leg.signed_in]

    # Sort inflows by date for stable processing
    inflows.sort(key=lambda x: x.date)
    used_out: set[str] = set()
    pairs: list[_Pair] = []
    for inflow in inflows:
        candidates: list[_Leg] = []
        for o in outflows:
            if o.tx_id in used_out:
                continue
            if o.account_id == inflow.account_id:
                continue
            if abs((o.date - inflow.date).days) > window_days:
                continue
            if abs(o.amount - inflow.amount) > tolerance:
                continue
            candidates.append(o)
        if not candidates:
            continue
        # Best match: smallest amount diff, then smallest date diff
        candidates.sort(
            key=lambda x: (abs(x.amount - inflow.amount), abs((x.date - inflow.date).days))
        )
        best = candidates[0]
        used_out.add(best.tx_id)
        pairs.append(_Pair(inflow=inflow, outflow=best))
    return pairs


if __name__ == "__main__":
    main()
