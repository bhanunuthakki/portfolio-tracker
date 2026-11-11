"""Recover long/short direction for option holdings from feeds that drop it.

Plaid exposes no side field on an investment holding, and SoFi reports a short
option with a positive quantity and positive market value. A positive quantity
from such a feed is therefore *ambiguous*. This module settles it only from
broker trade history for the exact account and contract:

* Replay buys and sells (plus expiry/assignment/exercise) for that contract.
* If the replayed net position equals the held size and is short, correct the
  sign. If it equals the held size and is long, keep the row.
* Anything else (no history, a net that does not match, unsupported rows, or
  unusable units) leaves the row unchanged and is reported as a data-quality
  finding. Direction is never assumed from the broker name or from a single sell.

Only feeds listed in ``DIRECTION_OMITTING_INSTITUTIONS`` are examined; a feed
that carries signed quantities (Robinhood) is authoritative as delivered.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    ItemSource,
    Security,
)
from portfolio_tracker.services.option_contracts import option_from_occ

DIRECTION_OMITTING_INSTITUTIONS: tuple[str, ...] = ("sofi",)

_CONTRACT_MULTIPLIER = Decimal(100)
# Amount = quantity x price (+/- fees). Fees and rounding stay well inside 5%.
_UNIT_TOLERANCE = Decimal("0.05")
_CLOSING_SUBTYPE_MARKERS = ("expir", "assign", "exercise")


class Verdict(StrEnum):
    SHORT_CONFIRMED = "short_confirmed"
    LONG_CONFIRMED = "long_confirmed"
    NO_HISTORY = "no_history"
    NET_MISMATCH = "net_mismatch"
    UNUSABLE_HISTORY = "unusable_history"


@dataclass(frozen=True)
class OptionTrade:
    trade_date: date
    type: str
    subtype: str | None
    quantity: Decimal
    amount: Decimal
    price: Decimal | None


@dataclass(frozen=True)
class DirectionResult:
    verdict: Verdict
    net_units: Decimal | None
    detail: str


def is_direction_omitting(institution_name: str | None) -> bool:
    name = (institution_name or "").lower()
    return any(marker in name for marker in DIRECTION_OMITTING_INSTITUTIONS)


def normalize_symbol(ticker: str | None) -> str | None:
    return "".join(ticker.split()) if ticker else None


def _units(trade: OptionTrade) -> Decimal | None:
    """Absolute size in underlying units, or None when the unit is not provable."""
    size = abs(trade.quantity)
    if size == 0 or trade.price is None or trade.price <= 0:
        return None
    notional = abs(trade.amount)
    if notional == 0:
        return None
    for scale in (Decimal(1), _CONTRACT_MULTIPLIER):
        expected = size * scale * trade.price
        if abs(expected - notional) <= expected * _UNIT_TOLERANCE:
            return size * scale
    return None


def _collapse_unit_duplicates(trades: Sequence[OptionTrade]) -> list[OptionTrade]:
    """Drop re-ingested copies of one fill that differ only by unit scale.

    Older imports stored the same fill in contracts and in underlying units.
    Rows with equal date/type/amount/price and equal size in underlying units
    are one fill; true repeat fills keep identical raw units and are retained.
    """
    kept: list[OptionTrade] = []
    seen: dict[tuple[object, ...], set[Decimal]] = {}
    for trade in trades:
        units = _units(trade)
        if units is None:
            kept.append(trade)
            continue
        key = (trade.trade_date, trade.type, trade.amount, trade.price, units)
        raws = seen.setdefault(key, set())
        raw = abs(trade.quantity)
        if raws and raw not in raws:
            continue
        raws.add(raw)
        kept.append(trade)
    return kept


def reconcile_direction(held_units: Decimal, trades: Iterable[OptionTrade]) -> DirectionResult:
    """Replay one account+contract history against the absolute held size."""
    ordered = sorted(trades, key=lambda t: t.trade_date)
    if not ordered:
        return DirectionResult(Verdict.NO_HISTORY, None, "no broker trades for this contract")
    net = Decimal(0)
    for trade in _collapse_unit_duplicates(ordered):
        kind = trade.type.lower()
        subtype = (trade.subtype or "").lower()
        units = _units(trade)
        closing = any(marker in subtype for marker in _CLOSING_SUBTYPE_MARKERS)
        if kind == "sell" and not closing:
            sign = Decimal(-1)
        elif kind == "buy" and not closing:
            sign = Decimal(1)
        elif closing:
            if units is None:
                return DirectionResult(
                    Verdict.UNUSABLE_HISTORY, None, "closing row size cannot be established"
                )
            reduction = min(units, abs(net))
            net += reduction if net < 0 else -reduction
            continue
        else:
            return DirectionResult(
                Verdict.UNUSABLE_HISTORY, None, f"unsupported {kind} row on this contract"
            )
        if units is None:
            return DirectionResult(
                Verdict.UNUSABLE_HISTORY, None, "trade size cannot be tied to quantity x price"
            )
        if (trade.quantity < 0) != (sign < 0) and trade.quantity != 0:
            return DirectionResult(
                Verdict.UNUSABLE_HISTORY, None, f"{kind} row carries the opposite quantity sign"
            )
        net += sign * units
    if net == -held_units:
        return DirectionResult(Verdict.SHORT_CONFIRMED, net, "history nets to the held short size")
    if net == held_units:
        return DirectionResult(Verdict.LONG_CONFIRMED, net, "history nets to the held long size")
    return DirectionResult(
        Verdict.NET_MISMATCH, net, "history does not net to the held size (partial history?)"
    )


def contract_trades(session: Session, account_id: int, symbol: str) -> list[OptionTrade]:
    rows = session.execute(
        select(InvestmentTransaction, Security.ticker)
        .join(Security, Security.security_id == InvestmentTransaction.security_id)
        .where(InvestmentTransaction.account_id == account_id)
    ).all()
    return [
        OptionTrade(
            trade_date=tx.date,
            type=tx.type,
            subtype=tx.subtype,
            quantity=tx.quantity,
            amount=tx.amount,
            price=tx.price,
        )
        for tx, ticker in rows
        if normalize_symbol(ticker) == symbol
    ]


@dataclass(frozen=True)
class OptionHoldingRow:
    row: HoldingSnapshot
    account_id: int
    symbol: str
    result: DirectionResult


def examine_option_rows(
    session: Session, snapshot_date: date, account_ids: Iterable[int] | None = None
) -> list[OptionHoldingRow]:
    """Classify each non-zero option row in direction-omitting Plaid accounts."""
    query = (
        select(HoldingSnapshot, Security.ticker, Item.institution_name)
        .join(Security, Security.security_id == HoldingSnapshot.security_id)
        .join(Account, Account.account_id == HoldingSnapshot.account_id)
        .join(Item, Item.item_id == Account.item_id)
        .where(
            HoldingSnapshot.snapshot_date == snapshot_date,
            HoldingSnapshot.quantity != 0,
            HoldingSnapshot.origin == "broker",
            Item.source == ItemSource.PLAID.value,
            Item.is_data_active.is_(True),
        )
    )
    if account_ids is not None:
        query = query.where(HoldingSnapshot.account_id.in_(list(account_ids)))
    out: list[OptionHoldingRow] = []
    for row, ticker, institution in session.execute(query).all():
        symbol = normalize_symbol(ticker)
        if symbol is None or option_from_occ(symbol) is None:
            continue
        if not is_direction_omitting(institution) or row.quantity_unit != "underlying_units":
            continue
        result = reconcile_direction(
            abs(row.quantity), contract_trades(session, row.account_id, symbol)
        )
        out.append(OptionHoldingRow(row, row.account_id, symbol, result))
    return out


def apply_inferred_short_signs(
    session: Session, snapshot_date: date, account_ids: Iterable[int] | None = None
) -> int:
    """Flip positive rows whose history proves a short. Idempotent; returns rows flipped."""
    flipped = 0
    for item in examine_option_rows(session, snapshot_date, account_ids):
        row = item.row
        if row.quantity > 0 and item.result.verdict is Verdict.SHORT_CONFIRMED:
            row.quantity = -abs(row.quantity)
            if row.institution_value is not None:
                row.institution_value = -abs(row.institution_value)
            if row.cost_basis is not None:
                row.cost_basis = -abs(row.cost_basis)
            flipped += 1
    return flipped
