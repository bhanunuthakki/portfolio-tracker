"""Pydantic schemas for the FastAPI surface.

These are the wire format. ORM models stay inside the backend; everything
crossing the HTTP boundary is one of these.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CashFlowTransactionOrigin: TypeAlias = Literal[
    "aggregator_transaction", "statement_supplement", "derived_share_transfer"
]
CashFlowDecisionAuthorityOut: TypeAlias = Literal[
    "provider", "brokerage_statement", "owner_approved"
]
CashFlowDecisionConfidenceOut: TypeAlias = Literal["exact", "high", "provisional"]
CashFlowEffectiveDateBasisOut: TypeAlias = Literal[
    "source_activity",
    "source_process",
    "source_settlement",
    "provider_posting",
    "owner_resolved",
]


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


class ConsolidatedHoldingOut(BaseModel):
    """A position rolled up across all accounts that hold the same security.

    `weighted_avg_cost_per_share` is `total_cost_basis / total_quantity` —
    the actual blended per-share cost across the user's accounts. None when
    we don't have cost basis from any account or when total_quantity is 0.

    Returns continue to be computed at the per-account level inside
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
    # The classification actually used by the Modified Dietz cash-flow
    # pipeline. A current provenance decision wins; otherwise this is the
    # owner override or deterministic name/type/subtype classification.
    # `excluded` is possible for a provenance-managed non-flow event.
    effective_classification: str | None = None


class PerformancePoint(BaseModel):
    date: date
    portfolio_value: Decimal
    # All return %s use Modified Dietz over the window so far.
    # Start at 0 on `start_date`, rise/fall thereafter.
    # Same denominator structure for all four so the gap between lines
    # = pure relative performance, V_start-independent in spirit.
    portfolio_return_pct: Decimal | None
    spy_return_pct: Decimal | None
    qqq_return_pct: Decimal | None
    policy_return_pct: Decimal | None
    # Synthetic values: what the user would have if every cashflow had
    # gone into the named index / policy mix instead of their actual book.
    spy_equivalent_value: Decimal | None
    qqq_equivalent_value: Decimal | None
    policy_equivalent_value: Decimal | None


class SourceCoverageRangeOut(BaseModel):
    start_date: date
    end_date: date


class CashFlowSourceGapOut(BaseModel):
    start_date: date
    end_date: date
    reason_code: str


class CashFlowSourceAttestationOut(BaseModel):
    attestation_key: str
    account_id: int
    coverage_start: date
    coverage_end: date
    source_type: str
    broker_archive_coverage: Literal[
        "unasserted", "provider_asserted", "statement_attested", "owner_asserted"
    ]
    source_reference: str
    source_sha256: str
    captured_at: datetime
    approved_at: datetime | None
    lifecycle_status: str
    superseded_at: datetime | None
    superseded_by_attestation_key: str | None
    methodology_version: str
    account_identity_sha256: str | None = None
    account_mapping_basis: str | None = None
    account_mapping_confidence: str | None = None
    source_format: str | None = None
    parser_version: str | None = None
    source_timezone: str | None = None
    source_row_count: int | None = None
    cashflow_candidate_count: int | None = None
    persisted_source_event_count: int = 0
    source_event_set_sha256: str | None = None
    manifest_sha256: str | None = None
    gaps: list[CashFlowSourceGapOut]
    validation_reason_codes: list[str]


class CashFlowAccountSourceCoverageOut(BaseModel):
    account_id: int
    status: str
    covered_ranges: list[SourceCoverageRangeOut]
    uncovered_ranges: list[SourceCoverageRangeOut]
    attestation_keys: list[str]
    broker_archive_status: Literal["complete", "partial", "unasserted"]
    broker_archive_covered_ranges: list[SourceCoverageRangeOut]
    broker_archive_uncovered_ranges: list[SourceCoverageRangeOut]


class CashFlowSourceCoverageOut(BaseModel):
    status: str
    is_complete: bool
    broker_archive_status: Literal["complete", "partial", "unasserted"]
    broker_archive_is_complete: bool
    requested_start_date: date
    requested_end_date: date
    required_start_date: date | None
    required_end_date: date | None
    accounts: list[CashFlowAccountSourceCoverageOut]
    attestations: list[CashFlowSourceAttestationOut]


class PerformanceDatedCashflow(BaseModel):
    """One net external flow used by the whole-portfolio return window."""

    date: date
    amount: Decimal
    flow_ids: list[str] = Field(default_factory=list)


class PerformanceOperativeCashflow(BaseModel):
    """One operative ledger row and its immutable reconciliation lineage."""

    flow_id: str
    date: date
    amount: Decimal
    transaction_id: str | None
    transaction_origin: CashFlowTransactionOrigin | Literal["calculation_adjustment"]
    source_event_ids: list[str]
    source_attestation_keys: list[str]
    active_decision_keys: list[str]
    decision_authorities: list[CashFlowDecisionAuthorityOut]
    decision_confidences: list[CashFlowDecisionConfidenceOut]
    assumption_codes: list[str]
    effective_date_bases: list[CashFlowEffectiveDateBasisOut]


class PerformanceBenchmarkPriceInput(BaseModel):
    """A benchmark close resolved for one valuation or flow-deployment date."""

    ticker: str
    target_date: date
    source_date: date
    close: Decimal
    resolution: Literal["same_day_close", "previous_market_close"]
    return_basis: Literal["total_return_adjusted", "raw_price_fallback"] = "raw_price_fallback"


class PerformanceBenchmarkEquation(BaseModel):
    """One cash-flow-matched counterfactual under the shared Dietz basis."""

    benchmark: str
    ending_value: Decimal
    investment_gain: Decimal
    return_pct: Decimal
    dollar_alpha: Decimal
    percentage_point_alpha: Decimal
    equation_residual: Decimal
    price_input_id: str
    price_inputs: list[PerformanceBenchmarkPriceInput]


class AccountValuationProvenanceOut(BaseModel):
    """Sanitized source metadata for one immutable valuation observation.

    Provider account locators and raw balance values remain out of this lookup;
    the performance receipt already carries the value used by the equation.
    """

    observation_key: str
    account_id: int
    as_of_date: date
    as_of_at: datetime | None
    currency: str
    source_kind: Literal["provider_api", "brokerage_statement", "provider_export"]
    source_provider: str
    has_source_record_id: bool
    source_payload_sha256: str | None
    fetched_at: datetime
    is_complete: bool
    is_empty: bool


class PerformanceEquationReceipt(BaseModel):
    """Atomic, reproducible bridge for one whole-portfolio calculation."""

    calculation_id: str
    external_flow_ledger_id: str
    portfolio_valuation_input_id: str
    opening_valuation_observation_keys: list[str] = Field(default_factory=list[str])
    ending_valuation_observation_keys: list[str] = Field(default_factory=list[str])
    included_account_ids: list[int]
    requested_start_date: date
    requested_end_date: date
    benchmark_price_resolution_policy: Literal["same_day_or_previous_us_market_close"]
    opening_value: Decimal
    dated_external_cashflows: list[PerformanceDatedCashflow]
    # Added in v1.2. Older v1 payloads omit this field, so it must remain
    # optional-on-input under the additive compatibility policy. The current
    # producer always supplies it; when supplied, the validator below enforces
    # the complete row-level lineage bridge.
    operative_external_cashflows: list[PerformanceOperativeCashflow] = Field(
        default_factory=list[PerformanceOperativeCashflow]
    )
    net_external_cashflow_in: Decimal
    ending_value: Decimal
    investment_gain: Decimal
    modified_dietz_denominator: Decimal
    portfolio_return_pct: Decimal
    portfolio_equation_residual: Decimal
    spy: PerformanceBenchmarkEquation
    qqq: PerformanceBenchmarkEquation
    policy: PerformanceBenchmarkEquation | None

    @model_validator(mode="after")
    def validate_exact_identities(self) -> PerformanceEquationReceipt:
        for keys in (
            self.opening_valuation_observation_keys,
            self.ending_valuation_observation_keys,
        ):
            if len(keys) != len(set(keys)):
                raise ValueError("valuation observation keys must be unique")
        net_flow = sum((flow.amount for flow in self.dated_external_cashflows), Decimal(0))
        if net_flow != self.net_external_cashflow_in:
            raise ValueError("dated external cashflows do not reconcile to net flow")
        dated_dates = [flow.date for flow in self.dated_external_cashflows]
        if len(dated_dates) != len(set(dated_dates)):
            raise ValueError("dated external cashflow dates must be unique")
        if "operative_external_cashflows" in self.model_fields_set:
            operative_ids = [flow.flow_id for flow in self.operative_external_cashflows]
            if len(operative_ids) != len(set(operative_ids)):
                raise ValueError("operative external cashflow IDs must be unique")
            operative_by_date: dict[date, Decimal] = {}
            for flow in self.operative_external_cashflows:
                operative_by_date[flow.date] = (
                    operative_by_date.get(flow.date, Decimal(0)) + flow.amount
                )
            nonzero_operative_dates = {
                flow_date for flow_date, amount in operative_by_date.items() if amount != 0
            }
            if set(dated_dates) != nonzero_operative_dates:
                raise ValueError("dated external cashflows must cover every nonzero operative date")
            for dated_flow in self.dated_external_cashflows:
                if operative_by_date.get(dated_flow.date, Decimal(0)) != dated_flow.amount:
                    raise ValueError("operative external cashflows do not reconcile by date")
                if set(dated_flow.flow_ids) != {
                    flow.flow_id
                    for flow in self.operative_external_cashflows
                    if flow.date == dated_flow.date
                }:
                    raise ValueError("dated external cashflow IDs do not match operative rows")
        if self.ending_value - self.opening_value - net_flow != self.investment_gain:
            raise ValueError("whole-portfolio value bridge does not reconcile")
        if self.portfolio_equation_residual != 0:
            raise ValueError("whole-portfolio equation residual must be zero")
        expected_return = (self.investment_gain / self.modified_dietz_denominator) * Decimal(100)
        if self.portfolio_return_pct != expected_return:
            raise ValueError("whole-portfolio return does not reconcile to Dietz capital")
        for benchmark in (self.spy, self.qqq, self.policy):
            if benchmark is None:
                continue
            if not benchmark.price_inputs:
                raise ValueError(f"{benchmark.benchmark} price inputs must not be empty")
            for price_input in benchmark.price_inputs:
                if price_input.close <= 0:
                    raise ValueError(
                        f"{benchmark.benchmark} price inputs must contain positive closes"
                    )
                if (
                    price_input.resolution == "same_day_close"
                    and price_input.source_date != price_input.target_date
                ):
                    raise ValueError(
                        f"{benchmark.benchmark} same-day price input dates do not match"
                    )
                if (
                    price_input.resolution == "previous_market_close"
                    and price_input.source_date >= price_input.target_date
                ):
                    raise ValueError(f"{benchmark.benchmark} prior-close price input is not prior")
            if benchmark.ending_value - self.opening_value - net_flow != benchmark.investment_gain:
                raise ValueError(f"{benchmark.benchmark} value bridge does not reconcile")
            if benchmark.equation_residual != 0:
                raise ValueError(f"{benchmark.benchmark} equation residual must be zero")
            benchmark_return = (
                benchmark.investment_gain / self.modified_dietz_denominator
            ) * Decimal(100)
            if benchmark.return_pct != benchmark_return:
                raise ValueError(
                    f"{benchmark.benchmark} return does not reconcile to Dietz capital"
                )
            if benchmark.dollar_alpha != self.investment_gain - benchmark.investment_gain:
                raise ValueError(f"{benchmark.benchmark} dollar alpha does not reconcile")
            if benchmark.percentage_point_alpha != self.portfolio_return_pct - benchmark.return_pct:
                raise ValueError(f"{benchmark.benchmark} percentage-point alpha does not reconcile")
        return self


class PerformanceSeries(BaseModel):
    methodology: Literal["performance.modified_dietz"]
    methodology_version: Literal["2"]
    calculation_status: Literal["available", "unavailable"]
    reconstruction_certification: Literal[
        "observed_certified", "source_provisional", "modeled_provisional", "unavailable"
    ] = "unavailable"
    calculation_reason_codes: list[str]
    start_date: date
    end_date: date
    base_value: Decimal
    points: list[PerformancePoint]
    # The earliest date in the window with an observed complete holdings or
    # whole-account valuation boundary (not transaction-walk reconstruction).
    # Anything before this is modeled. None when the window is reconstructed.
    earliest_observed_date: date | None = None
    # Net external cashflow into the portfolio over the window (positive = in).
    # Surfaced so the UI can show contributions alongside total return.
    net_external_cashflow_in: Decimal | None
    # Separate from structural ledger validity: approved source evidence must
    # cover every valuation account over the exact (start, end] flow window.
    source_coverage: CashFlowSourceCoverageOut
    # Whether the start value is suspiciously low vs the end value (suggests
    # the backfill is missing pre-existing positions). Frontend renders a
    # warning when true.
    backfill_start_unreliable: bool
    # Boundary lineage is explicit because a transaction-walk value is an
    # estimate, not broker-observed evidence. `None` means the requested
    # boundary could not be supported and the calculation is unavailable.
    opening_value_provenance: (
        Literal[
            "observed_complete_snapshot",
            "observed_account_valuation",
            "modeled_transaction_walkback",
        ]
        | None
    )
    ending_value_provenance: (
        Literal[
            "observed_complete_snapshot",
            "observed_account_valuation",
            "modeled_transaction_walkback",
        ]
        | None
    )
    opening_valuation_observation_keys: list[str] = Field(default_factory=list[str])
    ending_valuation_observation_keys: list[str] = Field(default_factory=list[str])
    # Exact account universe whose value and flows were paired by the return
    # engine. Empty only when no valued account universe could be established.
    valuation_account_ids: list[int]
    # Present only when every prerequisite is available. All headline dollar
    # and percentage values are derived atomically from this exact input set.
    equation_receipt: PerformanceEquationReceipt | None


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
      * `warning` — affects accuracy of derived metrics (returns, P&L)
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
    # ISO YYYY-MM-DD acquisition date. NULL preserves the pre-existing
    # fallback (synthetic SPY buy anchored at earliest snapshot date).
    # Most useful for ACATS-in shares acquired years before tracking began.
    acquired_at: date | None = None


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
    acquired_at: date | None = None
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

    ticker: str = Field(min_length=1, max_length=16)
    weight_pct: Decimal = Field(ge=0, le=100)
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("ticker")
    @classmethod
    def _normalize_ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("ticker must be non-empty")
        return normalized

    @field_validator("notes")
    @classmethod
    def _normalize_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class PolicyReplaceIn(BaseModel):
    weights: list[PolicyWeightIn] = Field(max_length=500)
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    source: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    as_of: datetime

    @field_validator("idempotency_key")
    @classmethod
    def _strip_idempotency_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("source")
    @classmethod
    def _normalize_source(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("as_of")
    @classmethod
    def _require_aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must include a UTC offset")
        return value.astimezone(UTC)


class PolicyWeightOut(BaseModel):
    ticker: str
    weight_pct: Decimal
    notes: str | None
    updated_at: datetime


class PolicyRecomputationOut(BaseModel):
    status: Literal["current", "required"]
    policy_revision: int
    reason: Literal["policy_weights_changed"] | None = None


class PolicyWriteReceiptOut(BaseModel):
    receipt_id: str
    idempotency_key: str
    outcome: Literal["applied", "unchanged"]
    recorded_at: datetime


class PolicyOut(BaseModel):
    weights: list[PolicyWeightOut]
    total_pct: Decimal
    is_balanced: bool
    revision: int
    source: str
    as_of: datetime
    recomputation: PolicyRecomputationOut
    receipt: PolicyWriteReceiptOut | None = None


# ---------------------------------------------------------------------------
# Positioning — the Holdings "how is the book positioned" cuts
# ---------------------------------------------------------------------------


class PositioningBucketOut(BaseModel):
    """One slice of a breakdown: a labeled bucket with its share of the book."""

    label: str
    value: Decimal
    weight_pct: Decimal
    count: int


class ConcentrationOut(BaseModel):
    """Concentration summary across consolidated-by-security positions."""

    num_positions: int
    top1_weight_pct: Decimal | None
    top5_weight_pct: Decimal | None
    top10_weight_pct: Decimal | None
    # HHI on the 0-10000 scale (sum of squared percent weights);
    # effective_holdings = 1 / sum(weight_fraction^2) reads as "behaves like
    # ~N equal positions".
    hhi: float | None
    effective_holdings: float | None


class PositionCorrelationRow(BaseModel):
    """Per-ticker correlation + beta to each benchmark over the window.

    Each `correlation_*` / `beta_*` is None when that security lacks enough
    overlapping price history with the benchmark (see `sample_size`).
    """

    security_id: int
    ticker: str | None
    name: str | None
    value: Decimal
    weight_pct: Decimal
    sample_size: int
    correlation_spy: float | None
    beta_spy: float | None
    correlation_qqq: float | None
    beta_qqq: float | None
    correlation_policy: float | None
    beta_policy: float | None


class PositioningOut(BaseModel):
    """Everything the Holdings positioning section renders in one payload."""

    snapshot_date: date
    start_date: date
    end_date: date
    total_value: Decimal
    by_asset_type: list[PositioningBucketOut]
    by_sector: list[PositioningBucketOut]
    by_region: list[PositioningBucketOut]
    by_account_type: list[PositioningBucketOut]
    concentration: ConcentrationOut
    correlations: list[PositionCorrelationRow]
    # Book value-weighted average correlation to SPY across the names that
    # have a correlation — a single "how diversified am I vs the market" read.
    weighted_avg_correlation_spy: float | None
    has_policy: bool
    notes: list[str]


class SecurityClassificationIn(BaseModel):
    """Set / replace the manual sector + region classification for a security."""

    security_id: int
    sector: str | None = None
    region: str | None = None
    notes: str | None = None


class SecurityClassificationOut(BaseModel):
    security_id: int
    ticker: str | None
    security_name: str | None
    sector: str | None
    region: str | None
    # 'auto' (yfinance enrichment) | 'manual' (user-entered)
    source: str
    notes: str | None
    updated_at: datetime
