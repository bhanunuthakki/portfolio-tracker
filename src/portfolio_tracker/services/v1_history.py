"""Cursor-paginated v1 history resources: transactions, cash flows, position
snapshots, and the securities master.

Pagination (PRD §7.5): stable keyset cursors, never OFFSET and never a silent
row cap. The cursor is an opaque base64 token over the ordering key; consumers
follow ``next_cursor`` until null. Historical endpoints take bounded date
parameters with documented defaults.

Cash-flow classification and performance both consume the canonical derived
ledger in ``services/external_flow_ledger.py``, so this endpoint cannot drift
from the return calculation it explains.
"""

from __future__ import annotations

import base64
import binascii
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    HoldingSnapshot,
    InvestmentTransaction,
    Security,
    SecurityClassification,
)
from portfolio_tracker.schemas import (
    CashFlowDecisionAuthorityOut,
    CashFlowDecisionConfidenceOut,
    CashFlowEffectiveDateBasisOut,
    CashFlowSourceCoverageOut,
    CashFlowTransactionOrigin,
)
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.cashflow_source_coverage import (
    assess_cashflow_source_coverage,
    source_coverage_out,
)
from portfolio_tracker.services.external_flow_ledger import (
    build_external_flow_ledger,
    effective_transaction_classifications,
    load_transaction_overrides,
)
from portfolio_tracker.services.performance import performance_account_ids
from portfolio_tracker.services.positioning import classify_asset_type
from portfolio_tracker.services.v1_accounts import build_accounts_result
from portfolio_tracker.services.v1_common import V1AccountCoverage, V1Meta, build_meta

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
    methodology_version: str = "1",
    included_account_ids: frozenset[int] | None = None,
    generated_at: datetime | None = None,
) -> V1Meta:
    """Envelope for history endpoints — coverage/sync from the accounts
    builder; ``as_of`` is the query window end (event streams have no single
    observation date)."""
    accounts_meta = build_accounts_result(session).meta
    coverage = accounts_meta.account_coverage
    if included_account_ids is not None:
        previously_included = set(coverage.included_account_ids)
        excluded = set(coverage.excluded_account_ids) | (previously_included - included_account_ids)
        coverage = V1AccountCoverage(
            included_account_ids=sorted(included_account_ids),
            excluded_account_ids=sorted(excluded),
            lagging_account_ids=sorted(set(coverage.lagging_account_ids) & included_account_ids),
        )
    return build_meta(
        as_of=as_of,
        source_providers=accounts_meta.source_providers,
        coverage=coverage,
        last_successful_sync_at=accounts_meta.last_successful_sync_at,
        warnings=[],
        links=links,
        methodology=methodology,
        methodology_version=methodology_version,
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
    # User override (None if none) and the classification the return pipeline
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

    overrides = load_transaction_overrides(session)
    page = rows[:limit]
    effective_classifications = effective_transaction_classifications(
        session,
        tuple(t for t, _a, _s in page),
        account_ids=accts,
    )
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
                effective_classification=effective_classifications.get(
                    t.plaid_investment_transaction_id
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
    flow_id: str
    transaction_id: str | None
    component_transaction_ids: list[str]
    account_id: int | None
    account_ids: list[int]
    account_name: str | None
    date: date
    name: str | None
    type: str
    subtype: str | None
    amount: Decimal
    # Signed flow INTO the portfolio as the Modified Dietz pipeline sees it (positive =
    # money entered). Zero for internal events.
    signed_external_amount: Decimal
    classification: str  # external_in | external_out | internal
    classification_source: str  # override | heuristic | derived_share_transfer_net
    classification_rule: str
    source_kind: str  # transaction | share_transfer_valuation
    source_provider: str
    currency: str
    security_id: int | None
    security_ids: list[int]
    ticker: str | None
    valuation_price: Decimal | None
    valuation_price_date: date | None
    valuation_price_source: str | None
    transaction_origin: CashFlowTransactionOrigin = "aggregator_transaction"
    source_event_ids: list[str] = Field(default_factory=list)
    source_attestation_keys: list[str] = Field(default_factory=list)
    active_decision_keys: list[str] = Field(default_factory=list)
    decision_authorities: list[CashFlowDecisionAuthorityOut] = []
    decision_confidences: list[CashFlowDecisionConfidenceOut] = []
    assumption_codes: list[str] = Field(default_factory=list)
    effective_date_bases: list[CashFlowEffectiveDateBasisOut] = []


class CashFlowIssueV1(BaseModel):
    code: str
    date: date
    security_key: str
    component_transaction_ids: list[str]


class CashFlowsV1Result(BaseModel):
    meta: V1Meta
    start_date: date
    end_date: date
    include_internal: bool
    cash_flows: list[CashFlowV1]
    net_external_cashflow_in: Decimal | None
    # Structural validity answers whether stored rows can be classified and
    # valued. Source coverage separately proves authoritative history was
    # reconciled for every valued account and requested date.
    structural_is_complete: bool
    source_coverage: CashFlowSourceCoverageOut
    is_complete: bool
    issues: list[CashFlowIssueV1]
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
    accts = performance_account_ids(session, start, end)
    links = {"transactions": "/api/v1/transactions"}
    meta = _history_meta(
        session,
        as_of=end,
        methodology="cash_flow.twr_classification",
        links=links,
        methodology_version="2",
        included_account_ids=accts,
        generated_at=generated_at,
    )
    source_coverage = assess_cashflow_source_coverage(
        session,
        start,
        end,
        account_ids=accts,
    )
    source_coverage_read = source_coverage_out(source_coverage)
    if not accts:
        return CashFlowsV1Result(
            meta=meta,
            start_date=start,
            end_date=end,
            include_internal=include_internal,
            cash_flows=[],
            net_external_cashflow_in=Decimal(0),
            structural_is_complete=True,
            source_coverage=source_coverage_read,
            is_complete=True,
            issues=[],
            next_cursor=None,
        )

    after: tuple[date, str] | None = None
    if cursor is not None:
        cursor_date_raw, cursor_id = _decode_cursor(cursor, 2)
        try:
            after = (date.fromisoformat(cursor_date_raw), cursor_id)
        except ValueError as exc:
            raise InvalidCursorError("cursor date is not ISO YYYY-MM-DD") from exc

    ledger = build_external_flow_ledger(session, start, end, account_ids=accts)
    eligible = [
        entry for entry in ledger.entries if include_internal or entry.classification != "internal"
    ]
    if after is not None:
        eligible = [entry for entry in eligible if (entry.date, entry.flow_id) < after]
    page = eligible[:limit]
    out = [
        CashFlowV1(
            flow_id=entry.flow_id,
            transaction_id=entry.transaction_id,
            component_transaction_ids=list(entry.component_transaction_ids),
            account_id=entry.account_id,
            account_ids=list(entry.account_ids),
            account_name=entry.account_name,
            date=entry.date,
            name=entry.name,
            type=entry.type,
            subtype=entry.subtype,
            amount=entry.amount,
            signed_external_amount=entry.signed_external_amount,
            classification=entry.classification,
            classification_source=entry.classification_source,
            classification_rule=entry.classification_rule,
            source_kind=entry.source_kind,
            source_provider=entry.source_provider,
            currency=entry.currency,
            security_id=entry.security_id,
            security_ids=list(entry.security_ids),
            ticker=entry.ticker,
            valuation_price=entry.valuation_price,
            valuation_price_date=entry.valuation_price_date,
            valuation_price_source=entry.valuation_price_source,
            transaction_origin=entry.transaction_origin,
            source_event_ids=list(entry.source_event_ids),
            source_attestation_keys=list(entry.source_attestation_keys),
            active_decision_keys=list(entry.active_decision_keys),
            decision_authorities=list(entry.decision_authorities),
            decision_confidences=list(entry.decision_confidences),
            assumption_codes=list(entry.assumption_codes),
            effective_date_bases=list(entry.effective_date_bases),
        )
        for entry in page
    ]
    next_cursor = None
    if len(eligible) > limit:
        last = page[-1]
        next_cursor = _encode_cursor(last.date.isoformat(), last.flow_id)
    structural_is_complete = not ledger.issues
    is_complete = structural_is_complete and source_coverage.is_complete
    return CashFlowsV1Result(
        meta=meta,
        start_date=start,
        end_date=end,
        include_internal=include_internal,
        cash_flows=out,
        net_external_cashflow_in=(ledger.net_external_cashflow_in if is_complete else None),
        structural_is_complete=structural_is_complete,
        source_coverage=source_coverage_read,
        is_complete=is_complete,
        issues=[
            CashFlowIssueV1(
                code=issue.code,
                date=issue.date,
                security_key=issue.security_key,
                component_transaction_ids=list(issue.component_transaction_ids),
            )
            for issue in ledger.issues
        ],
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
