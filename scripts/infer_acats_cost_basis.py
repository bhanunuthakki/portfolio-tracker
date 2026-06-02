"""Infer cost basis for positions that arrived via ACATS transfer.

When shares move between brokerages, the receiving broker reports them
with `cost_basis=NULL` (or sometimes $0). That inflates P&L on the
Holdings page because gain = market_value - 0 = entire market value.

This script pairs every ACATS-in event with its matching ACATS-out event
(same date, same security, same |qty|), computes the cost basis that
WAS in the source account at the transfer date, and writes it to
`cost_basis_overrides` with `source='inferred_acats'`.

Cost-basis lookup, in priority order:
  1. Source account's HoldingSnapshot the day BEFORE the transfer date
     (most reliable; uses the broker's own cost-basis tracking)
  2. Average-cost reconstruction from the source account's `buy`
     transactions in investment_transactions (fallback when no prior
     snapshot exists, e.g., the source account was added after the
     transfer)

Existing overrides with source='manual' are NEVER overwritten — those
are the user's explicit choices and inference must defer to them.

Run:
    python -m scripts.infer_acats_cost_basis [--commit]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import (
    Account,
    CostBasisOverride,
    HoldingSnapshot,
    InvestmentTransaction,
    Security,
)


@dataclass
class AcatsLeg:
    tx_id: str
    date: date
    account_id: int
    account_name: str
    security_id: int
    ticker: str
    quantity: Decimal  # signed (negative for OUT)


@dataclass
class InferredOverride:
    dest_account_id: int
    dest_account_name: str
    security_id: int
    ticker: str
    transferred_qty: Decimal
    per_share_cost: Decimal
    total_cost_basis: Decimal
    method: str  # "snapshot" | "buy_avg" | "skipped:reason"
    notes: str


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write rows to cost_basis_overrides. Without this, dry-run only.",
    )
    args = parser.parse_args()

    with SessionLocal() as session:
        in_legs = _load_legs(session, "external_asset_transfer_in")
        out_legs = _load_legs(session, "external_asset_transfer_out")

        # Pair on (date, security_id, abs(qty))
        out_index: dict[tuple[date, int, Decimal], list[AcatsLeg]] = defaultdict(list)
        for o in out_legs:
            out_index[(o.date, o.security_id, abs(o.quantity))].append(o)

        plans: list[InferredOverride] = []
        for inn in in_legs:
            key = (inn.date, inn.security_id, abs(inn.quantity))
            matches = out_index.get(key)
            if not matches:
                plans.append(
                    _skip(
                        inn, f"no matching ACATS-out (date={inn.date}, |qty|={abs(inn.quantity)})"
                    )
                )
                continue
            # Use first match. Could refine if there are multiple pairs on the
            # same day for the same security (rare).
            source_leg = matches[0]
            plan = _infer_for_pair(session, inn, source_leg)
            plans.append(plan)

        # Skip overrides where the user has already manually set one.
        manual_keys = {
            (ov.account_id, ov.security_id)
            for ov in session.execute(
                select(CostBasisOverride).where(CostBasisOverride.source == "manual")
            ).scalars()
        }

        print(f"{'mode':<26} {'dest acct':<28} {'tkr':<6} {'qty':>10}  {'cost':>14}  notes")
        print("-" * 120)
        n_written = n_skipped = n_protected = 0
        for p in plans:
            if (p.dest_account_id, p.security_id) in manual_keys:
                n_protected += 1
                print(
                    f"  {'skip:manual-override':<26} acct[{p.dest_account_id}] {p.dest_account_name[:18]:<18}  {p.ticker:<6}  {float(p.transferred_qty):>9,.2f}  $   skipped     manual override exists; not touching"
                )
                continue
            if p.method.startswith("skipped"):
                n_skipped += 1
                print(
                    f"  {p.method:<26} acct[{p.dest_account_id}] {p.dest_account_name[:18]:<18}  {p.ticker:<6}  {float(p.transferred_qty):>9,.2f}  ${'':>13}  {p.notes}"
                )
                continue
            print(
                f"  {p.method:<26} acct[{p.dest_account_id}] {p.dest_account_name[:18]:<18}  {p.ticker:<6}  {float(p.transferred_qty):>9,.2f}  ${float(p.total_cost_basis):>12,.2f}  {p.notes}"
            )
            if args.commit:
                _upsert_override(session, p)
                n_written += 1

        if args.commit:
            session.commit()

        print()
        print(f"  inferred: {sum(1 for p in plans if not p.method.startswith('skipped'))}")
        print(f"  skipped:  {n_skipped}")
        print(f"  protected by manual override: {n_protected}")
        if args.commit:
            print(f"  WRITTEN: {n_written}")
        else:
            print("  (dry-run; re-run with --commit to write)")


def _load_legs(session: Session, subtype: str) -> list[AcatsLeg]:
    rows = session.execute(
        select(InvestmentTransaction, Account, Security)
        .join(Account, Account.account_id == InvestmentTransaction.account_id)
        .join(
            Security,
            Security.security_id == InvestmentTransaction.security_id,
            isouter=True,
        )
        .where(InvestmentTransaction.subtype == subtype)
    ).all()
    out: list[AcatsLeg] = []
    for tx, a, sec in rows:
        if sec is None:
            continue
        out.append(
            AcatsLeg(
                tx_id=tx.plaid_investment_transaction_id,
                date=tx.date,
                account_id=tx.account_id,
                account_name=a.name,
                security_id=tx.security_id,
                ticker=sec.ticker or f"sec_{sec.security_id}",
                quantity=Decimal(tx.quantity or 0),
            )
        )
    return out


def _infer_for_pair(session: Session, in_leg: AcatsLeg, out_leg: AcatsLeg) -> InferredOverride:
    transferred = abs(in_leg.quantity)
    # Attempt 1: source-account snapshot the day before
    snap = session.execute(
        select(HoldingSnapshot)
        .where(HoldingSnapshot.account_id == out_leg.account_id)
        .where(HoldingSnapshot.security_id == out_leg.security_id)
        .where(HoldingSnapshot.snapshot_date < in_leg.date)
        .order_by(HoldingSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if (
        snap is not None
        and snap.cost_basis is not None
        and snap.cost_basis > 0
        and snap.quantity > 0
    ):
        per_share = Decimal(snap.cost_basis) / Decimal(snap.quantity)
        total = per_share * transferred
        return InferredOverride(
            dest_account_id=in_leg.account_id,
            dest_account_name=in_leg.account_name,
            security_id=in_leg.security_id,
            ticker=in_leg.ticker,
            transferred_qty=transferred,
            per_share_cost=per_share,
            total_cost_basis=total,
            method="snapshot",
            notes=f"from acct[{out_leg.account_id}] snapshot {snap.snapshot_date}: qty {float(snap.quantity):,.2f} cost ${float(snap.cost_basis):,.2f}",
        )

    # Attempt 2: running average from source-account buys
    avg_cost = _running_avg_cost_at(session, out_leg.account_id, out_leg.security_id, in_leg.date)
    if avg_cost is not None:
        total = avg_cost * transferred
        return InferredOverride(
            dest_account_id=in_leg.account_id,
            dest_account_name=in_leg.account_name,
            security_id=in_leg.security_id,
            ticker=in_leg.ticker,
            transferred_qty=transferred,
            per_share_cost=avg_cost,
            total_cost_basis=total,
            method="buy_avg",
            notes=f"avg cost from acct[{out_leg.account_id}] buy history before {in_leg.date}: ${float(avg_cost):,.4f}/sh",
        )

    return _skip(
        in_leg,
        f"no source snapshot or buy history (source acct[{out_leg.account_id}])",
    )


def _running_avg_cost_at(
    session: Session, account_id: int, security_id: int, asof_date: date
) -> Decimal | None:
    """Average cost per share in `account_id` for `security_id`, computed
    by walking buy/sell transactions up to (and including) the day BEFORE
    `asof_date`.

    Method: standard running average. Each BUY adds (qty, cost) to the
    running pool. Each SELL removes shares at the current avg price, so
    avg per-share is preserved. If the running quantity hits zero or below,
    we reset (defensive — shouldn't happen with consistent data).
    """
    rows = (
        session.execute(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.account_id == account_id)
            .where(InvestmentTransaction.security_id == security_id)
            .where(InvestmentTransaction.date < asof_date)
            .where(InvestmentTransaction.type.in_(("buy", "sell")))
            .order_by(
                InvestmentTransaction.date, InvestmentTransaction.plaid_investment_transaction_id
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None

    running_qty = Decimal(0)
    running_cost = Decimal(0)
    for tx in rows:
        qty = abs(Decimal(tx.quantity or 0))
        # Plaid signs `amount` from cash-account perspective:
        #   buy  → amount > 0 (cash debited; we paid that much)
        #   sell → amount < 0 (cash credited; we received that much)
        # We want the absolute dollars paid for buys.
        amt = abs(Decimal(tx.amount or 0))
        if tx.type == "buy":
            running_qty += qty
            running_cost += amt
        elif tx.type == "sell":
            if running_qty > 0:
                per_share = running_cost / running_qty if running_qty else Decimal(0)
                running_qty -= qty
                running_cost -= per_share * qty
                if running_qty <= 0:
                    running_qty = Decimal(0)
                    running_cost = Decimal(0)
    if running_qty <= 0:
        return None
    return running_cost / running_qty


def _upsert_override(session: Session, plan: InferredOverride) -> None:
    existing = session.get(CostBasisOverride, (plan.dest_account_id, plan.security_id))
    if existing is None:
        session.add(
            CostBasisOverride(
                account_id=plan.dest_account_id,
                security_id=plan.security_id,
                total_cost_basis=plan.total_cost_basis,
                notes=plan.notes,
                source="inferred_acats",
            )
        )
    else:
        # Only overwrite if it's also an inferred row (re-running the
        # script is OK). Manual rows are filtered out at the caller; this
        # is a belt-and-suspenders check.
        if existing.source != "manual":
            existing.total_cost_basis = plan.total_cost_basis
            existing.notes = plan.notes
            existing.source = "inferred_acats"


def _skip(in_leg: AcatsLeg, reason: str) -> InferredOverride:
    return InferredOverride(
        dest_account_id=in_leg.account_id,
        dest_account_name=in_leg.account_name,
        security_id=in_leg.security_id,
        ticker=in_leg.ticker,
        transferred_qty=abs(in_leg.quantity),
        per_share_cost=Decimal(0),
        total_cost_basis=Decimal(0),
        method=f"skipped:{reason}",
        notes=reason,
    )


if __name__ == "__main__":
    main()
