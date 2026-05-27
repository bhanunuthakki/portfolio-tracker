"""Pydantic schemas for the FastAPI surface.

These are the wire format. ORM models stay inside the backend; everything
crossing the HTTP boundary is one of these.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class LinkTokenOut(BaseModel):
    link_token: str


class ExchangePublicTokenIn(BaseModel):
    public_token: str
    institution_id: str | None = None
    institution_name: str | None = None


class ExchangePublicTokenOut(BaseModel):
    item_id: int
    accounts_linked: int


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: int
    name: str
    official_name: str | None
    type: str
    subtype: str | None
    mask: str | None
    currency: str


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_id: int
    source: str
    institution_name: str | None
    plaid_institution_id: str | None
    linked_at: datetime
    last_refreshed_at: datetime | None
    is_data_active: bool
    accounts: list[AccountOut]


class ItemDataActiveIn(BaseModel):
    """Toggle whether this Item's data flows into aggregations.

    Setting `is_data_active=False` keeps the connection linked (so the
    aggregator slot isn't surrendered) but excludes the Item's accounts
    from every holdings / transactions / V / risk / trade-analysis query.
    """

    is_data_active: bool


class HoldingOut(BaseModel):
    snapshot_date: date
    account_id: int
    account_name: str
    security_id: int
    ticker: str | None
    name: str | None
    quantity: Decimal
    institution_price: Decimal | None
    institution_value: Decimal | None
    cost_basis: Decimal | None
    currency: str


class HoldingByAccountOut(BaseModel):
    """The drill-down portion of a consolidated holding — one row per account."""

    account_id: int
    account_name: str
    quantity: Decimal
    institution_value: Decimal | None
    cost_basis: Decimal | None
    # Where the cost basis came from:
    #   None              — Plaid/SnapTrade-reported (broker truth)
    #   'manual'          — user-entered override
    #   'inferred_acats'  — derived from ACATS pair-matching
    #   'inferred_1099'   — reserved
    # Surfaced so the UI can badge inferred / manual rows distinctly.
    cost_basis_source: str | None = None
    # True when broker-reported cost basis looks implausibly low vs market
    # value (e.g. $15 reported for a $13,000 position). Indicates the broker
    # didn't transmit a full cost figure — common for ACATS-in positions or
    # buys older than Plaid's 24-month retention. Never set when an override
    # is in place (the override IS the truth in that case).
    cost_basis_unreliable: bool = False


class EarningsEnrichment(BaseModel):
    """Per-ticker context pulled from the companion earnings-summary project.
    All fields nullable so the dashboard degrades cleanly when the companion
    project isn't available."""

    tracked: bool = False                     # in earnings-summary's watchlist?
    list_type: str | None = None              # "portfolio" | "watchlist"
    next_earnings_date: date | None = None
    thesis_status: str | None = None          # "ok" | "breach" | ...
    thesis_summary: str | None = None
    has_brief: bool = False
    latest_brief_iso_date: str | None = None


class ConsolidatedHoldingOut(BaseModel):
    """A position rolled up across all accounts that hold the same security.

    `weighted_avg_cost_per_share` is `total_cost_basis / total_quantity` —
    the actual blended per-share cost across the user's accounts. None when
    we don't have cost basis from any account or when total_quantity is 0.

    Returns / TWR continue to be computed at the per-account level inside
    the `services/performance.py` pipeline; this consolidation is a
    presentation-layer rollup only.
    """

    snapshot_date: date
    security_id: int
    ticker: str | None
    name: str | None
    total_quantity: Decimal
    total_value: Decimal | None
    total_cost_basis: Decimal | None
    weighted_avg_cost_per_share: Decimal | None
    unrealized_pnl: Decimal | None
    accounts: list[HoldingByAccountOut]
    # True iff any contributing account has cost_basis_unreliable=True. Lets
    # the UI dim the consolidated P&L cell with a single boolean rather than
    # walking the accounts list.
    has_unreliable_cost_basis: bool = False
    currency: str
    # Cross-project enrichment from earnings-summary, if available.
    earnings: EarningsEnrichment | None = None


class InvestmentTransactionOut(BaseModel):
    plaid_investment_transaction_id: str
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
    # User-set override (None if no override). One of:
    #   external_in / external_out / internal
    override_classification: str | None = None
    # The classification actually used by the cashflow / TWR pipeline.
    # Same as override_classification when set; otherwise derived from
    # type+subtype heuristics. Useful for the UI to show "what's the
    # pipeline doing with this row right now."
    effective_classification: str | None = None


class PerformancePoint(BaseModel):
    date: date
    portfolio_value: Decimal
    # All return %s use Modified Dietz over the window so far.
    # Start at 0 on `start_date`, rise/fall thereafter.
    # Same denominator structure for all four so the gap between lines
    # = pure relative performance, V_start-independent in spirit.
    portfolio_return_pct: Decimal
    spy_return_pct: Decimal | None
    qqq_return_pct: Decimal | None
    policy_return_pct: Decimal | None
    # Synthetic values: what the user would have if every cashflow had
    # gone into the named index / policy mix instead of their actual book.
    spy_equivalent_value: Decimal | None
    qqq_equivalent_value: Decimal | None
    policy_equivalent_value: Decimal | None


class PerformanceSeries(BaseModel):
    start_date: date
    end_date: date
    base_value: Decimal
    points: list[PerformancePoint]
    # The earliest date in the window that's an OBSERVED forward snapshot
    # (not a transaction-walk reconstruction). Anything before this is
    # modeled. None when the entire window is reconstructed.
    earliest_observed_date: date | None = None
    # Net external cashflow into the portfolio over the window (positive = in).
    # Surfaced so the UI can show contributions alongside total return.
    net_external_cashflow_in: Decimal = Decimal(0)
    # Whether the start value is suspiciously low vs the end value (suggests
    # the backfill is missing pre-existing positions). Frontend renders a
    # warning when true.
    backfill_start_unreliable: bool = False


class CashflowGroupOut(BaseModel):
    """A summary row for a (type, subtype) group of investment transactions."""

    type: str
    subtype: str | None
    count: int
    sum_amount: Decimal
    classified_as_external_cashflow: bool


class CashflowAuditOut(BaseModel):
    start_date: date
    end_date: date
    groups: list[CashflowGroupOut]
    net_external_cashflow_in: Decimal
    notes: list[str]


class DataQualityFindingOut(BaseModel):
    """A single data-quality issue surfaced for the user.

    `severity` ranks user attention:
      * `info`    — known limitation, no action required (e.g., SoFi
                    doesn't expose cost basis through Plaid).
      * `warning` — affects accuracy of derived metrics (TWR, P&L)
                    but the rest of the data is fine.
      * `error`   — broken contract; something needs fixing.

    `recommended_action` is rendered on the UI as the next-step text.
    """

    category: str
    severity: str
    title: str
    detail: str
    recommended_action: str | None = None
    context: dict[str, str] = {}


class DataQualityReportOut(BaseModel):
    generated_at: datetime
    findings: list[DataQualityFindingOut]
    summary_counts: dict[str, int]


class CostBasisOverrideIn(BaseModel):
    account_id: int
    security_id: int
    total_cost_basis: Decimal
    notes: str | None = None


class CostBasisOverrideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: int
    security_id: int
    account_name: str
    ticker: str | None
    security_name: str | None
    total_cost_basis: Decimal
    notes: str | None
    # 'manual' | 'inferred_acats' | 'inferred_1099'
    source: str = "manual"
    updated_at: datetime


class TickerOverrideIn(BaseModel):
    security_id: int
    ticker: str
    notes: str | None = None


class TickerOverrideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    security_id: int
    plaid_security_id: str
    security_name: str | None
    ticker: str
    notes: str | None
    updated_at: datetime


class TransactionOverrideIn(BaseModel):
    """Set / replace the cashflow classification for one transaction."""

    plaid_investment_transaction_id: str
    classification: str  # external_in | external_out | internal
    notes: str | None = None


class TransactionOverrideOut(BaseModel):
    plaid_investment_transaction_id: str
    classification: str
    notes: str | None
    updated_at: datetime
    # Helpful context for the UI without a separate fetch.
    tx_date: date | None = None
    tx_type: str | None = None
    tx_subtype: str | None = None
    tx_amount: Decimal | None = None
    account_name: str | None = None
    ticker: str | None = None


class PolicyWeightIn(BaseModel):
    """One row in the user's policy-portfolio target allocation.

    `weight_pct` is in PERCENT (e.g., 60.0 for 60%) — the API converts to
    basis points internally. Frontend handles % directly for usability.
    """

    ticker: str
    weight_pct: Decimal
    notes: str | None = None


class PolicyWeightOut(BaseModel):
    ticker: str
    weight_pct: Decimal
    notes: str | None
    updated_at: datetime


class PolicyOut(BaseModel):
    weights: list[PolicyWeightOut]
    total_pct: Decimal
    is_balanced: bool


class HumanCapitalBucketWeightIn(BaseModel):
    """One bucket assignment for a ticker.

    `weight_pct` is 0–100 (matches the DB column). A ticker can carry
    multiple rows (one per bucket); the per-ticker upsert call replaces
    all existing bucket rows for that ticker with the supplied list.
    """

    bucket: str
    weight_pct: Decimal
    notes: str | None = None


class HumanCapitalOverlapIn(BaseModel):
    """PUT body: full set of (bucket, weight) rows for one ticker.

    PUT semantics — supplying an empty `buckets` list is the same as
    DELETE for that ticker (all rows removed). Use the DELETE endpoint
    when the intent is clearer that way.
    """

    ticker: str
    buckets: list[HumanCapitalBucketWeightIn]


class HumanCapitalBucketWeightOut(BaseModel):
    bucket: str
    weight_pct: Decimal
    notes: str | None
    updated_at: datetime


class HumanCapitalOverlapOut(BaseModel):
    """One row per ticker; `buckets` carries the per-bucket detail."""

    ticker: str
    buckets: list[HumanCapitalBucketWeightOut]


class HumanCapitalOverlapsOut(BaseModel):
    """Flat list of all (ticker, bucket, weight) rows + the active caps.

    The caps come from `services/coaching.py:_BUCKET_CAPS` so the editor
    can render the cap inline next to each bucket without a second fetch.
    """

    overlaps: list[HumanCapitalOverlapOut]
    bucket_caps_pct: dict[str, Decimal]


class JobRunOut(BaseModel):
    job: str
    status: str
    items_processed: int
    started_at: datetime
    finished_at: datetime
