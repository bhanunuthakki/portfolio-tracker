"""Register verified short calls in the private Plaid snapshot overrides."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import Account, HoldingSnapshot, Item, ItemSource, Security
from portfolio_tracker.services.plaid_short_calls import (
    OVERRIDES_PATH,
    ShortCallOverride,
    load_short_call_overrides,
)


def verified_rules(
    session: Session, account_mask: str, symbols: list[str], confirmed_on: date, evidence: str
) -> list[ShortCallOverride]:
    accounts = session.scalars(
        select(Account)
        .join(Item)
        .where(
            Account.mask == account_mask,
            Item.source == ItemSource.PLAID.value,
            Item.is_data_active.is_(True),
        )
    ).all()
    if len(accounts) != 1:
        raise ValueError("Expected exactly one active Plaid account matching the account mask")
    account = accounts[0]
    latest = session.scalar(
        select(func.max(HoldingSnapshot.snapshot_date)).where(
            HoldingSnapshot.account_id == account.account_id
        )
    )
    tickers = session.scalars(
        select(Security.ticker)
        .join(HoldingSnapshot, Security.security_id == HoldingSnapshot.security_id)
        .where(
            HoldingSnapshot.account_id == account.account_id,
            HoldingSnapshot.snapshot_date == latest,
            HoldingSnapshot.quantity != 0,
            HoldingSnapshot.origin == "broker",
        )
    )
    held = {"".join(ticker.split()) for ticker in tickers if ticker}
    requested = {"".join(symbol.split()) for symbol in symbols}
    if not requested <= held:
        raise ValueError(
            "A requested contract is missing from the account's latest broker snapshot"
        )
    try:
        return [
            ShortCallOverride(
                plaid_account_id=account.plaid_account_id,
                occ_symbol=symbol,
                confirmed_on=confirmed_on,
                evidence=evidence,
            )
            for symbol in sorted(requested)
        ]
    except ValidationError:
        raise ValueError("Invalid short-call registration") from None


def register(rules: list[ShortCallOverride]) -> int:
    path = OVERRIDES_PATH
    existing = load_short_call_overrides(path)
    known = {(rule.plaid_account_id, "".join(rule.occ_symbol.split())) for rule in existing}
    additions = [rule for rule in rules if (rule.plaid_account_id, rule.occ_symbol) not in known]
    if not additions:
        return 0
    payload = TypeAdapter(tuple[ShortCallOverride, ...]).dump_json(
        (*existing, *additions), indent=2
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        temporary = staged.name
        staged.write(payload + b"\n")
    staged_path = Path(temporary)
    try:
        staged_path.replace(path)
    finally:
        staged_path.unlink(missing_ok=True)
    return len(additions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-mask", required=True)
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--confirmed-on", type=date.fromisoformat, required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    try:
        with SessionLocal() as session:
            rules = verified_rules(
                session, args.account_mask, args.symbol, args.confirmed_on, args.evidence
            )
        added = register(rules)
    except ValueError as exc:
        parser.exit(status=1, message=f"{exc}\n")
    print(f"Verified {len(rules)} short-call rule(s), registered {added}")


if __name__ == "__main__":
    main()
