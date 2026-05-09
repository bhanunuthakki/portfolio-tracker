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
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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


class Price(Base):
    """Daily close price for a security (yfinance backfill).

    Used to value past positions when reconstructing portfolio history from
    transactions. Composite PK on (security_id, date).
    """

    __tablename__ = "prices"

    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.security_id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)


class Benchmark(Base):
    """Daily close for a benchmark index (SPY, QQQ, etc.)."""

    __tablename__ = "benchmarks"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)


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
    """User-supplied cost basis for an (account, security) pair.

    Plaid returns `cost_basis` for some institutions but not all (notably
    SoFi via Plaid). When the broker doesn't supply it, derived metrics
    like weighted-avg cost and unrealized P&L are unavailable. This table
    lets the user provide the value manually — checked from brokerage
    statements or computed from transaction history.

    `total_cost_basis` is the TOTAL dollars paid (price × shares + fees),
    matching Plaid's convention. The consolidated-holdings endpoint
    transparently falls back to this value when the snapshot is missing.
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




class TradeDecision(Base):
    """Pre-trade thesis log — written BEFORE clicking buy/sell.

    The act of writing the thesis is the feature: it raises activation
    energy for low-conviction trades and produces a journal you can
    review against actual outcomes weeks/months later. `outcome_*`
    fields are filled in retrospectively (after the time horizon
    elapses or when an invalidation trigger fires).
    """

    __tablename__ = "trade_decisions"

    decision_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    decision_date: Mapped[date] = mapped_column(Date, nullable=False)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    thesis: Mapped[str] = mapped_column(Text, nullable=False)
    expected_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    invalidation_triggers: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_size_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    time_horizon_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    outcome_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TradeTag(Base):
    """Behavioral pattern tag attached to a holding window for a ticker.

    Tags are user-defined (`panic_sold`, `held_too_long`, `bought_too_high`,
    `thesis_validated`, etc.). Many tags can apply to one (ticker, window).
    `period_end` NULL means the tag applies to the currently-open holding.
    """

    __tablename__ = "trade_tags"

    tag_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    tag: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EarningsCalendar(Base):
    """Upcoming and past earnings dates per ticker.

    Refreshed daily by `jobs.earnings_calendar`. Composite PK on
    (ticker, earnings_date) so we can store consensus + actual side-by-
    side and never duplicate.
    """

    __tablename__ = "earnings_calendar"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    earnings_date: Mapped[date] = mapped_column(Date, primary_key=True)
    earnings_time: Mapped[str | None] = mapped_column(String(16), nullable=True)
    eps_estimate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    eps_actual: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    revenue_estimate: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    revenue_actual: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
