"""Cursor-paginated v1 history resources: transactions, cash flows, position
snapshots, and the securities master.

Pagination (PRD §7.5): stable keyset cursors, never OFFSET and never a silent
row cap. The cursor is an opaque base64 token over the ordering key; consumers
follow ``next_cursor`` until null. Historical endpoints take bounded date
parameters with documented defaults.

Cash-flow classification reuses the exact TWR pipeline primitives
(`services/performance.py`) so this endpoint can never disagree with the
return calculation it explains.
"""

from __future__ import annotations

import base64
import binascii
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    HoldingSnapshot,
    InvestmentTransaction,
    Security,
    SecurityClassification,
)
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.performance import (
    _load_transaction_overrides,  # pyright: ignore[reportPrivateUsage]
    _signed_cashflow,  # pyright: ignore[reportPrivateUsage]
    effective_classification,
)
from portfolio_tracker.services.positioning import classify_asset_type
from portfolio_tracker.services.v1_accounts import build_accounts_result
from portfolio_tracker.services.v1_common import V1Meta, build_meta

DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 1000
# Default lookback windows (documented in v1-overview.md).
TRANSACTIONS_DEFAULT_DAYS = 730  # Plaid's retention
SNAPSHOTS_DEFAULT_DAYS = 90


class InvalidCursorError(ValueError):
    """Raised when a cursor token does not decode to a valid ordering key."""


def _encode_cursor(*parts: str) -> str:
    raw = "|".join(parts)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(token: str, expected_parts: int) -> list[str]:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursorError("cursor is not valid base64") from exc
    parts = raw.split("|")
    if len(parts) != expected_parts:
        raise InvalidCursorError(f"cursor must have {expected_parts} parts")
    return parts


def _history_meta(
    session: Session,
    *,
    as_of: date,
    methodology: str,
    links: dict[str, str],
    generated_at: datetime | None = None,
) -> V1Meta:
    """Envelope for history endpoints — coverage/sync from the accounts
    builder; ``as_of`` is the query window end (event streams have no single
    observation date)."""
    accounts_meta = build_accounts_result(session).meta
    return build_meta(
        as_of=as_of,
        source_providers=accounts_meta.source_providers,
        coverage=accounts_meta.account_coverage,
        last_successful_sync_at=accounts_meta.last_successful_sync_at,
        warnings=[],
        links=links,
        methodology=methodology,
        methodology_version="1",
        # History windows are user-chosen; the *holdings* staleness signal
        # lives on snapshot/accounts responses. Suppress date-based staleness
        # by evaluating against the window end itself.
        today=as_of,
        generated_at=generated_at,
    )


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class TransactionV1(BaseModel):
    transaction_id: str
    account_id: int
    account_name: str
    security_id: int | None
    ticker: str | None
    date: date
    name: str | None
    quantity: Decimal
    amount: Decimal
    price: Decimal | None
    fees: Decimal | None
    type: str
    subtype: str | None
    currency: str
    # User override (None if none) and the classification the TWR pipeline
    # actually uses (override wins; else heuristic).
    override_classification: str | None
    effective_classification: str | None


class TransactionsV1Result(BaseModel):
    meta: V1Meta
    start_date: date
    end_date: date
    transactions: list[TransactionV1]
    next_cursor: str | None


def _bounded_window(
    start_date: date | None, end_date: date | None, default_days: int
) -> tuple[date, date]:
    end = end_date or date.today()
    start = start_date or (end - timedelta(days=default_days))
    return start, end


def build_transactions_page(
    session: Session,
    *,
    start_date: date | None,
    end_date: date | None,
    limit: int,
    cursor: str | None,
    generated_at: datetime | None = None,
) -> TransactionsV1Result:
    start, end = _bounded_window(start_date, end_date, TRANSACTIONS_DEFAULT_DAYS)
    accts = active_account_ids(session)
    links = {"cash_flows": "/api/v1/cash-flows", "accounts": "/api/v1/accounts"}
    meta = _history_meta(
        session,
        as_of=end,
        methodology="transactions.normalized",
        links=links,
        generated_at=generated_at,
    )
    if not accts:
        return TransactionsV1Result(
            meta=meta, start_date=start, end_date=end, transactions=[], next_cursor=None
        )

    # NOTE: the isouter join makes Security nullable at runtime even though
    # SQLAlchemy's static Select type says otherwise; the loop guards on None.
    stmt = (
        select(InvestmentTransaction, Account, Security)
        .join(Account, Account.account_id == InvestmentTransaction.account_id)
        .join(
            Security,
            Security.security_id == InvestmentTransaction.security_id,
            isouter=True,
        )
        .where(InvestmentTransaction.date >= start)
        .where(InvestmentTransaction.date <= end)
        .where(InvestmentTransaction.account_id.in_(accts))
        .order_by(
            InvestmentTransaction.date.desc(),
            InvestmentTransaction.plaid_investment_transaction_id.desc(),
        )
    )
    if cursor is not None:
        cursor_date_raw, cursor_id = _decode_cursor(cursor, 2)
        try:
            cursor_date = date.fromisoformat(cursor_date_raw)
        except ValueError as exc:
            raise InvalidCursorError("cursor date is not ISO YYYY-MM-DD") from exc
        stmt = stmt.where(
            tuple_(
                InvestmentTransaction.date,
                InvestmentTransaction.plaid_investment_transaction_id,
            )
            < (cursor_date, cursor_id)
        )
    rows = session.execute(stmt.limit(limit + 1)).all()

    overrides = _load_transaction_overrides(session)
    page = rows[:limit]
    out: list[TransactionV1] = []
    for t, a, s in page:
        out.append(
            TransactionV1(
                transaction_id=t.plaid_investment_transaction_id,
                account_id=a.account_id,
                account_name=a.name,
                security_id=s.security_id if s is not None else None,
                ticker=s.ticker if s is not None else None,
                date=t.date,
                name=t.name,
                quantity=t.quantity,
                amount=t.amount,
                price=t.price,
                fees=t.fees,
                type=t.type,
                subtype=t.subtype,
                currency=t.currency,
                override_classification=overrides.get(t.plaid_investment_transaction_id),
                effective_classification=effective_classification(
                    t.type,
                    t.subtype,
                    overrides.get(t.plaid_investment_transaction_id),
                    amount=Decimal(t.amount) if t.amount is not None else None,
                    name=t.name,
                ),
            )
        )
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        next_cursor = _encode_cursor(last.date.isoformat(), last.plaid_investment_transaction_id)
    return TransactionsV1Result(
        meta=meta, start_date=start, end_date=end, transactions=out, next_cursor=next_cursor
    )


# ---------------------------------------------------------------------------
# Cash flows
# ---------------------------------------------------------------------------


class CashFlowV1(BaseModel):
    transaction_id: str
    account_id: int
    account_name: str
    date: date
    name: str | None
    type: str
    subtype: str | None
    amount: Decimal
    # Signed flow INTO the portfolio as the TWR pipeline sees it (positive =
    # money entered). Zero for internal events.
    signed_external_amount: Decimal
    classification: str  # external_in | external_out | internal
    classification_source: str  # override | heuristic
    currency: str


class CashFlowsV1Result(BaseModel):
    meta: V1Meta
    start_date: date
    end_date: date
    include_internal: bool
    cash_flows: list[CashFlowV1]
    net_external_cashflow_in: Decimal
    next_cursor: str | None


def build_cash_flows_page(
    session: Session,
    *,
    start_date: date | None,
    end_date: date | None,
    include_internal: bool,
    limit: int,
    cursor: str | None,
    generated_at: datetime | None = None,
) -> CashFlowsV1Result:
    start, end = _bounded_window(start_date, end_date, TRANSACTIONS_DEFAULT_DAYS)
    accts = active_account_ids(session)
    links = {"transactions": "/api/v1/transactions"}
    meta = _history_meta(
        session,
        as_of=end,
        methodology="cash_flow.twr_classification",
        links=links,
        generated_at=generated_at,
    )
    if not accts:
        return CashFlowsV1Result(
            meta=meta,
            start_date=start,
            end_date=end,
            include_internal=include_internal,
            cash_flows=[],
            net_external_cashflow_in=Decimal(0),
            next_cursor=None,
        )

    def base_stmt(after: tuple[date, str] | None):
        stmt = (
            select(InvestmentTransaction, Account)
            .join(Account, Account.account_id == InvestmentTransaction.account_id)
            .where(InvestmentTransaction.date >= start)
            .where(InvestmentTransaction.date <= end)
            .where(InvestmentTransaction.account_id.in_(accts))
            .order_by(
                InvestmentTransaction.date.desc(),
                InvestmentTransaction.plaid_investment_transaction_id.desc(),
            )
        )
        if after is not None:
            stmt = stmt.where(
                tuple_(
                    InvestmentTransaction.date,
                    InvestmentTransaction.plaid_investment_transaction_id,
                )
                < after
            )
        return stmt

    after: tuple[date, str] | None = None
    if cursor is not None:
        cursor_date_raw, cursor_id = _decode_cursor(cursor, 2)
        try:
            after = (date.fromisoformat(cursor_date_raw), cursor_id)
        except ValueError as exc:
            raise InvalidCursorError("cursor date is not ISO YYYY-MM-DD") from exc

    overrides = _load_transaction_overrides(session)
    # Classification filters rows AFTER the SQL keyset walk, so page through
    # the raw stream batch-by-batch and emit classified rows until the page
    # fills or the stream is exhausted — never a silent cap (PRD §7.5).
    out: list[CashFlowV1] = []
    net_in = Decimal(0)
    next_cursor: str | None = None
    batch_size = MAX_PAGE_SIZE * 4
    while next_cursor is None:
        batch = session.execute(base_stmt(after).limit(batch_size)).all()
        for t, a in batch:
            after = (t.date, t.plaid_investment_transaction_id)
            override = overrides.get(t.plaid_investment_transaction_id)
            cls = effective_classification(
                t.type,
                t.subtype,
                override,
                amount=Decimal(t.amount) if t.amount is not None else None,
                name=t.name,
            )
            if cls is None:
                continue  # not a cashflow-shaped row (buy/sell/fee)
            if cls == "internal" and not include_internal:
                continue
            signed = _signed_cashflow(
                t.type,
                t.subtype,
                Decimal(t.amount or 0),
                override=override,
                name=t.name,
            )
            out.append(
                CashFlowV1(
                    transaction_id=t.plaid_investment_transaction_id,
                    account_id=a.account_id,
                    account_name=a.name,
                    date=t.date,
                    name=t.name,
                    type=t.type,
                    subtype=t.subtype,
                    amount=t.amount if t.amount is not None else Decimal(0),
                    signed_external_amount=signed,
                    classification=cls,
                    classification_source="override" if override is not None else "heuristic",
                    currency=t.currency,
                )
            )
            net_in += signed
            if len(out) >= limit:
                next_cursor = _encode_cursor(after[0].isoformat(), after[1])
                break
        if len(batch) < batch_size:
            break  # stream exhausted
    return CashFlowsV1Result(
        meta=meta,
        start_date=start,
        end_date=end,
        include_internal=include_internal,
        cash_flows=out,
        net_external_cashflow_in=net_in,
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------------------
# Position snapshots
# ---------------------------------------------------------------------------


class PositionSnapshotV1(BaseModel):
    snapshot_date: date
    account_id: int
    account_name: str
    security_id: int
    ticker: str | None
    quantity: Decimal
    institution_price: Decimal | None
    institution_value: Decimal | None
    currency: str
    # 'broker' = provider pass-through; 'manual' = app-synthesized gap fill.
    origin: str


class PositionSnapshotsV1Result(BaseModel):
    meta: V1Meta
    start_date: date
    end_date: date
    snapshots: list[PositionSnapshotV1]
    next_cursor: str | None


def build_position_snapshots_page(
    session: Session,
    *,
    start_date: date | None,
    end_date: date | None,
    limit: int,
    cursor: str | None,
    generated_at: datetime | None = None,
) -> PositionSnapshotsV1Result:
    start, end = _bounded_window(start_date, end_date, SNAPSHOTS_DEFAULT_DAYS)
    accts = active_account_ids(session)
    links = {"positions": "/api/v1/portfolio/positions"}
    meta = _history_meta(
        session,
        as_of=end,
        methodology="position_snapshots.observed",
        links=links,
        generated_at=generated_at,
    )
    if not accts:
        return PositionSnapshotsV1Result(
            meta=meta, start_date=start, end_date=end, snapshots=[], next_cursor=None
        )
    stmt = (
        select(HoldingSnapshot, Account, Security)
        .join(Account, Account.account_id == HoldingSnapshot.account_id)
        .join(Security, Security.security_id == HoldingSnapshot.security_id)
        .where(HoldingSnapshot.snapshot_date >= start)
        .where(HoldingSnapshot.snapshot_date <= end)
        .where(HoldingSnapshot.account_id.in_(accts))
        .order_by(
            HoldingSnapshot.snapshot_date.desc(),
            HoldingSnapshot.account_id.asc(),
            HoldingSnapshot.security_id.asc(),
        )
    )
    if cursor is not None:
        d_raw, acct_raw, sec_raw = _decode_cursor(cursor, 3)
        try:
            cursor_key = (date.fromisoformat(d_raw), int(acct_raw), int(sec_raw))
        except ValueError as exc:
            raise InvalidCursorError("cursor must be date|account_id|security_id") from exc
        # (date desc, account asc, security asc): resume strictly after the key.
        stmt = stmt.where(
            (HoldingSnapshot.snapshot_date < cursor_key[0])
            | (
                (HoldingSnapshot.snapshot_date == cursor_key[0])
                & (
                    tuple_(HoldingSnapshot.account_id, HoldingSnapshot.security_id)
                    > (cursor_key[1], cursor_key[2])
                )
            )
        )
    rows = session.execute(stmt.limit(limit + 1)).all()
    page = rows[:limit]
    out = [
        PositionSnapshotV1(
            snapshot_date=h.snapshot_date,
            account_id=a.account_id,
            account_name=a.name,
            security_id=s.security_id,
            ticker=s.ticker,
            quantity=h.quantity,
            institution_price=h.institution_price,
            institution_value=h.institution_value,
            currency=h.currency,
            origin=h.origin,
        )
        for h, a, s in page
    ]
    next_cursor = None
    if len(rows) > limit:
        last_h = page[-1][0]
        next_cursor = _encode_cursor(
            last_h.snapshot_date.isoformat(), str(last_h.account_id), str(last_h.security_id)
        )
    return PositionSnapshotsV1Result(
        meta=meta, start_date=start, end_date=end, snapshots=out, next_cursor=next_cursor
    )


# ---------------------------------------------------------------------------
# Securities
# ---------------------------------------------------------------------------


class SecurityV1(BaseModel):
    security_id: int
    ticker: str | None
    name: str | None
    cusip: str | None
    isin: str | None
    type: str | None
    currency: str
    is_cash_equivalent: bool
    asset_type: str
    sector: str | None
    region: str | None
    classification_source: str | None  # 'auto' | 'manual' | None
    classification_updated_at: datetime | None


class SecuritiesV1Result(BaseModel):
    meta: V1Meta
    securities: list[SecurityV1]


def build_securities_result(
    session: Session,
    *,
    today: date | None = None,
    generated_at: datetime | None = None,
) -> SecuritiesV1Result:
    """The full security master — a single-user book is small enough to
    return whole (PRD §7.5 allows complete current-state reads)."""
    accounts_meta = build_accounts_result(session, today=today).meta
    meta = build_meta(
        as_of=accounts_meta.as_of,
        source_providers=accounts_meta.source_providers,
        coverage=accounts_meta.account_coverage,
        last_successful_sync_at=accounts_meta.last_successful_sync_at,
        warnings=[],
        links={"positions": "/api/v1/portfolio/positions"},
        methodology="securities.master",
        methodology_version="1",
        today=today,
        generated_at=generated_at,
    )
    classifications = {
        c.security_id: c for c in session.execute(select(SecurityClassification)).scalars().all()
    }
    out: list[SecurityV1] = []
    for sec in session.execute(select(Security).order_by(Security.ticker)).scalars().all():
        c = classifications.get(sec.security_id)
        out.append(
            SecurityV1(
                security_id=sec.security_id,
                ticker=sec.ticker,
                name=sec.name,
                cusip=sec.cusip,
                isin=sec.isin,
                type=sec.type,
                currency=sec.currency,
                is_cash_equivalent=sec.is_cash_equivalent,
                asset_type=classify_asset_type(sec.type, sec.is_cash_equivalent).value,
                sector=c.sector if c else None,
                region=c.region if c else None,
                classification_source=c.source if c else None,
                classification_updated_at=c.updated_at if c else None,
            )
        )
    return SecuritiesV1Result(meta=meta, securities=out)
