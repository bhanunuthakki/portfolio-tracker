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


class StockSplit(Base):
    """A corporate stock-split event for a security.

    `ratio` is the yfinance convention: shares-after / shares-before for one
    pre-split share — 4.0 for a 4:1 forward split, 0.125 for a 1:8 reverse.
    `prices` stores split-adjusted (back-adjusted) closes, so historical
    transaction/snapshot QUANTITIES (recorded in as-traded units) must be
    scaled to today's split-adjusted units before being valued against those
    prices in the walk-back — see `services/splits.py`. Refreshed by
    `jobs.splits` from yfinance `.splits`. Composite PK so a re-fetch upserts.
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
