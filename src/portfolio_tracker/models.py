"""SQLAlchemy ORM models — the persisted shape of every domain object.

All financial quantities are `Numeric` (Decimal) to avoid float drift. Dates
are calendar dates (no time zone); timestamps that need wall-clock precision
are `DateTime(timezone=True)`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import ColumnElement


class Base(DeclarativeBase):
    """Common base — all tables inherit timestamp behavior from here when needed."""


class ItemSource(StrEnum):
    """Which aggregator owns this Item's credentials and data path."""

    PLAID = "plaid"
    SNAPTRADE = "snaptrade"


class AccountType(StrEnum):
    """Plaid's `account.type` enum — top-level category."""

    INVESTMENT = "investment"
    DEPOSITORY = "depository"
    CREDIT = "credit"
    LOAN = "loan"
    BROKERAGE = "brokerage"
    OTHER = "other"


# Account types this app cares about. Plaid uses `investment` for retirement
# (IRA / 401k / 529 / HSA) and modern brokerage; `brokerage` is the legacy
# label some older institutions still return. Everything else is filtered out
# at ingest and via the `scrub` job.
INVESTMENT_ACCOUNT_TYPES: frozenset[str] = frozenset(
    {AccountType.INVESTMENT.value, AccountType.BROKERAGE.value}
)


class InvestmentTransactionType(StrEnum):
    """Plaid's `investment_transaction.type` enum."""

    BUY = "buy"
    SELL = "sell"
    CANCEL = "cancel"
    CASH = "cash"
    FEE = "fee"
    TRANSFER = "transfer"


class Item(Base):
    """One row per linked institution connection.

    `source` selects which aggregator owns the credentials and data path:
      * `plaid`     → uses `plaid_item_id` + `plaid_access_token_encrypted`
      * `snaptrade` → uses `snaptrade_user_id` + `snaptrade_user_secret_encrypted`
                       + `snaptrade_authorization_id`

    All credentials encrypted at rest via `crypto.encrypt_token`. Read back
    through `crypto.decrypt_token` — never log the decrypted value.
    """

    __tablename__ = "items"

    item_id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="plaid")

    # Plaid-specific (nullable — populated when source='plaid')
    plaid_item_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    plaid_access_token_encrypted: Mapped[str | None] = mapped_column(String, nullable=True)
    plaid_institution_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # SnapTrade-specific (nullable — populated when source='snaptrade')
    snaptrade_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    snaptrade_user_secret_encrypted: Mapped[str | None] = mapped_column(String, nullable=True)
    snaptrade_authorization_id: Mapped[str | None] = mapped_column(
        String, unique=True, nullable=True
    )

    institution_name: Mapped[str | None] = mapped_column(String, nullable=True)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_transactions_cursor: Mapped[str | None] = mapped_column(String, nullable=True)

    # When False, every aggregation query (holdings, transactions, V series,
    # backfill, data quality, trade analysis) silently skips this item. The
    # connection itself stays linked and the snapshot job still touches it
    # so the access_token doesn't go stale — but the data it pulls doesn't
    # influence any user-facing number. Used to retire a redundant aggregator
    # connection (e.g., Plaid Robinhood when SnapTrade is now the source of
    # truth) without surrendering the Plaid Item slot.
    is_data_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )

    accounts: Mapped[list[Account]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


class Account(Base):
    """One row per account inside an Item (e.g., Roth IRA, Taxable, 401k)."""

    __tablename__ = "accounts"

    account_id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.item_id", ondelete="CASCADE"), nullable=False
    )
    plaid_account_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    official_name: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    subtype: Mapped[str | None] = mapped_column(String, nullable=True)
    mask: Mapped[str | None] = mapped_column(String, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    item: Mapped[Item] = relationship(back_populates="accounts")


class Security(Base):
    """One row per unique security Plaid has reported."""

    __tablename__ = "securities"

    security_id: Mapped[int] = mapped_column(primary_key=True)
    plaid_security_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    ticker: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    cusip: Mapped[str | None] = mapped_column(String, nullable=True)
    isin: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str | None] = mapped_column(String, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    is_cash_equivalent: Mapped[bool] = mapped_column(default=False, nullable=False)


class HoldingSnapshot(Base):
    """A daily snapshot of a single (account, security) position.

    The forward time series of portfolio composition. Written by `jobs.snapshot`
    once per day after market close. Composite primary key prevents duplicate
    rows for the same date/account/security.
    """

    __tablename__ = "holdings_snapshots"
    __table_args__ = (
        Index("ix_holdings_snapshots_date", "snapshot_date"),
        Index("ix_holdings_snapshots_account", "account_id", "snapshot_date"),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="CASCADE"), primary_key=True
    )
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.security_id", ondelete="RESTRICT"), primary_key=True
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    institution_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    institution_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    cost_basis: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    # 'broker' = pass-through of Plaid/SnapTrade snapshot; 'manual' = synthesized
    # by the app to smooth over a known data gap. See migration 0009.
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="broker", server_default="broker"
    )


class InvestmentTransaction(Base):
    """A single buy / sell / dividend / transfer event from Plaid.

    Plaid retains up to 24 months. Used to backfill the time series in concert
    with `prices`. `plaid_investment_transaction_id` is the natural primary key
    so re-pulling never duplicates.
    """

    __tablename__ = "investment_transactions"
    __table_args__ = (
        Index("ix_inv_tx_account_date", "account_id", "date"),
        Index("ix_inv_tx_date", "date"),
    )

    plaid_investment_transaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="CASCADE"), nullable=False
    )
    security_id: Mapped[int | None] = mapped_column(
        ForeignKey("securities.security_id", ondelete="RESTRICT"), nullable=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False, default=Decimal(0))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    fees: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    subtype: Mapped[str | None] = mapped_column(String, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    # 'broker' = pass-through of Plaid/SnapTrade tx; 'manual' = synthesized by
    # the app (typically ACATS in/out matching pair). See migration 0009.
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="broker", server_default="broker"
    )


class PriceSource(StrEnum):
    """Provider that supplied a persisted daily security price."""

    UNKNOWN = "unknown"
    YFINANCE = "yfinance"
    STOOQ = "stooq"


class PriceAdjustmentBasis(StrEnum):
    """Corporate-action and distribution adjustment applied to a price."""

    UNKNOWN = "unknown"
    RAW_UNADJUSTED = "raw_unadjusted"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN_ADJUSTED = "total_return_adjusted"


class Price(Base):
    """Daily close price for a security with durable provenance.

    Used to value past positions when reconstructing portfolio history from
    transactions. `source` names the provider and `adjustment_basis` records
    the price-series semantics. Legacy/unverified rows use `unknown`; consumers
    must not infer provenance from the mere presence of a row. Composite PK on
    (security_id, date).
    """

    __tablename__ = "prices"
    __table_args__ = (
        CheckConstraint(
            "source IN ('unknown', 'yfinance', 'stooq')",
            name="ck_prices_source",
        ),
        CheckConstraint(
            "adjustment_basis IN "
            "('unknown', 'raw_unadjusted', 'split_adjusted', 'total_return_adjusted')",
            name="ck_prices_adjustment_basis",
        ),
    )

    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.security_id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=PriceSource.UNKNOWN.value,
        server_default=PriceSource.UNKNOWN.value,
    )
    adjustment_basis: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=PriceAdjustmentBasis.UNKNOWN.value,
        server_default=PriceAdjustmentBasis.UNKNOWN.value,
    )

    @property
    def is_position_price_trade_eligible(self) -> bool:
        """Whether this row can compare a position close with as-traded prices."""
        return (
            self.source == PriceSource.YFINANCE.value
            and self.adjustment_basis == PriceAdjustmentBasis.SPLIT_ADJUSTED.value
            and self.close > 0
        )

    @classmethod
    def position_price_trade_eligibility_clause(cls) -> ColumnElement[bool]:
        """SQL predicate matching rows eligible for price/trade comparisons."""
        return and_(
            cls.source == PriceSource.YFINANCE.value,
            cls.adjustment_basis == PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            cls.close > 0,
        )


class StockSplit(Base):
    """A corporate stock-split event for a security.

    `ratio` is the yfinance convention: shares-after / shares-before for one
    pre-split share — 4.0 for a 4:1 forward split, 0.125 for a 1:8 reverse.
    Historical transaction/snapshot QUANTITIES are recorded in as-traded
    units. Position-return consumers use these events to normalize quantities
    into current split units before comparing them with yfinance's
    split-adjusted Close — see `services/splits.py`. Refreshed by `jobs.splits`
    from yfinance `.splits`. Composite PK so a re-fetch upserts.
    """

    __tablename__ = "stock_splits"

    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.security_id", ondelete="CASCADE"), primary_key=True
    )
    split_date: Mapped[date] = mapped_column(Date, primary_key=True)
    ratio: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)


class Benchmark(Base):
    """Daily marks for a benchmark index (SPY, QQQ, etc.).

    `close` is the raw price close (split-adjusted, dividends NOT reinvested)
    — kept for display and data-quality coverage. `total_return_close` is the
    dividend-reinvested adjusted close (yfinance `Adj Close`); it's the series
    return/counterfactual math should use so the comparison is total-return
    on both sides. A buy-and-hold of SPY earns its ~1.3 %/yr dividend, so a
    price-only counterfactual understates the benchmark and over-credits the
    user's alpha. `total_return_close` is nullable for rows written before
    migration 0021 — consumers `coalesce` to `close` until a re-fetch
    (`python -m portfolio_tracker.jobs.benchmarks --start <earliest>`)
    backfills it.
    """

    __tablename__ = "benchmarks"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    total_return_close: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)


class PortfolioValueDaily(Base):
    """Cached daily totals derived from holdings_snapshots.

    Recomputed by the performance service whenever snapshots change. Storing
    the derivation lets the chart endpoint return in O(days) rather than
    rescanning every snapshot row.
    """

    __tablename__ = "portfolio_values_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    total_value: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    total_cost_basis: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # "snapshot" | "backfill"


class SnapTradeUser(Base):
    """SnapTrade-registered user, one row per profile.

    SnapTrade's auth model is per-user: a user_id maps to a user_secret, and
    the same user can hold many brokerage authorizations (Fidelity + Schwab +
    ...). We persist the secret here so it survives across the open-portal /
    return / sync flow — losing the secret means the SnapTrade user is
    unreachable from our backend (re-registering returns the same secret in
    the SDK, but only if the user was deleted first).

    `user_secret_encrypted` is stored encrypted via `crypto.encrypt_token`.
    """

    __tablename__ = "snaptrade_users"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_secret_encrypted: Mapped[str] = mapped_column(String, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CostBasisOverride(Base):
    """Authoritative cost basis for an (account, security) pair.

    Used when broker-reported cost_basis is missing, $0 from an ACATS
    transfer, or otherwise wrong. The consolidated-holdings endpoint
    prefers this row when present — see `source` for who set it.

    `total_cost_basis` is the TOTAL dollars paid (price × shares + fees),
    matching Plaid's convention.

    `source` distinguishes who/what set the override:
      * `manual`          — user-entered via API/UI
      * `inferred_acats`  — derived from source-account buy history at
                            ACATS transfer date (see infer_acats_cost_basis.py)
      * `inferred_1099`   — reserved for future 1099-B based inference
    The marker is on the row itself (not on a separate registry) so any
    downstream consumer can filter by origin without consulting other tables.

    `acquired_at` is the date the user (or ACATS source broker) actually
    bought the shares. Used as the anchor for synthetic SPY buys in the
    Trade Timeline and as the pre-window quantity anchor in Position
    Alpha for shares acquired before the system started tracking them.
    NULL means "unknown — fall back to earliest snapshot date as proxy",
    which preserves the prior behavior.
    """

    __tablename__ = "cost_basis_overrides"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="CASCADE"), primary_key=True
    )
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.security_id", ondelete="CASCADE"), primary_key=True
    )
    total_cost_basis: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="manual",
        server_default="manual",
    )
    acquired_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PolicyWeight(Base):
    """User's policy-portfolio target weight for a ticker.

    The "policy benchmark" is a synthetic portfolio that matches the user's
    intended asset allocation rather than a single-index proxy. It's the
    right comparison for someone who is intentionally diversifying (e.g.,
    international + treasuries to dilute concentrated employer stock) —
    "did I beat my own policy?" is more meaningful than "did I beat SPY?"

    Weights are stored in basis points (1 bp = 0.01%), so 6000 = 60.00%.
    Integer math avoids float-comparison issues; UI displays as percent.
    """

    __tablename__ = "policy_weights"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    weight_bps: Mapped[int] = mapped_column(nullable=False)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PolicyState(Base):
    """Singleton revision and benchmark-invalidation state for policy weights."""

    __tablename__ = "policy_state"
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_policy_state_singleton"),
        CheckConstraint("revision >= 0", name="ck_policy_state_revision_nonnegative"),
        CheckConstraint(
            "benchmark_status IN ('current', 'required')",
            name="ck_policy_state_benchmark_status",
        ),
    )

    singleton_id: Mapped[int] = mapped_column(primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    benchmark_status: Mapped[str] = mapped_column(String(16), nullable=False)
    benchmark_invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PolicyWriteReceipt(Base):
    """Durable idempotency and audit receipt for a policy replacement."""

    __tablename__ = "policy_write_receipts"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_policy_write_receipts_key"),
        CheckConstraint(
            "outcome IN ('applied', 'unchanged')",
            name="ck_policy_write_receipts_outcome",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_revision: Mapped[int] = mapped_column(nullable=False)
    accepted_revision: Mapped[int] = mapped_column(nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TransactionOverride(Base):
    """User-supplied classification override for a single investment_transactions row.

    The TWR / contributions pipeline classifies each tx as external-in,
    external-out, or internal based on (type, subtype) heuristics. Brokers
    are inconsistent — an ACATS transfer may look like a contribution to one
    feed and an internal move to another, causing contribution totals to
    swing across windows. A row here forces the pipeline to use the user's
    classification instead.

    Valid `classification` values (enforced by CHECK constraint):
      * `external_in`   — contribution / deposit
      * `external_out`  — withdrawal / distribution
      * `internal`      — internal move; excluded from cashflow
    """

    __tablename__ = "transaction_overrides"

    plaid_investment_transaction_id: Mapped[str] = mapped_column(
        ForeignKey(
            "investment_transactions.plaid_investment_transaction_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CashFlowSourceType(StrEnum):
    """Authoritative evidence used to reconcile an account's flow history."""

    BROKERAGE_STATEMENT = "brokerage_statement"
    PROVIDER_EXPORT = "provider_export"
    OWNER_RECONCILIATION = "owner_reconciliation"


class CashFlowSourceGapReason(StrEnum):
    """Why a source attestation does not cover part of its declared range."""

    PROVIDER_HISTORY_UNAVAILABLE = "provider_history_unavailable"
    STATEMENT_MISSING = "statement_missing"
    UNRESOLVED_CLASSIFICATION = "unresolved_classification"
    UNRECONCILED_DIFFERENCE = "unreconciled_difference"


class CashFlowAccountMappingBasis(StrEnum):
    """Evidence used to associate a private source with a normalized account."""

    PROVIDER_ACCOUNT_ID = "provider_account_id"
    STATEMENT_ACCOUNT_IDENTIFIER = "statement_account_identifier"
    OWNER_CONFIRMED = "owner_confirmed"


class CashFlowEvidenceConfidence(StrEnum):
    """Review confidence retained with account mappings and flow decisions."""

    EXACT = "exact"
    HIGH = "high"
    PROVISIONAL = "provisional"


class CashFlowSourceLocatorKind(StrEnum):
    """Stable locator shape for one immutable Source event."""

    ROW = "row"
    PAGE_LINE = "page_line"
    PROVIDER_RECORD = "provider_record"


class CashFlowSourceAmountSignBasis(StrEnum):
    """Meaning of the amount sign as observed in a Source event."""

    STATEMENT_PRINTED = "statement_printed"
    PROVIDER_REPORTED = "provider_reported"
    NORMALIZED_EXTERNAL = "normalized_external"


class CashFlowResolutionKind(StrEnum):
    """How a Source event resolves into the normalized transaction authority."""

    PROVIDER_EXACT = "provider_exact"
    STATEMENT_SUPPLEMENT = "statement_supplement"
    INTERNAL = "internal"
    EXCLUDED = "excluded"
    UNRESOLVED = "unresolved"
    PROVIDER_SUPERSEDES_SUPPLEMENT = "provider_supersedes_supplement"


class CashFlowEffectiveDateBasis(StrEnum):
    """Source date selected for end-of-day Modified-Dietz weighting."""

    SOURCE_ACTIVITY = "source_activity"
    SOURCE_PROCESS = "source_process"
    SOURCE_SETTLEMENT = "source_settlement"
    PROVIDER_POSTING = "provider_posting"
    OWNER_RESOLVED = "owner_resolved"


class CashFlowDecisionAuthority(StrEnum):
    """Authority responsible for a Reconciliation decision."""

    PROVIDER = "provider"
    BROKERAGE_STATEMENT = "brokerage_statement"
    OWNER_APPROVED = "owner_approved"


class CashFlowReconciliationRunStatus(StrEnum):
    """Durable lifecycle of one approved reconciliation plan."""

    PREVIEWED = "previewed"
    APPLIED = "applied"


class CashFlowReconciliationRunDecisionKind(StrEnum):
    """How a decision participated in one reconciliation run."""

    CREATED = "created"
    SUPERSEDED = "superseded"
    VERIFIED = "verified"


class CashFlowReconciliationTransactionMutationKind(StrEnum):
    """Normalized transaction-side mutation performed by a reconciliation run."""

    TRANSACTION_INSERT = "transaction_insert"
    OVERRIDE_INSERT = "override_insert"
    OVERRIDE_UPDATE = "override_update"


class CashFlowSourceAttestation(Base):
    """Owner-approved evidence that reconciles one account over a date range.

    Coverage dates are inclusive source-history dates. Performance uses an
    end-of-day opening value, so a requested ``[start, end]`` return requires
    attested flow-source coverage over ``[start + 1 day, end]``.

    ``source_reference`` identifies the private evidence without storing it;
    ``source_sha256`` makes later replacement detectable. A row counts only
    after approval and only while it has not been superseded. Corrections are
    append-only: insert the replacement, then link the old row to it.
    """

    __tablename__ = "cashflow_source_attestations"
    __table_args__ = (
        CheckConstraint(
            "coverage_start <= coverage_end",
            name="ck_cashflow_source_attestations_date_order",
        ),
        CheckConstraint(
            "source_type IN ('brokerage_statement', 'provider_export', 'owner_reconciliation')",
            name="ck_cashflow_source_attestations_source_type",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_cashflow_source_attestations_sha256_length",
        ),
        CheckConstraint(
            "approved_at IS NULL OR approved_at >= captured_at",
            name="ck_cashflow_source_attestations_approval_order",
        ),
        CheckConstraint(
            "(superseded_at IS NULL AND superseded_by_attestation_id IS NULL) OR "
            "(superseded_at IS NOT NULL AND superseded_by_attestation_id IS NOT NULL)",
            name="ck_cashflow_source_attestations_supersession_pair",
        ),
        CheckConstraint(
            "account_identity_sha256 IS NULL OR length(account_identity_sha256) = 64",
            name="ck_cashflow_source_attestations_account_sha256_length",
        ),
        CheckConstraint(
            "account_mapping_basis IS NULL OR account_mapping_basis IN "
            "('provider_account_id', 'statement_account_identifier', 'owner_confirmed')",
            name="ck_cashflow_source_attestations_mapping_basis",
        ),
        CheckConstraint(
            "account_mapping_confidence IS NULL OR account_mapping_confidence IN "
            "('exact', 'high', 'provisional')",
            name="ck_cashflow_source_attestations_mapping_confidence",
        ),
        CheckConstraint(
            "source_row_count IS NULL OR source_row_count >= 0",
            name="ck_cashflow_source_attestations_source_row_count",
        ),
        CheckConstraint(
            "cashflow_candidate_count IS NULL OR cashflow_candidate_count >= 0",
            name="ck_cashflow_source_attestations_candidate_count",
        ),
        CheckConstraint(
            "source_row_count IS NULL OR cashflow_candidate_count IS NULL OR "
            "cashflow_candidate_count <= source_row_count",
            name="ck_cashflow_source_attestations_candidate_within_rows",
        ),
        CheckConstraint(
            "source_event_set_sha256 IS NULL OR length(source_event_set_sha256) = 64",
            name="ck_cashflow_source_attestations_event_set_sha256_length",
        ),
        CheckConstraint(
            "manifest_sha256 IS NULL OR length(manifest_sha256) = 64",
            name="ck_cashflow_source_attestations_manifest_sha256_length",
        ),
        CheckConstraint(
            "(account_identity_sha256 IS NULL AND account_mapping_basis IS NULL AND "
            "account_mapping_confidence IS NULL AND source_format IS NULL AND "
            "parser_version IS NULL AND source_timezone IS NULL AND source_row_count IS NULL AND "
            "cashflow_candidate_count IS NULL AND source_event_set_sha256 IS NULL AND "
            "manifest_sha256 IS NULL) OR "
            "(account_identity_sha256 IS NOT NULL AND account_mapping_basis IS NOT NULL AND "
            "account_mapping_confidence IS NOT NULL AND source_format IS NOT NULL AND "
            "parser_version IS NOT NULL AND source_timezone IS NOT NULL AND "
            "source_row_count IS NOT NULL AND cashflow_candidate_count IS NOT NULL AND "
            "source_event_set_sha256 IS NOT NULL AND manifest_sha256 IS NOT NULL)",
            name="ck_cashflow_source_attestations_provenance_bundle",
        ),
        Index(
            "ix_cashflow_source_attestations_account_dates",
            "account_id",
            "coverage_start",
            "coverage_end",
        ),
    )

    attestation_id: Mapped[int] = mapped_column(primary_key=True)
    attestation_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False
    )
    coverage_start: Mapped[date] = mapped_column(Date, nullable=False)
    coverage_end: Mapped[date] = mapped_column(Date, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    methodology_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    # Nullable as one coherent bundle so migration-0025 rows remain readable but
    # later certification logic can fail closed until exact event provenance is
    # imported from the owner-approved private source.
    account_identity_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_mapping_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_mapping_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_format: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cashflow_candidate_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_event_set_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_attestation_id: Mapped[int | None] = mapped_column(
        ForeignKey("cashflow_source_attestations.attestation_id", ondelete="RESTRICT"),
        nullable=True,
    )


class CashFlowSourceGap(Base):
    """An explicitly unresolved interval inside one source attestation."""

    __tablename__ = "cashflow_source_gaps"
    __table_args__ = (
        CheckConstraint(
            "gap_start <= gap_end",
            name="ck_cashflow_source_gaps_date_order",
        ),
        CheckConstraint(
            "reason_code IN "
            "('provider_history_unavailable', 'statement_missing', "
            "'unresolved_classification', 'unreconciled_difference')",
            name="ck_cashflow_source_gaps_reason_code",
        ),
        Index("ix_cashflow_source_gaps_attestation", "attestation_id"),
    )

    gap_id: Mapped[int] = mapped_column(primary_key=True)
    attestation_id: Mapped[int] = mapped_column(
        ForeignKey("cashflow_source_attestations.attestation_id", ondelete="CASCADE"),
        nullable=False,
    )
    gap_start: Mapped[date] = mapped_column(Date, nullable=False)
    gap_end: Mapped[date] = mapped_column(Date, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(48), nullable=False)


class CashFlowSourceEvent(Base):
    """One immutable Source event from an attested document or provider record."""

    __tablename__ = "cashflow_source_events"
    __table_args__ = (
        CheckConstraint(
            "length(source_event_id) = 64",
            name="ck_cashflow_source_events_id_length",
        ),
        CheckConstraint(
            "length(source_row_sha256) = 64",
            name="ck_cashflow_source_events_row_sha256_length",
        ),
        CheckConstraint(
            "source_locator_kind IN ('row', 'page_line', 'provider_record')",
            name="ck_cashflow_source_events_locator_kind",
        ),
        CheckConstraint(
            "source_amount_sign_basis IN "
            "('statement_printed', 'provider_reported', 'normalized_external')",
            name="ck_cashflow_source_events_amount_sign_basis",
        ),
        CheckConstraint(
            "activity_date IS NOT NULL OR process_date IS NOT NULL OR settlement_date IS NOT NULL",
            name="ck_cashflow_source_events_has_date",
        ),
        CheckConstraint(
            "(source_locator_kind = 'row' AND source_row_ordinal IS NOT NULL AND "
            "source_page IS NULL AND source_line IS NULL) OR "
            "(source_locator_kind = 'page_line' AND source_row_ordinal IS NULL AND "
            "source_page IS NOT NULL AND source_line IS NOT NULL) OR "
            "(source_locator_kind = 'provider_record' AND source_row_ordinal IS NULL AND "
            "source_page IS NULL AND source_line IS NULL AND source_record_id IS NOT NULL)",
            name="ck_cashflow_source_events_locator_shape",
        ),
        CheckConstraint(
            "source_row_ordinal IS NULL OR source_row_ordinal > 0",
            name="ck_cashflow_source_events_row_ordinal_positive",
        ),
        CheckConstraint(
            "source_page IS NULL OR source_page > 0",
            name="ck_cashflow_source_events_page_positive",
        ),
        CheckConstraint(
            "source_line IS NULL OR source_line > 0",
            name="ck_cashflow_source_events_line_positive",
        ),
        UniqueConstraint(
            "attestation_id",
            "source_locator_kind",
            "source_locator",
            name="uq_cashflow_source_events_attestation_locator",
        ),
        Index("ix_cashflow_source_events_attestation", "attestation_id"),
        Index("ix_cashflow_source_events_activity_date", "activity_date"),
    )

    source_event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    attestation_id: Mapped[int] = mapped_column(
        ForeignKey("cashflow_source_attestations.attestation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_record_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_locator_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_locator: Mapped[str] = mapped_column(String(512), nullable=False)
    source_row_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_row_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    activity_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    process_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    settlement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_amount: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    source_amount_sign_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CashFlowReconciliationDecision(Base):
    """Append-only Reconciliation decision for one immutable Source event."""

    __tablename__ = "cashflow_reconciliation_decisions"
    __table_args__ = (
        CheckConstraint(
            "length(decision_key) = 64",
            name="ck_cashflow_reconciliation_decisions_key_length",
        ),
        CheckConstraint(
            "length(decision_payload_sha256) = 64",
            name="ck_cashflow_reconciliation_decisions_payload_sha256_length",
        ),
        CheckConstraint(
            "resolution_kind IN ('provider_exact', 'statement_supplement', 'internal', "
            "'excluded', 'unresolved', 'provider_supersedes_supplement')",
            name="ck_cashflow_reconciliation_decisions_resolution_kind",
        ),
        CheckConstraint(
            "classification IS NULL OR classification IN "
            "('external_in', 'external_out', 'internal', 'excluded')",
            name="ck_cashflow_reconciliation_decisions_classification",
        ),
        CheckConstraint(
            "decision_authority IN ('provider', 'brokerage_statement', 'owner_approved')",
            name="ck_cashflow_reconciliation_decisions_authority",
        ),
        CheckConstraint(
            "confidence IN ('exact', 'high', 'provisional')",
            name="ck_cashflow_reconciliation_decisions_confidence",
        ),
        CheckConstraint(
            "effective_date_basis IS NULL OR effective_date_basis IN "
            "('source_activity', 'source_process', 'source_settlement', "
            "'provider_posting', 'owner_resolved')",
            name="ck_cashflow_reconciliation_decisions_date_basis",
        ),
        CheckConstraint(
            "(resolution_kind = 'unresolved' AND classification IS NULL AND "
            "signed_external_amount IS NULL AND effective_date IS NULL AND "
            "effective_date_basis IS NULL AND effective_timezone IS NULL AND "
            "target_transaction_id IS NULL) OR "
            "(resolution_kind != 'unresolved' AND classification IS NOT NULL AND "
            "signed_external_amount IS NOT NULL AND effective_date IS NOT NULL AND "
            "effective_date_basis IS NOT NULL AND effective_timezone IS NOT NULL)",
            name="ck_cashflow_reconciliation_decisions_resolution_fields",
        ),
        CheckConstraint(
            "(classification = 'external_in' AND signed_external_amount > 0) OR "
            "(classification = 'external_out' AND signed_external_amount < 0) OR "
            "(classification IN ('internal', 'excluded') AND signed_external_amount = 0) OR "
            "(classification IS NULL AND signed_external_amount IS NULL)",
            name="ck_cashflow_reconciliation_decisions_amount_direction",
        ),
        CheckConstraint(
            "(resolution_kind IN ('provider_exact', 'statement_supplement', "
            "'provider_supersedes_supplement') AND "
            "classification IN ('external_in', 'external_out') AND "
            "target_transaction_id IS NOT NULL) OR "
            "(resolution_kind = 'internal' AND classification = 'internal' AND "
            "signed_external_amount = 0) OR "
            "(resolution_kind = 'excluded' AND classification = 'excluded' AND "
            "signed_external_amount = 0) OR "
            "(resolution_kind = 'unresolved' AND classification IS NULL AND "
            "signed_external_amount IS NULL AND target_transaction_id IS NULL)",
            name="ck_cashflow_reconciliation_decisions_resolution_semantics",
        ),
        CheckConstraint(
            "(superseded_at IS NULL AND superseded_by_decision_key IS NULL) OR "
            "(superseded_at IS NOT NULL AND superseded_by_decision_key IS NOT NULL)",
            name="ck_cashflow_reconciliation_decisions_supersession_pair",
        ),
        CheckConstraint(
            "superseded_by_decision_key IS NULL OR superseded_by_decision_key != decision_key",
            name="ck_cashflow_reconciliation_decisions_no_self_supersession",
        ),
        UniqueConstraint(
            "source_event_id",
            "decision_key",
            name="uq_cashflow_reconciliation_decisions_event_key",
        ),
        ForeignKeyConstraint(
            ["source_event_id", "superseded_by_decision_key"],
            [
                "cashflow_reconciliation_decisions.source_event_id",
                "cashflow_reconciliation_decisions.decision_key",
            ],
            name="fk_cashflow_reconciliation_decisions_same_event_successor",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "uq_cashflow_reconciliation_decisions_current_event",
            "source_event_id",
            unique=True,
            sqlite_where=text("superseded_at IS NULL"),
        ),
        Index("ix_cashflow_reconciliation_decisions_target", "target_transaction_id"),
    )

    decision_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_event_id: Mapped[str] = mapped_column(
        ForeignKey("cashflow_source_events.source_event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "investment_transactions.plaid_investment_transaction_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    resolution_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True)
    signed_external_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_date_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)
    effective_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_authority: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    assumption_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    methodology_version: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_decision_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class CashFlowReconciliationRun(Base):
    """Durable receipt for one previewed or applied reconciliation plan."""

    __tablename__ = "cashflow_reconciliation_runs"
    __table_args__ = (
        CheckConstraint(
            "length(run_id) = 64",
            name="ck_cashflow_reconciliation_runs_id_length",
        ),
        CheckConstraint(
            "length(plan_digest) = 64",
            name="ck_cashflow_reconciliation_runs_plan_digest_length",
        ),
        CheckConstraint(
            "length(manifest_set_sha256) = 64",
            name="ck_cashflow_reconciliation_runs_manifest_sha256_length",
        ),
        CheckConstraint(
            "affected_start <= affected_end",
            name="ck_cashflow_reconciliation_runs_date_order",
        ),
        CheckConstraint(
            "affected_account_count >= 0 AND source_event_count >= 0 AND "
            "planned_mutation_count >= 0 AND applied_mutation_count >= 0 AND "
            "applied_mutation_count <= planned_mutation_count",
            name="ck_cashflow_reconciliation_runs_counts",
        ),
        CheckConstraint(
            "status IN ('previewed', 'applied')",
            name="ck_cashflow_reconciliation_runs_status",
        ),
        CheckConstraint(
            "(status = 'previewed' AND applied_at IS NULL AND applied_mutation_count = 0) OR "
            "(status = 'applied' AND approved_at IS NOT NULL AND applied_at IS NOT NULL AND "
            "applied_at >= approved_at AND applied_mutation_count = planned_mutation_count)",
            name="ck_cashflow_reconciliation_runs_status_fields",
        ),
        Index("ix_cashflow_reconciliation_runs_status_created", "status", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    manifest_set_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    software_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    backup_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    preview_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    affected_start: Mapped[date] = mapped_column(Date, nullable=False)
    affected_end: Mapped[date] = mapped_column(Date, nullable=False)
    affected_account_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    planned_mutation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_mutation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CashFlowReconciliationRunDecision(Base):
    """Append-only membership of one decision in a reconciliation run receipt."""

    __tablename__ = "cashflow_reconciliation_run_decisions"
    __table_args__ = (
        CheckConstraint(
            "membership_kind IN ('created', 'superseded', 'verified')",
            name="ck_cashflow_reconciliation_run_decisions_membership_kind",
        ),
        Index(
            "ix_cashflow_reconciliation_run_decisions_decision",
            "decision_key",
        ),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("cashflow_reconciliation_runs.run_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    decision_key: Mapped[str] = mapped_column(
        ForeignKey("cashflow_reconciliation_decisions.decision_key", ondelete="RESTRICT"),
        primary_key=True,
    )
    membership_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CashFlowReconciliationRunTransactionMutation(Base):
    """Append-only before/after receipt for one transaction-side mutation."""

    __tablename__ = "cashflow_reconciliation_run_transaction_mutations"
    __table_args__ = (
        CheckConstraint(
            "mutation_kind IN ('transaction_insert', 'override_insert', 'override_update')",
            name="ck_cashflow_reconciliation_run_tx_mutations_kind",
        ),
        CheckConstraint(
            "before_payload_sha256 IS NULL OR length(before_payload_sha256) = 64",
            name="ck_cashflow_reconciliation_run_tx_mutations_before_sha256_length",
        ),
        CheckConstraint(
            "length(after_payload_sha256) = 64",
            name="ck_cashflow_reconciliation_run_tx_mutations_after_sha256_length",
        ),
        CheckConstraint(
            "(mutation_kind IN ('transaction_insert', 'override_insert') AND "
            "before_payload_sha256 IS NULL) OR "
            "(mutation_kind = 'override_update' AND before_payload_sha256 IS NOT NULL)",
            name="ck_cashflow_reconciliation_run_tx_mutations_payload_shape",
        ),
        Index(
            "ix_cashflow_reconciliation_run_tx_mutations_target",
            "target_transaction_id",
        ),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("cashflow_reconciliation_runs.run_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    target_transaction_id: Mapped[str] = mapped_column(
        ForeignKey(
            "investment_transactions.plaid_investment_transaction_id",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    mutation_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    before_payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TickerOverride(Base):
    """User-supplied ticker symbol for a security Plaid returned without one.

    Some securities (mutual funds, foreign listings) come back from Plaid
    with no `ticker_symbol`. Without a ticker, the historical-price job
    can't fetch close prices from yfinance, breaking the backfill curve
    for that position. This override lets the user supply a yfinance-
    compatible symbol so backfill works going forward.
    """

    __tablename__ = "ticker_overrides"

    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.security_id", ondelete="CASCADE"), primary_key=True
    )
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SecurityClassification(Base):
    """Sector + coarse region for a security — backs the Holdings positioning view.

    Two cuts the broker feeds don't supply: a GICS-style `sector` and a
    coarse `region` (`US` / `International`). `jobs.classify_securities`
    fills these from yfinance `.info` for individual equities and writes
    `source='auto'`. Funds/ETFs have no single sector, so the job stamps
    `sector='ETF/Fund'` and leaves `region` NULL (the user can override a
    broad-international fund like VXUS). Crypto gets `sector='Crypto'`.

    `source` mirrors `CostBasisOverride`'s convention so a later enrichment
    run never clobbers a human edit:
      * `auto`   — written by the enrichment job from yfinance
      * `manual` — user-entered via the API/UI; the job skips these rows

    One row per security. `sector` / `region` are independently nullable so
    a partial classification (sector known, region unknown) is representable.
    The positioning service treats a missing row — or a NULL field — as the
    explicit "Unclassified" / "Unknown" bucket rather than dropping the
    position, so every dollar still lands in some slice.
    """

    __tablename__ = "security_classifications"

    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.security_id", ondelete="CASCADE"), primary_key=True
    )
    sector: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="auto", server_default="auto"
    )
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TaxFormImport(Base):
    """One row per imported 1099 PDF. Parent record for all augmentative tax
    data. Owner attribution is via `recipient_name`, so a second household
    member's 1099s fit the same schema without any change."""

    __tablename__ = "tax_form_imports"
    __table_args__ = (Index("ix_tax_form_imports_broker_year", "broker", "tax_year"),)

    import_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    tax_year: Mapped[int] = mapped_column(Integer, nullable=False)
    account_mask: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="SET NULL"), nullable=True
    )
    form_types: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    document_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    statement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    recipient_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TaxFormRealizedLot(Base):
    """1099-B sales detail: one row per closed tax lot reported by the broker.
    Contains acquired/disposed dates, qty, proceeds, cost basis, wash sale
    disallowed, gain/loss, term. Authoritative for tax records."""

    __tablename__ = "tax_form_realized_lots"
    __table_args__ = (
        Index("ix_tax_form_realized_lots_import_id", "import_id"),
        Index("ix_tax_form_realized_lots_security_id", "security_id"),
    )

    lot_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("tax_form_imports.import_id", ondelete="CASCADE"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cusip: Mapped[str | None] = mapped_column(String(16), nullable=True)
    security_id: Mapped[int | None] = mapped_column(
        ForeignKey("securities.security_id", ondelete="SET NULL"), nullable=True
    )
    acquired_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    acquired_various: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    disposed_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    proceeds: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    cost_basis: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    wash_sale_loss_disallowed: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    net_gain_loss: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    # 'short' | 'long' | 'undetermined'
    term: Mapped[str] = mapped_column(String(16), nullable=False)
    # 'A' | 'B' | 'C' | 'D' | 'E' | 'F' per Form 8949
    form_8949_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # 'N' (net of option premium) | 'G' (gross)
    proceeds_net_or_gross: Mapped[str | None] = mapped_column(String(8), nullable=True)
    additional_info: Mapped[str | None] = mapped_column(String(256), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class TaxFormDividend(Base):
    """1099-DIV payment-level detail. Includes qualified/nonqualified
    dividends, foreign tax paid, Section 199A dividends, etc."""

    __tablename__ = "tax_form_dividends"
    __table_args__ = (Index("ix_tax_form_dividends_import_id", "import_id"),)

    div_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("tax_form_imports.import_id", ondelete="CASCADE"), nullable=False
    )
    security_description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cusip: Mapped[str | None] = mapped_column(String(16), nullable=True)
    security_id: Mapped[int | None] = mapped_column(
        ForeignKey("securities.security_id", ondelete="SET NULL"), nullable=True
    )
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(64), nullable=False)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class TaxFormInterest(Base):
    """1099-INT detail: interest payments, security lending income, etc."""

    __tablename__ = "tax_form_interest"
    __table_args__ = (Index("ix_tax_form_interest_import_id", "import_id"),)

    int_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("tax_form_imports.import_id", ondelete="CASCADE"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    transaction_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="Interest", server_default="Interest"
    )


class TaxFormRetirementDistribution(Base):
    """1099-R: retirement distributions, rollovers, conversions."""

    __tablename__ = "tax_form_retirement_distributions"
    __table_args__ = (Index("ix_tax_form_retirement_distributions_import_id", "import_id"),)

    dist_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("tax_form_imports.import_id", ondelete="CASCADE"), nullable=False
    )
    gross_distribution: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    taxable_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    federal_tax_withheld: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    state_tax_withheld: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    distribution_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    payer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
