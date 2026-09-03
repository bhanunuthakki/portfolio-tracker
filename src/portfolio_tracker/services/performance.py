"""Portfolio performance vs. benchmarks (money-weighted Modified Dietz).

Builds a daily series of cumulative window returns for the portfolio and for
synthetic SPY / QQQ / policy books that receive the SAME external cashflows,
so the GAP between the lines is a clean relative-performance signal even when
the reconstructed V_start is noisy.

NOTE ON THE METRIC: despite "benchmarking" connoting time-weighted return,
this series is **money-weighted (Modified Dietz)**, NOT a chained daily TWR.
At each observation date `d` it computes a single-window return-since-start
with a contribution-weighted denominator:

    return_pct(d) = (V(d) − V_start − Σ_{i: d_0<d_i≤d} C_i)
                    / (V_start + Σ_{i: d_0<d_i≤d} C_i · (d − d_i)/(d − d_0))

(see `modified_dietz_series`). `C_i` is an *external* cashflow on day `d_i`
— deposits, withdrawals, ACATS transfers; trades, dividends, fees, and
interest are internal events that move V but not the cashflow term. The
contribution-weighted denominator makes the % sensitive to cashflow timing
on a heavy-contribution book. This whole-portfolio series is the canonical
headline comparison; position-level results are supporting attribution.

Two-stage construction:

  1. **Daily portfolio value** is sourced from `holdings_snapshots` going
     forward; for dates predating the first snapshot, it's reconstructed by
     walking `investment_transactions` backward and valuing each position
     against `prices` (yfinance backfill).

  2. **Modified Dietz** is evaluated per date over the window via the formula
     above; the result is rebased to an index for the chart.

The raw `portfolio_value` is also returned per-point so the UI can show the
underlying dollar series alongside the rebased index if it wants.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Benchmark,
    HoldingSnapshot,
    InvestmentTransaction,
    InvestmentTransactionType,
    Price,
    Security,
)
from portfolio_tracker.schemas import (
    PerformanceBenchmarkEquation,
    PerformanceBenchmarkPriceInput,
    PerformanceDatedCashflow,
    PerformanceEquationReceipt,
    PerformanceOperativeCashflow,
    PerformancePoint,
    PerformanceSeries,
)
from portfolio_tracker.services import external_flow_ledger
from portfolio_tracker.services.active_items import valued_account_ids
from portfolio_tracker.services.cashflow_source_coverage import (
    assess_cashflow_source_coverage,
    source_coverage_out,
)
from portfolio_tracker.services.policy import load_policy_weights
from portfolio_tracker.services.splits import load_split_factors

# Diagnostics-only threshold: daily portfolio-value swings beyond this are
# almost certainly reconstruction artifacts (unobserved transfers, gifted
# stock, etc.) rather than real market moves. Surfaced via the data-quality
# report so the user can investigate the underlying transactions.
_ABNORMAL_DAILY_RETURN: Decimal = Decimal("0.30")

_NO_PORTFOLIO_VALUES = "no_portfolio_values"
_EXTERNAL_SHARE_MOVEMENT_MISSING_SECURITY = "external_share_movement_missing_security"
_EXTERNAL_SHARE_MOVEMENT_MISSING_TICKER = "external_share_movement_missing_ticker"
_EXTERNAL_SHARE_MOVEMENT_PRICE_UNAVAILABLE = "external_share_movement_price_unavailable"
_NONPOSITIVE_DIETZ_DENOMINATOR = "nonpositive_dietz_denominator"
_PORTFOLIO_START_VALUE_UNAVAILABLE = "portfolio_start_value_unavailable"
_PORTFOLIO_END_VALUE_UNAVAILABLE = "portfolio_end_value_unavailable"
_PARTIAL_SNAPSHOT_START_DATE = "partial_snapshot_start_date"
_PARTIAL_SNAPSHOT_END_DATE = "partial_snapshot_end_date"
_MODELED_OPENING_ACCOUNT_COVERAGE_INCOMPLETE = "modeled_opening_account_coverage_incomplete"
_MODELED_OPENING_VALUATION_COVERAGE_INCOMPLETE = "modeled_opening_valuation_coverage_incomplete"
_UNPRICEABLE_HOLDING_SNAPSHOT = "unpriceable_holding_snapshot"

_ValueProvenance = Literal["observed_complete_snapshot", "modeled_transaction_walkback"]
_OBSERVED_VALUE: _ValueProvenance = "observed_complete_snapshot"
_MODELED_VALUE: _ValueProvenance = "modeled_transaction_walkback"
_SPY_BENCHMARK_PRICE_UNAVAILABLE = "spy_benchmark_price_unavailable"
_QQQ_BENCHMARK_PRICE_UNAVAILABLE = "qqq_benchmark_price_unavailable"
_POLICY_BENCHMARK_PRICE_UNAVAILABLE = "policy_benchmark_price_unavailable"

# Query cushion only. Resolution below accepts exactly the target market close
# or, for a weekend/known US-market holiday, the immediately preceding close.
_BENCHMARK_QUERY_LOOKBACK_DAYS = 7
_EXTERNAL_FLOW_SOURCE_COVERAGE_INCOMPLETE = "external_flow_source_coverage_incomplete"


@dataclass(frozen=True)
class _CashflowAssessment:
    cashflows: dict[date, Decimal]
    calculation_reason_codes: tuple[str, ...]
    external_flow_ledger_id: str | None = None
    account_ids: tuple[int, ...] = ()
    entries: tuple[external_flow_ledger.ExternalFlowEntry, ...] = ()


@dataclass(frozen=True)
class _PortfolioValueAssessment:
    values: dict[date, Decimal]
    provenance: dict[date, _ValueProvenance]
    valuation_account_ids: tuple[int, ...]
    calculation_reason_codes: tuple[str, ...]


# `transfer` is Plaid's catch-all for asset movements. EXTERNAL transfers
# (ACATS in/out, ACH deposits, wires) are TWR cashflow. INTERNAL transfers
# (option assignments, account-to-account moves within the same login) are
# NOT — they're position rearrangements that don't change the user's basis.
# Plaid signals the difference via `subtype`.
_INTERNAL_TRANSFER_SUBTYPES: frozenset[str] = frozenset(
    {
        "assignment",  # option exercise / assignment
        "exercise",
        "merger",
        "spin off",
        "split",
        "stock distribution",
    }
)

# `cash` subtypes that represent EXTERNAL flows INTO the portfolio.
# Direction is determined by the subtype NAME, not Plaid's `amount` sign —
# different brokers report contribution/deposit signs inconsistently
# (some from the cash account's perspective, some from the investor's).
_INFLOW_CASH_SUBTYPES: frozenset[str] = frozenset(
    {
        "deposit",
        "contribution",
        "rollover",
        "wire",
        "ach",
    }
)

# `cash` subtypes that represent EXTERNAL flows OUT of the portfolio.
_OUTFLOW_CASH_SUBTYPES: frozenset[str] = frozenset(
    {
        "withdrawal",
    }
)

# `cash` subtype that's ambiguous in direction — fall back to Plaid's sign.
_AMBIGUOUS_CASH_SUBTYPES: frozenset[str] = frozenset(
    {
        "transfer",
    }
)

# `cash`-typed subtypes that **change share quantities** without a real cash
# flow. SnapTrade emits these instead of TRANSFER for ACATS in/out, option
# assignment/expiration to underlying shares, and dividend reinvestments.
# The walk-back must reverse the *quantity* (treat exactly like TRANSFER —
# `rolling[sid] += -signed_quantity`) AND must NOT touch the cash adjustment
# series (these have either amount=$0 or a paired-but-not-recorded cash
# event, so applying the default cash/`-magnitude` rule produces phantom
# cash holes on past dates).
#
# Surfaced bug this fixes: an outgoing ACATS that moved several positions
# out of a brokerage IRA
# was not being reversed during walk-back. Anchor positions (today) reflect
# the post-transfer state — without reversing the transfer, the walk-back
# left those shares OUT of historical rolling. Combined with the buys we
# do reverse during walk-back, the rolling quantity went **negative**,
# producing negative dollar position values that flipped sign across days
# and manifested as $30-$60k step-ups in the chart.
_SHARE_MOVING_CASH_SUBTYPES: frozenset[str] = frozenset(
    {
        "external_asset_transfer_in",
        "external_asset_transfer_out",
        "optionassignment",
        "optionexpiration",
        "rei",  # dividend reinvestment — shares acquired, no separate cash leg
    }
)

# Broad-market US-equity ETFs the user likely treats as "core / index
# allocation" rather than active stock-picking. When the user toggles the
# "Exclude broad-index holdings" view on the dashboard, the value of any
# positions in these tickers is removed from V on every date so the chart
# isolates the active stock-picking portion. Internal flows from buying /
# selling these (which transfer cash between active and index buckets) are
# also tracked so the synthetic benchmark gets a fair matched-flow series.
#
# Excludes QQQ deliberately — it's already used as a benchmark line. Add
# total-market mutual-fund tickers if the user holds them (VTSAX, FXAIX).
_BROAD_INDEX_ETF_TICKERS: frozenset[str] = frozenset({"VTI", "VOO", "SPY", "IVV", "RSP"})


def compute_performance_series(
    session: Session,
    start_date: date,
    end_date: date,
    reserve_amount: Decimal = Decimal(0),
    exclude_index_etfs: bool = False,
) -> PerformanceSeries:
    """Build a money-flow-matched return series for [start_date, end_date].

    For the actual portfolio AND for synthetic SPY / QQQ "what if I'd just
    bought the index with the same money flows" portfolios, we compute a
    Modified Dietz return at every observation date:

        return_pct(d) = (V(d) - V_start - cumulative_C(d))
                        / (V_start + sum_{i: d_0<d_i<=d} C_i * (d-d_i)/(d-d_0))

    Both portfolio and benchmark series share the SAME denominator and dated
    external-flow set on each day, so percentage-point and dollar differences
    reconcile. Any warning about reconstructed V_start still applies to both
    the absolute returns and their comparison.

    `reserve_amount` carves a fixed dollar amount off the top of both the
    actual portfolio's V and the synthetic-benchmark starting capital. The
    intent is "treat the first $X as untouchable emergency reserves and
    only show the investable portion's return." The same number is
    subtracted from V on every date (it's the same reserve every day) and
    from each synthetic's starting base. Cashflows are unchanged — labeling
    cash "reserves" doesn't change the fact that real-money deposits
    happened. Set to `Decimal(0)` for the standard full-portfolio view.

    `exclude_index_etfs` strips broad-market index ETFs (VTI/VOO/SPY/IVV/
    RSP — see `_BROAD_INDEX_ETF_TICKERS`) from V on every date so the chart
    isolates the active stock-picking portion. Buying/selling those ETFs
    moves cash between the "index bucket" and the "active bucket" inside
    the user's portfolio — these are *internal* flows from the active
    portion's perspective. The synthetic benchmark must see those same
    flows to be apples-to-apples, otherwise active V grows free of them
    and the comparison is rigged in active's favor. We compute internal
    flows from index-ETF BUYs (active outflow) and SELLs (active inflow)
    and add them to the cashflow series used for both Modified Dietz on
    V_active and for the synthetic benchmarks.
    """
    account_ids = valued_account_ids(session)
    source_coverage = assess_cashflow_source_coverage(
        session,
        start_date,
        end_date,
        account_ids=account_ids,
    )
    source_coverage_read = source_coverage_out(source_coverage)
    value_assessment = _daily_portfolio_value_assessment(session, start_date, end_date)
    daily_value = value_assessment.values
    opening_provenance = value_assessment.provenance.get(start_date)
    ending_provenance = value_assessment.provenance.get(end_date)
    cashflow_assessment = _daily_external_cashflow_assessment(session, start_date, end_date)
    daily_cashflow = cashflow_assessment.cashflows
    benchmark_series = _benchmark_series(session, start_date, end_date)
    benchmark_return_basis = _benchmark_return_basis(session, start_date, end_date)
    policy_weights = load_policy_weights(session)

    # Gather every independently assessable prerequisite before returning for
    # an unsupported valuation boundary. This gives reconciliation one closure
    # packet instead of revealing ledger and benchmark gaps one retry at a time.
    boundary_reason_codes = set(value_assessment.calculation_reason_codes)
    boundary_reason_codes.update(cashflow_assessment.calculation_reason_codes)
    if not daily_value:
        boundary_reason_codes.add(_NO_PORTFOLIO_VALUES)
    if start_date not in daily_value:
        boundary_reason_codes.add(_PORTFOLIO_START_VALUE_UNAVAILABLE)
    if end_date not in daily_value:
        boundary_reason_codes.add(_PORTFOLIO_END_VALUE_UNAVAILABLE)
    if daily_value and ending_provenance != _OBSERVED_VALUE:
        boundary_reason_codes.add(_PORTFOLIO_END_VALUE_UNAVAILABLE)
    if not source_coverage.is_complete:
        boundary_reason_codes.add(_EXTERNAL_FLOW_SOURCE_COVERAGE_INCOMPLETE)

    preliminary_price_dates = sorted(
        {start_date, end_date, *daily_value}
        | {flow_date for flow_date in daily_cashflow if start_date < flow_date <= end_date}
    )
    if _resolved_price_inputs(benchmark_series.get("SPY", {}), preliminary_price_dates) is None:
        boundary_reason_codes.add(_SPY_BENCHMARK_PRICE_UNAVAILABLE)
    if _resolved_price_inputs(benchmark_series.get("QQQ", {}), preliminary_price_dates) is None:
        boundary_reason_codes.add(_QQQ_BENCHMARK_PRICE_UNAVAILABLE)
    required_policy_tickers = {ticker for ticker, weight in policy_weights.items() if weight > 0}
    if any(
        _resolved_price_inputs(benchmark_series.get(ticker, {}), preliminary_price_dates) is None
        for ticker in required_policy_tickers
    ):
        boundary_reason_codes.add(_POLICY_BENCHMARK_PRICE_UNAVAILABLE)

    has_valuation_boundary_failure = (
        not daily_value
        or start_date not in daily_value
        or end_date not in daily_value
        or ending_provenance != _OBSERVED_VALUE
        or bool(value_assessment.calculation_reason_codes)
    )
    if has_valuation_boundary_failure:
        return PerformanceSeries(
            methodology="performance.modified_dietz",
            methodology_version="2",
            calculation_status="unavailable",
            reconstruction_certification="unavailable",
            calculation_reason_codes=sorted(boundary_reason_codes),
            start_date=start_date,
            end_date=end_date,
            base_value=daily_value.get(start_date, Decimal(0)),
            points=[
                PerformancePoint(
                    date=current_date,
                    portfolio_value=daily_value[current_date],
                    portfolio_return_pct=None,
                    spy_return_pct=None,
                    qqq_return_pct=None,
                    policy_return_pct=None,
                    spy_equivalent_value=None,
                    qqq_equivalent_value=None,
                    policy_equivalent_value=None,
                )
                for current_date in sorted(daily_value)
            ],
            earliest_observed_date=_earliest_observed_date(session, start_date, end_date),
            net_external_cashflow_in=None,
            backfill_start_unreliable=(opening_provenance != _OBSERVED_VALUE),
            opening_value_provenance=opening_provenance,
            ending_value_provenance=ending_provenance,
            valuation_account_ids=list(value_assessment.valuation_account_ids),
            equation_receipt=None,
            source_coverage=source_coverage_read,
        )

    sorted_dates = sorted(daily_value.keys())

    # ---- Index-ETF carve-out -------------------------------------------
    daily_value_active = daily_value
    if exclude_index_etfs:
        index_sids = _broad_index_security_ids(session)
        if index_sids:
            daily_index_value = _daily_subset_value(session, start_date, end_date, index_sids)
            daily_value_active = {
                d: daily_value[d] - daily_index_value.get(d, Decimal(0)) for d in daily_value
            }
            internal_flows = _daily_internal_index_cashflows(
                session, start_date, end_date, index_sids
            )
            for d, c in internal_flows.items():
                daily_cashflow[d] = daily_cashflow.get(d, Decimal(0)) + c

    # The opening value is end-of-day, so activity on that date is already
    # represented in V_start. Keep one canonical (start, end] flow set for
    # the portfolio return, synthetic books, and reported four-part bridge.
    daily_cashflow = _cashflows_within_valuation_period(sorted_dates, daily_cashflow)

    # Reserve adjustment: shift V_start, V[d], and synthetic bases down by
    # `reserve_amount`. The cashflow series is left intact. Clamp to zero
    # so a too-large reserve doesn't produce a negative denominator.
    base_value = daily_value_active[sorted_dates[0]]
    end_value = daily_value_active[sorted_dates[-1]]
    reserve = max(Decimal(0), reserve_amount)
    if reserve > base_value:
        reserve = base_value
    base_value_adj = base_value - reserve
    daily_value_adj = (
        {d: v - reserve for d, v in daily_value_active.items()}
        if reserve > 0
        else daily_value_active
    )

    required_price_dates = sorted(set(sorted_dates) | set(daily_cashflow))
    spy_price_inputs = _resolved_price_inputs(benchmark_series.get("SPY", {}), required_price_dates)
    qqq_price_inputs = _resolved_price_inputs(benchmark_series.get("QQQ", {}), required_price_dates)
    policy_price_inputs: dict[str, dict[date, tuple[date, Decimal]]] = {}
    for ticker, weight in policy_weights.items():
        if weight <= 0:
            continue
        resolved = _resolved_price_inputs(benchmark_series.get(ticker, {}), required_price_dates)
        if resolved is not None:
            policy_price_inputs[ticker] = resolved

    calculation_reason_codes = set(cashflow_assessment.calculation_reason_codes)
    if spy_price_inputs is None:
        calculation_reason_codes.add(_SPY_BENCHMARK_PRICE_UNAVAILABLE)
    if qqq_price_inputs is None:
        calculation_reason_codes.add(_QQQ_BENCHMARK_PRICE_UNAVAILABLE)
    required_policy_tickers = {ticker for ticker, weight in policy_weights.items() if weight > 0}
    if required_policy_tickers and set(policy_price_inputs) != required_policy_tickers:
        calculation_reason_codes.add(_POLICY_BENCHMARK_PRICE_UNAVAILABLE)
    if not source_coverage.is_complete:
        calculation_reason_codes.add(_EXTERNAL_FLOW_SOURCE_COVERAGE_INCOMPLETE)
    if (
        not cashflow_assessment.calculation_reason_codes
        and not modified_dietz_denominators_are_positive(
            sorted_dates, daily_cashflow, base_value_adj
        )
    ):
        calculation_reason_codes.add(_NONPOSITIVE_DIETZ_DENOMINATOR)

    spy_equivalent = (
        _money_flow_matched_value(
            sorted_dates, base_value_adj, daily_cashflow, benchmark_series.get("SPY", {})
        )
        if spy_price_inputs is not None
        else {}
    )
    qqq_equivalent = (
        _money_flow_matched_value(
            sorted_dates, base_value_adj, daily_cashflow, benchmark_series.get("QQQ", {})
        )
        if qqq_price_inputs is not None
        else {}
    )
    policy_equivalent = (
        _policy_matched_value(
            sorted_dates, base_value_adj, daily_cashflow, benchmark_series, policy_weights
        )
        if policy_weights and not (required_policy_tickers - set(policy_price_inputs))
        else {}
    )

    if calculation_reason_codes:
        points = [
            PerformancePoint(
                date=current_date,
                portfolio_value=daily_value_adj[current_date],
                portfolio_return_pct=None,
                spy_return_pct=None,
                qqq_return_pct=None,
                policy_return_pct=None,
                spy_equivalent_value=None,
                qqq_equivalent_value=None,
                policy_equivalent_value=None,
            )
            for current_date in sorted_dates
        ]
        return PerformanceSeries(
            methodology="performance.modified_dietz",
            methodology_version="2",
            calculation_status="unavailable",
            reconstruction_certification="unavailable",
            calculation_reason_codes=sorted(calculation_reason_codes),
            start_date=start_date,
            end_date=end_date,
            base_value=base_value_adj,
            points=points,
            earliest_observed_date=_earliest_observed_date(session, start_date, end_date),
            net_external_cashflow_in=None,
            source_coverage=source_coverage_read,
            backfill_start_unreliable=(
                opening_provenance != _OBSERVED_VALUE
                or _is_start_value_unreliable(base_value, end_value)
            ),
            opening_value_provenance=(
                opening_provenance
                if opening_provenance in {_OBSERVED_VALUE, _MODELED_VALUE}
                else None
            ),
            ending_value_provenance=(
                ending_provenance
                if ending_provenance in {_OBSERVED_VALUE, _MODELED_VALUE}
                else None
            ),
            valuation_account_ids=list(value_assessment.valuation_account_ids),
            equation_receipt=None,
        )

    portfolio_returns = modified_dietz_series(
        sorted_dates, daily_value_adj, daily_cashflow, base_value_adj
    )
    spy_returns = (
        modified_dietz_series(sorted_dates, spy_equivalent, daily_cashflow, base_value_adj)
        if spy_equivalent
        else {}
    )
    qqq_returns = (
        modified_dietz_series(sorted_dates, qqq_equivalent, daily_cashflow, base_value_adj)
        if qqq_equivalent
        else {}
    )
    policy_returns = (
        modified_dietz_series(sorted_dates, policy_equivalent, daily_cashflow, base_value_adj)
        if policy_equivalent
        else {}
    )

    points: list[PerformancePoint] = []
    for current_date in sorted_dates:
        points.append(
            PerformancePoint(
                date=current_date,
                portfolio_value=daily_value_adj[current_date],
                portfolio_return_pct=portfolio_returns[current_date],
                spy_return_pct=spy_returns.get(current_date),
                qqq_return_pct=qqq_returns.get(current_date),
                policy_return_pct=policy_returns.get(current_date),
                spy_equivalent_value=spy_equivalent.get(current_date),
                qqq_equivalent_value=qqq_equivalent.get(current_date),
                policy_equivalent_value=policy_equivalent.get(current_date),
            )
        )

    assert spy_price_inputs is not None
    assert qqq_price_inputs is not None
    equation_receipt = _build_equation_receipt(
        start_date=start_date,
        end_date=end_date,
        sorted_dates=sorted_dates,
        daily_value=daily_value_adj,
        daily_cashflow=daily_cashflow,
        base_value=base_value_adj,
        portfolio_return_pct=portfolio_returns[end_date],
        spy_equivalent=spy_equivalent,
        spy_return_pct=spy_returns[end_date],
        spy_price_inputs=spy_price_inputs,
        qqq_equivalent=qqq_equivalent,
        qqq_return_pct=qqq_returns[end_date],
        qqq_price_inputs=qqq_price_inputs,
        policy_equivalent=policy_equivalent,
        policy_return_pct=policy_returns.get(end_date),
        policy_price_inputs=policy_price_inputs,
        cashflow_assessment=cashflow_assessment,
        benchmark_return_basis=benchmark_return_basis,
        reserve=reserve,
        exclude_index_etfs=exclude_index_etfs,
    )

    return PerformanceSeries(
        methodology="performance.modified_dietz",
        methodology_version="2",
        calculation_status="available",
        reconstruction_certification=(
            "observed_certified" if opening_provenance == _OBSERVED_VALUE else "modeled_provisional"
        ),
        calculation_reason_codes=[],
        start_date=start_date,
        end_date=end_date,
        base_value=base_value_adj,
        points=points,
        earliest_observed_date=_earliest_observed_date(session, start_date, end_date),
        net_external_cashflow_in=sum(daily_cashflow.values(), Decimal(0)),
        source_coverage=source_coverage_read,
        backfill_start_unreliable=(
            opening_provenance != _OBSERVED_VALUE
            or _is_start_value_unreliable(base_value, end_value)
        ),
        opening_value_provenance=opening_provenance,
        ending_value_provenance=ending_provenance,
        valuation_account_ids=list(value_assessment.valuation_account_ids),
        equation_receipt=equation_receipt,
    )


def _stable_input_id(namespace: str, rows: Iterable[tuple[object, ...]]) -> str:
    """Return a deterministic opaque identifier without exposing input data."""
    digest = sha256(namespace.encode("utf-8"))
    for row in rows:
        digest.update(b"\x1e")
        for value in row:
            encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"


def _price_input_id(
    benchmark: str,
    resolved_prices: dict[date, tuple[date, Decimal]],
    basis_by_date: dict[date, str] | None = None,
) -> str:
    return _stable_input_id(
        f"performance-benchmark-price:{benchmark}",
        (
            (
                target_date,
                source_date,
                price,
                (basis_by_date or {}).get(source_date, "raw_price_fallback"),
            )
            for target_date, (source_date, price) in sorted(resolved_prices.items())
        ),
    )


def _price_input_rows(
    ticker: str,
    resolved_prices: dict[date, tuple[date, Decimal]],
    basis_by_date: dict[date, str] | None = None,
) -> list[PerformanceBenchmarkPriceInput]:
    return [
        PerformanceBenchmarkPriceInput(
            ticker=ticker,
            target_date=target_date,
            source_date=source_date,
            close=price,
            resolution=(
                "same_day_close" if target_date == source_date else "previous_market_close"
            ),
            return_basis=(
                "total_return_adjusted"
                if (basis_by_date or {}).get(source_date) == "total_return_adjusted"
                else "raw_price_fallback"
            ),
        )
        for target_date, (source_date, price) in sorted(resolved_prices.items())
    ]


def _build_benchmark_equation(
    *,
    benchmark: str,
    ending_value: Decimal,
    opening_value: Decimal,
    net_external_cashflow: Decimal,
    portfolio_gain: Decimal,
    portfolio_return_pct: Decimal,
    return_pct: Decimal,
    price_input_id: str,
    price_inputs: list[PerformanceBenchmarkPriceInput],
) -> PerformanceBenchmarkEquation:
    investment_gain = ending_value - opening_value - net_external_cashflow
    return PerformanceBenchmarkEquation(
        benchmark=benchmark,
        ending_value=ending_value,
        investment_gain=investment_gain,
        return_pct=return_pct,
        dollar_alpha=portfolio_gain - investment_gain,
        percentage_point_alpha=portfolio_return_pct - return_pct,
        equation_residual=(ending_value - opening_value - net_external_cashflow - investment_gain),
        price_input_id=price_input_id,
        price_inputs=price_inputs,
    )


def _build_equation_receipt(
    *,
    start_date: date,
    end_date: date,
    sorted_dates: list[date],
    daily_value: dict[date, Decimal],
    daily_cashflow: dict[date, Decimal],
    base_value: Decimal,
    portfolio_return_pct: Decimal,
    spy_equivalent: dict[date, Decimal],
    spy_return_pct: Decimal,
    spy_price_inputs: dict[date, tuple[date, Decimal]],
    qqq_equivalent: dict[date, Decimal],
    qqq_return_pct: Decimal,
    qqq_price_inputs: dict[date, tuple[date, Decimal]],
    policy_equivalent: dict[date, Decimal],
    policy_return_pct: Decimal | None,
    policy_price_inputs: dict[str, dict[date, tuple[date, Decimal]]],
    cashflow_assessment: _CashflowAssessment,
    benchmark_return_basis: dict[str, dict[date, str]],
    reserve: Decimal,
    exclude_index_etfs: bool,
) -> PerformanceEquationReceipt:
    """Freeze every headline value and its input lineage into one response."""
    net_external_cashflow = sum(daily_cashflow.values(), Decimal(0))
    ending_value = daily_value[end_date]
    portfolio_gain = ending_value - base_value - net_external_cashflow
    denominator = _modified_dietz_denominator_at(start_date, end_date, daily_cashflow, base_value)
    portfolio_value_input_id = _stable_input_id(
        "performance-portfolio-valuations",
        ((value_date, daily_value[value_date]) for value_date in sorted_dates),
    )
    flow_ledger_id = _stable_input_id(
        "performance-external-flow-window",
        [
            (
                cashflow_assessment.external_flow_ledger_id or "derived-daily-flow-projection",
                reserve,
                exclude_index_etfs,
            ),
            *((flow_date, amount) for flow_date, amount in sorted(daily_cashflow.items())),
        ],
    )
    spy = _build_benchmark_equation(
        benchmark="SPY",
        ending_value=spy_equivalent[end_date],
        opening_value=base_value,
        net_external_cashflow=net_external_cashflow,
        portfolio_gain=portfolio_gain,
        portfolio_return_pct=portfolio_return_pct,
        return_pct=spy_return_pct,
        price_input_id=_price_input_id(
            "SPY", spy_price_inputs, benchmark_return_basis.get("SPY", {})
        ),
        price_inputs=_price_input_rows(
            "SPY", spy_price_inputs, benchmark_return_basis.get("SPY", {})
        ),
    )
    qqq = _build_benchmark_equation(
        benchmark="QQQ",
        ending_value=qqq_equivalent[end_date],
        opening_value=base_value,
        net_external_cashflow=net_external_cashflow,
        portfolio_gain=portfolio_gain,
        portfolio_return_pct=portfolio_return_pct,
        return_pct=qqq_return_pct,
        price_input_id=_price_input_id(
            "QQQ", qqq_price_inputs, benchmark_return_basis.get("QQQ", {})
        ),
        price_inputs=_price_input_rows(
            "QQQ", qqq_price_inputs, benchmark_return_basis.get("QQQ", {})
        ),
    )
    policy: PerformanceBenchmarkEquation | None = None
    if policy_equivalent and policy_return_pct is not None:
        policy_input_id = _stable_input_id(
            "performance-benchmark-price:policy",
            (
                (
                    ticker,
                    target_date,
                    source_date,
                    price,
                    benchmark_return_basis.get(ticker, {}).get(source_date, "raw_price_fallback"),
                )
                for ticker, resolved in sorted(policy_price_inputs.items())
                for target_date, (source_date, price) in sorted(resolved.items())
            ),
        )
        policy = _build_benchmark_equation(
            benchmark="policy",
            ending_value=policy_equivalent[end_date],
            opening_value=base_value,
            net_external_cashflow=net_external_cashflow,
            portfolio_gain=portfolio_gain,
            portfolio_return_pct=portfolio_return_pct,
            return_pct=policy_return_pct,
            price_input_id=policy_input_id,
            price_inputs=[
                row
                for ticker, resolved in sorted(policy_price_inputs.items())
                for row in _price_input_rows(
                    ticker, resolved, benchmark_return_basis.get(ticker, {})
                )
            ],
        )
    operative_flows = [
        PerformanceOperativeCashflow(
            flow_id=entry.flow_id,
            date=entry.date,
            amount=entry.signed_external_amount,
            transaction_id=entry.transaction_id,
            transaction_origin=entry.transaction_origin,
            source_event_ids=list(entry.source_event_ids),
            source_attestation_keys=list(entry.source_attestation_keys),
            active_decision_keys=list(entry.active_decision_keys),
            decision_authorities=list(entry.decision_authorities),
            decision_confidences=list(entry.decision_confidences),
            assumption_codes=list(entry.assumption_codes),
            effective_date_bases=list(entry.effective_date_bases),
        )
        for entry in sorted(cashflow_assessment.entries, key=lambda item: (item.date, item.flow_id))
        if entry.classification != "internal" and entry.signed_external_amount != 0
    ]
    operative_by_date: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for flow in operative_flows:
        operative_by_date[flow.date] += flow.amount
    for flow_date, amount in sorted(daily_cashflow.items()):
        residual = amount - operative_by_date[flow_date]
        if residual == 0:
            continue
        operative_flows.append(
            PerformanceOperativeCashflow(
                flow_id=f"calculation-adjustment:index-etf-carve:{flow_date.isoformat()}",
                date=flow_date,
                amount=residual,
                transaction_id=None,
                transaction_origin="calculation_adjustment",
                source_event_ids=[],
                source_attestation_keys=[],
                active_decision_keys=[],
                decision_authorities=[],
                decision_confidences=[],
                assumption_codes=["exclude_index_etfs_internal_flow"],
                effective_date_bases=["provider_posting"],
            )
        )
    flow_ids_by_date: dict[date, list[str]] = defaultdict(list)
    for flow in operative_flows:
        flow_ids_by_date[flow.date].append(flow.flow_id)
    calculation_id = _stable_input_id(
        "performance-equation-receipt-v2",
        [
            (
                start_date,
                end_date,
                flow_ledger_id,
                portfolio_value_input_id,
                spy.price_input_id,
                qqq.price_input_id,
                policy.price_input_id if policy is not None else "no-policy",
                base_value,
                net_external_cashflow,
                ending_value,
                portfolio_gain,
                denominator,
                portfolio_return_pct,
            )
        ],
    )
    return PerformanceEquationReceipt(
        calculation_id=calculation_id,
        external_flow_ledger_id=flow_ledger_id,
        portfolio_valuation_input_id=portfolio_value_input_id,
        included_account_ids=list(cashflow_assessment.account_ids),
        requested_start_date=start_date,
        requested_end_date=end_date,
        benchmark_price_resolution_policy="same_day_or_previous_us_market_close",
        opening_value=base_value,
        dated_external_cashflows=[
            PerformanceDatedCashflow(
                date=flow_date,
                amount=amount,
                flow_ids=sorted(flow_ids_by_date[flow_date]),
            )
            for flow_date, amount in sorted(daily_cashflow.items())
        ],
        operative_external_cashflows=operative_flows,
        net_external_cashflow_in=net_external_cashflow,
        ending_value=ending_value,
        investment_gain=portfolio_gain,
        modified_dietz_denominator=denominator,
        portfolio_return_pct=portfolio_return_pct,
        portfolio_equation_residual=(
            ending_value - base_value - net_external_cashflow - portfolio_gain
        ),
        spy=spy,
        qqq=qqq,
        policy=policy,
    )


def _broad_index_security_ids(session: Session) -> frozenset[int]:
    """Resolve `_BROAD_INDEX_ETF_TICKERS` to a set of `security_id`s.

    A single ticker can have multiple `security_id`s in our DB when the
    same fund was ingested via different aggregators (e.g., Plaid vs
    SnapTrade) and ended up with slight metadata differences. We want the
    union of all of them.
    """
    rows = (
        session.execute(
            select(Security.security_id).where(Security.ticker.in_(_BROAD_INDEX_ETF_TICKERS))
        )
        .scalars()
        .all()
    )
    return frozenset(rows)


def _daily_subset_value(
    session: Session,
    start_date: date,
    end_date: date,
    security_ids: frozenset[int],
) -> dict[date, Decimal]:
    """Daily total value of just the listed securities (no cash adjustment).

    Forward dates use `holdings_snapshots` filtered to the subset. Earlier
    dates walk transactions backward, anchored on the first snapshot,
    tracking only the subset's positions. Used to extract the
    "broad-index ETF" portion of V for the active-equity carve-out.
    """
    if not security_ids:
        return {}

    forward = _forward_subset_values(session, start_date, end_date, security_ids)
    earliest_known = min(forward.keys()) if forward else None
    if earliest_known is None or earliest_known > start_date:
        backfill_end = (
            earliest_known - timedelta(days=1) if earliest_known is not None else end_date
        )
        backfill = _backfill_subset_values(session, start_date, backfill_end, security_ids)
        for d, v in backfill.items():
            forward.setdefault(d, v)
    return forward


def _forward_subset_values(
    session: Session,
    start_date: date,
    end_date: date,
    security_ids: frozenset[int],
) -> dict[date, Decimal]:
    accts = valued_account_ids(session)
    if not accts:
        return {}
    rows = session.execute(
        select(
            HoldingSnapshot.snapshot_date,
            HoldingSnapshot.institution_value,
            HoldingSnapshot.quantity,
            HoldingSnapshot.institution_price,
        )
        .where(HoldingSnapshot.snapshot_date >= start_date)
        .where(HoldingSnapshot.snapshot_date <= end_date)
        .where(HoldingSnapshot.security_id.in_(security_ids))
        .where(HoldingSnapshot.account_id.in_(accts))
    ).all()
    totals: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for snap_date, value, quantity, price in rows:
        if value is not None:
            totals[snap_date] += Decimal(value)
        elif price is not None:
            totals[snap_date] += Decimal(quantity) * Decimal(price)
    return dict(totals)


def _backfill_subset_values(
    session: Session,
    start_date: date,
    end_date: date,
    security_ids: frozenset[int],
) -> dict[date, Decimal]:
    """Walk transactions backward, tracking ONLY the subset's positions.

    No cash adjustment is applied here — the subset's V is just `qty *
    price` summed across the listed securities, which is what we want for
    "value of the index-ETF portion."
    """
    accts = valued_account_ids(session)
    if not accts:
        return {}
    anchor_date = session.execute(
        select(HoldingSnapshot.snapshot_date)
        .where(HoldingSnapshot.account_id.in_(accts))
        .order_by(HoldingSnapshot.snapshot_date.asc())
        .limit(1)
    ).scalar_one_or_none()
    if anchor_date is None:
        return {}

    rows = session.execute(
        select(HoldingSnapshot.security_id, HoldingSnapshot.quantity)
        .where(HoldingSnapshot.snapshot_date == anchor_date)
        .where(HoldingSnapshot.security_id.in_(security_ids))
        .where(HoldingSnapshot.account_id.in_(accts))
    ).all()
    rolling: dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    for sid, qty in rows:
        rolling[sid] += Decimal(qty)

    backward_tx = (
        session.execute(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.security_id.in_(security_ids))
            .where(InvestmentTransaction.account_id.in_(accts))
            .where(InvestmentTransaction.date < anchor_date)
            .where(InvestmentTransaction.date >= start_date)
            .order_by(InvestmentTransaction.date.desc())
        )
        .scalars()
        .all()
    )

    daily_quantities: dict[date, dict[int, Decimal]] = {}
    cursor_date = anchor_date - timedelta(days=1)
    daily_quantities[cursor_date] = dict(rolling)
    for tx in backward_tx:
        while cursor_date > tx.date:
            cursor_date -= timedelta(days=1)
            daily_quantities[cursor_date] = dict(rolling)
        if tx.security_id is None:
            continue
        delta = _reverse_transaction_quantity(tx)
        if delta is not None:
            rolling[tx.security_id] = rolling.get(tx.security_id, Decimal(0)) + delta
    while cursor_date > start_date:
        cursor_date -= timedelta(days=1)
        daily_quantities[cursor_date] = dict(rolling)

    return _value_subset_with_prices(session, daily_quantities, security_ids, start_date, end_date)


def _value_subset_with_prices(
    session: Session,
    daily_quantities: dict[date, dict[int, Decimal]],
    security_ids: frozenset[int],
    start_date: date,
    end_date: date,
) -> dict[date, Decimal]:
    """Mark-to-market valuation for the subset. No `snapshot_price`
    fallback (the listed securities are liquid index ETFs with full price
    coverage; if a price is missing on a given day we skip rather than
    fabricate)."""
    relevant_dates = [d for d in daily_quantities if start_date <= d <= end_date]
    if not relevant_dates:
        return {}

    securities = (
        session.execute(select(Security).where(Security.security_id.in_(security_ids)))
        .scalars()
        .all()
    )
    sec_meta: dict[int, Security] = {s.security_id: s for s in securities}

    price_rows = session.execute(
        select(Price.security_id, Price.date, Price.close)
        .where(Price.security_id.in_(security_ids))
        .where(Price.date >= start_date - timedelta(days=14))
        .where(Price.date <= end_date)
    ).all()
    price_lookup: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for security_id, price_date, close in price_rows:
        price_lookup[security_id][price_date] = Decimal(close)

    totals: dict[date, Decimal] = {}
    for current_date in sorted(relevant_dates):
        snap = daily_quantities[current_date]
        total = Decimal(0)
        for security_id, quantity in snap.items():
            sec = sec_meta.get(security_id)
            if sec is not None and sec.is_cash_equivalent:
                total += quantity * Decimal(1)
                continue
            close = _last_known_price(price_lookup.get(security_id, {}), current_date)
            if close is None:
                continue
            total += quantity * close
        totals[current_date] = total
    return totals


def _daily_internal_index_cashflows(
    session: Session,
    start_date: date,
    end_date: date,
    security_ids: frozenset[int],
) -> dict[date, Decimal]:
    """Internal flows from buying / selling broad-index ETFs.

    From the *active-portion* point of view, buying VTI is an OUTFLOW
    (cash leaves active to fund the index purchase) and selling VTI is
    an INFLOW (cash from the index sale lands in active). Including these
    in the cashflow series lets the synthetic benchmark match exactly the
    money the active portion actually had to work with on each day, so
    the comparison is fair.

    Sign convention follows `_reverse_transaction_cash_delta` — derive
    direction from the transaction TYPE, not from amount sign, because
    Plaid and SnapTrade disagree.
    """
    if not security_ids:
        return {}
    accts = valued_account_ids(session)
    if not accts:
        return {}
    rows = (
        session.execute(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.security_id.in_(security_ids))
            .where(InvestmentTransaction.account_id.in_(accts))
            .where(InvestmentTransaction.date >= start_date)
            .where(InvestmentTransaction.date <= end_date)
        )
        .scalars()
        .all()
    )
    totals: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for tx in rows:
        magnitude = abs(Decimal(tx.amount))
        # SELL of an index → cash returns to the active bucket (inflow).
        # BUY of an index → cash leaves the active bucket (outflow).
        if tx.type == InvestmentTransactionType.SELL.value:
            totals[tx.date] += magnitude
        elif tx.type == InvestmentTransactionType.BUY.value:
            totals[tx.date] -= magnitude
    return dict(totals)


def _policy_matched_value(
    sorted_dates: list[date],
    base_value: Decimal,
    daily_cashflow: dict[date, Decimal],
    benchmark_series: dict[str, dict[date, Decimal]],
    weights: dict[str, Decimal],
) -> dict[date, Decimal]:
    """Synthetic value of a multi-ticker policy portfolio over time.

    Same logic as `_money_flow_matched_value` but for a basket: every
    purchase (initial + each cashflow after the end-of-day opening value)
    is split across the policy tickers in their target weights. Cashflow
    dates need not also be displayed valuation dates. Each lot is valued
    at today's prices for every component.

    The public performance path validates every configured component at every
    displayed and flow-deployment date before calling this helper. The local
    renormalization remains defensive for private callers, but an incomplete
    policy basket is never returned as an available whole-portfolio result.
    """
    if not sorted_dates or not weights:
        return {}
    start_date = sorted_dates[0]

    # Initial lot at start_date: split base_value across priceable tickers,
    # renormalized to sum to 1.
    initial_lot = _build_lot_renormalized(base_value, weights, benchmark_series, start_date)
    if not initial_lot:
        return {}
    lots: list[dict[str, Decimal]] = [initial_lot]
    cashflow_events = sorted(
        _cashflows_within_valuation_period(sorted_dates, daily_cashflow).items()
    )
    cashflow_index = 0

    out: dict[date, Decimal] = {}
    for current_date in sorted_dates:
        while (
            cashflow_index < len(cashflow_events)
            and cashflow_events[cashflow_index][0] <= current_date
        ):
            cashflow_date, cf = cashflow_events[cashflow_index]
            cf_lot = _build_lot_renormalized(cf, weights, benchmark_series, cashflow_date)
            if not cf_lot:
                return {}
            lots.append(cf_lot)
            cashflow_index += 1

        # Value all lots at today's prices for every component.
        total = Decimal(0)
        for lot in lots:
            for ticker, qty in lot.items():
                price_today = _benchmark_price(benchmark_series.get(ticker, {}), current_date)
                if price_today is None:
                    continue
                total += qty * price_today
        if total > 0:
            out[current_date] = total
    return out


def _build_lot_renormalized(
    capital: Decimal,
    weights: dict[str, Decimal],
    benchmark_series: dict[str, dict[date, Decimal]],
    on_date: date,
) -> dict[str, Decimal]:
    """Split `capital` across policy tickers that have a price on `on_date`,
    renormalizing surviving weights to sum to 1 so the full capital lands.

    Returns `{ticker: shares}`. Empty dict if no policy ticker is priceable
    on the date — caller decides whether to skip the lot or fail loudly.
    """
    priceable: list[tuple[str, Decimal, Decimal]] = []
    surviving_weight = Decimal(0)
    for ticker, weight in weights.items():
        closes = benchmark_series.get(ticker, {})
        price = _benchmark_price(closes, on_date)
        if price is None:
            continue
        priceable.append((ticker, weight, price))
        surviving_weight += weight
    if not priceable or surviving_weight == 0:
        return {}
    lot: dict[str, Decimal] = {}
    for ticker, weight, price in priceable:
        renormed_weight = weight / surviving_weight
        lot[ticker] = (capital * renormed_weight) / price
    return lot


def _money_flow_matched_value(
    sorted_dates: list[date],
    base_value: Decimal,
    daily_cashflow: dict[date, Decimal],
    benchmark_closes: dict[date, Decimal],
) -> dict[date, Decimal]:
    """Build a synthetic value series: V_start invested in the benchmark at
    `sorted_dates[0]`, plus each subsequent daily cashflow `cf` invested in
    the benchmark at that day's close, all valued at the benchmark's close
    on each day in `sorted_dates`. A cashflow date does not need to be a
    displayed valuation date.

    Returns an empty dict when the benchmark series doesn't cover the start
    (we can't anchor the synthetic portfolio without a base price).
    """
    if not sorted_dates:
        return {}
    start_date = sorted_dates[0]
    if any(
        _benchmark_price(benchmark_closes, current_date) is None for current_date in sorted_dates
    ):
        return {}
    base_price = _benchmark_price(benchmark_closes, start_date)
    if base_price is None:
        return {}

    # Each "lot" in the synthetic portfolio: a quantity of benchmark shares
    # purchased at the lot's date. Today's value = sum(qty * price_today).
    initial_shares = base_value / base_price
    lots: list[tuple[date, Decimal]] = [(start_date, initial_shares)]
    cashflow_events = sorted(
        _cashflows_within_valuation_period(sorted_dates, daily_cashflow).items()
    )
    cashflow_index = 0

    out: dict[date, Decimal] = {}
    for current_date in sorted_dates:
        while (
            cashflow_index < len(cashflow_events)
            and cashflow_events[cashflow_index][0] <= current_date
        ):
            cashflow_date, cf = cashflow_events[cashflow_index]
            cf_price = _benchmark_price(benchmark_closes, cashflow_date)
            if cf_price is None:
                return {}
            lots.append((cashflow_date, cf / cf_price))
            cashflow_index += 1
        price_today = _benchmark_price(benchmark_closes, current_date)
        if price_today is None:
            continue
        total_shares = sum((qty for _, qty in lots), Decimal(0))
        out[current_date] = total_shares * price_today
    return out


def _cashflows_within_valuation_period(
    sorted_dates: list[date], daily_cashflow: dict[date, Decimal]
) -> dict[date, Decimal]:
    """Return the canonical end-of-day cashflow set for a valuation window.

    The first value is an end-of-day opening balance, so same-day flows are
    already inside it. The ending value includes activity through its date.
    Restricting once to ``(first valuation date, last valuation date]`` keeps
    the actual return, benchmark lot walks, and reported net flow reconciled.
    """
    if not sorted_dates:
        return {}
    first_date = sorted_dates[0]
    last_date = sorted_dates[-1]
    return {
        flow_date: amount
        for flow_date, amount in daily_cashflow.items()
        if first_date < flow_date <= last_date and amount != 0
    }


def modified_dietz_series(
    sorted_dates: list[date],
    daily_value: dict[date, Decimal],
    daily_cashflow: dict[date, Decimal],
    base_value: Decimal,
) -> dict[date, Decimal]:
    """Cumulative Modified Dietz return % at each date.

    Standard formula:
        R = (V_end - V_start - C_total) / (V_start + sum(C_i * w_i))
    where w_i = (period_end - cashflow_date) / period_length. We compute it
    cumulatively: at each `current_date`, treat that as the period end.

    Returns Decimal 0 (not %) on the start date. End-of-day percentages
    expressed as the multiplier × 100.
    """
    if not sorted_dates:
        return {}
    out: dict[date, Decimal] = {sorted_dates[0]: Decimal(0)}
    start_date = sorted_dates[0]

    sorted_cashflows = sorted(
        ((d, c) for d, c in daily_cashflow.items() if c != 0 and d > start_date),
        key=lambda x: x[0],
    )

    for current_date in sorted_dates[1:]:
        period_days = (current_date - start_date).days
        if period_days <= 0:
            out[current_date] = Decimal(0)
            continue
        v_now = daily_value.get(current_date)
        if v_now is None:
            continue

        cumulative_cf = Decimal(0)
        weighted_cf = Decimal(0)
        for cf_date, cf_amount in sorted_cashflows:
            if cf_date > current_date:
                break
            cumulative_cf += cf_amount
            weight = Decimal((current_date - cf_date).days) / Decimal(period_days)
            weighted_cf += cf_amount * weight

        denominator = base_value + weighted_cf
        if denominator <= 0:
            out[current_date] = Decimal(0)
            continue
        numerator = v_now - base_value - cumulative_cf
        out[current_date] = (numerator / denominator) * Decimal(100)
    return out


def modified_dietz_denominators_are_positive(
    sorted_dates: list[date],
    daily_cashflow: dict[date, Decimal],
    base_value: Decimal,
) -> bool:
    """Whether every cumulative Modified-Dietz denominator is defined."""
    if not sorted_dates or base_value <= 0:
        return False
    start_date = sorted_dates[0]
    cashflows = sorted(
        ((d, c) for d, c in daily_cashflow.items() if c != 0 and d > start_date),
        key=lambda item: item[0],
    )
    for current_date in sorted_dates[1:]:
        if (
            _modified_dietz_denominator_at(start_date, current_date, dict(cashflows), base_value)
            <= 0
        ):
            return False
    return True


def _modified_dietz_denominator_at(
    start_date: date,
    end_date: date,
    daily_cashflow: dict[date, Decimal],
    base_value: Decimal,
) -> Decimal:
    """Exact shared capital base for one cumulative Modified-Dietz window."""
    period_days = (end_date - start_date).days
    if period_days <= 0:
        return Decimal(0)
    weighted_cashflow = sum(
        (
            amount * (Decimal((end_date - flow_date).days) / Decimal(period_days))
            for flow_date, amount in sorted(daily_cashflow.items())
            if start_date < flow_date <= end_date and amount != 0
        ),
        Decimal(0),
    )
    return base_value + weighted_cashflow


# Backward-compatible private name for older internal callers and focused unit
# tests. New cross-service consumers use the public pure-math helper above.
_modified_dietz_series = modified_dietz_series


def _earliest_observed_date(session: Session, start_date: date, end_date: date) -> date | None:
    """First date in the window with complete valued-account snapshots.

    A partial broker sync is not observed portfolio value. Earlier values (if
    any) come from transaction-walk reconstruction. Returns None if no complete
    full-book snapshot lives in the window.
    """
    complete = _complete_snapshot_dates(session, _snapshot_dates(session, start_date, end_date))
    return min(complete) if complete else None


def _is_start_value_unreliable(base_value: Decimal, end_value: Decimal) -> bool:
    """Heuristic: backfilled start is suspect when far below the end value.

    The backfill reconstructs positions but not cash, so positions bought
    during the window with pre-existing cash get netted out — collapsing
    the reconstructed start. When the start is < 25% of end, the resulting
    TWR is dominated by reconstruction noise.
    """
    if base_value <= 0 or end_value <= 0:
        return True
    return (base_value / end_value) < Decimal("0.25")


def _daily_external_cashflow_assessment(
    session: Session, start_date: date, end_date: date
) -> _CashflowAssessment:
    """Project the canonical ledger into the performance calculation contract."""
    ledger = external_flow_ledger.build_external_flow_ledger(session, start_date, end_date)
    issue_reason_codes = {
        "share_transfer_missing_security": _EXTERNAL_SHARE_MOVEMENT_MISSING_SECURITY,
        "share_transfer_missing_ticker": _EXTERNAL_SHARE_MOVEMENT_MISSING_TICKER,
        "share_transfer_price_unavailable": _EXTERNAL_SHARE_MOVEMENT_PRICE_UNAVAILABLE,
    }
    reason_codes = tuple(
        sorted(
            {
                issue_reason_codes.get(issue.code, f"external_flow_{issue.code}")
                for issue in ledger.issues
            }
        )
    )
    ledger_input_id = _stable_input_id(
        "performance-external-flow-ledger",
        [
            (ledger.start_date, ledger.end_date, *sorted(ledger.account_ids)),
            *(
                (
                    entry.flow_id,
                    entry.date,
                    entry.signed_external_amount,
                    entry.classification,
                    entry.classification_source,
                    entry.classification_rule,
                    entry.valuation_price,
                    entry.valuation_price_date,
                    entry.transaction_origin,
                    *entry.source_event_ids,
                    *entry.active_decision_keys,
                )
                for entry in sorted(ledger.entries, key=lambda item: (item.date, item.flow_id))
            ),
        ],
    )
    return _CashflowAssessment(
        cashflows=(ledger.daily_external_cashflows if not reason_codes else {}),
        calculation_reason_codes=reason_codes,
        external_flow_ledger_id=ledger_input_id,
        account_ids=tuple(sorted(ledger.account_ids)),
        entries=ledger.entries,
    )


def _legacy_daily_external_cashflow_assessment(  # pyright: ignore[reportUnusedFunction]
    session: Session, start_date: date, end_date: date
) -> _CashflowAssessment:
    """Sum signed external cashflows per day (positive = INTO portfolio).

    Direction handling:
      * For unambiguously-named `cash` subtypes (`contribution`, `deposit`,
        `withdrawal`), the NAME determines direction and we use abs(amount).
        Brokers report the `amount` sign inconsistently for these — some
        from the cash account's perspective, some from the investor's.
      * For ambiguous subtypes (`cash/transfer` and the bare `transfer`
        type), we trust Plaid's standard sign convention: negative amount
        = cash going INTO the account = inflow, so cashflow_in = -amount.
    """
    accts = valued_account_ids(session)
    if not accts:
        return _CashflowAssessment(cashflows={}, calculation_reason_codes=())
    rows = session.execute(
        select(
            InvestmentTransaction.plaid_investment_transaction_id,
            InvestmentTransaction.date,
            InvestmentTransaction.amount,
            InvestmentTransaction.type,
            InvestmentTransaction.subtype,
            InvestmentTransaction.name,
        )
        .where(InvestmentTransaction.date >= start_date)
        .where(InvestmentTransaction.date <= end_date)
        .where(InvestmentTransaction.account_id.in_(accts))
    ).all()

    overrides = _load_transaction_overrides(session)
    totals: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for tx_id, tx_date, amount, tx_type, tx_subtype, tx_name in rows:
        cashflow_in = _signed_cashflow(
            tx_type,
            tx_subtype,
            Decimal(amount),
            override=overrides.get(tx_id),
            name=tx_name,
        )
        if cashflow_in == 0:
            continue
        totals[tx_date] += cashflow_in

    # Plus: net dollar value of share-side ACATS in/out events that have
    # no matching counter-event in our linked accounts. SnapTrade emits
    # `cash/external_asset_transfer_in/out` with amount=$0 (the
    # counterparty doesn't tell us a USD value), so we have to compute
    # it ourselves at the close price on the transfer date. We then
    # treat the net as an external cashflow for TWR purposes — same
    # as a deposit / withdrawal — because from our portfolio's frame of
    # reference, value entered or left without a market gain/loss.
    #
    # Matching logic: an in/out pair with same (date, ticker, abs(qty))
    # netted to zero means an internal move between two of the user's
    # linked accounts; we skip those. Anything left over is a true
    # external flow.
    transfer_assessment = _assess_share_transfer_external_cashflows(
        session, start_date, end_date, accts
    )
    for d, c in transfer_assessment.cashflows.items():
        totals[d] = totals.get(d, Decimal(0)) + c

    return _CashflowAssessment(
        cashflows=dict(totals),
        calculation_reason_codes=transfer_assessment.calculation_reason_codes,
    )


def _daily_external_cashflows(  # pyright: ignore[reportUnusedFunction]
    session: Session, start_date: date, end_date: date
) -> dict[date, Decimal]:
    """Compatibility/raw-fact view of all deterministically known cashflows."""
    return _daily_external_cashflow_assessment(session, start_date, end_date).cashflows


def _share_transfer_external_cashflows(  # pyright: ignore[reportUnusedFunction]
    session: Session,
    start_date: date,
    end_date: date,
    accts: frozenset[int],
) -> dict[date, Decimal]:
    """Compatibility view of deterministically valued in-kind cashflows."""
    return _assess_share_transfer_external_cashflows(session, start_date, end_date, accts).cashflows


def _assess_share_transfer_external_cashflows(
    session: Session,
    start_date: date,
    end_date: date,
    accts: frozenset[int],
) -> _CashflowAssessment:
    """Net dollar-valued cashflow from unmatched share-side ACATS events.

    For each (date, normalized ticker), sum split-normalized quantities for
    zero-dollar plain TRANSFER rows and `cash/external_asset_transfer_*`
    events. Net != 0 means shares crossed the tracked portfolio boundary.

    We value at close price on the transfer date and contribute the
    signed dollar to the cashflow series:
      net_qty > 0 (more in than out) → inflow → cf += +value
      net_qty < 0 (more out than in) → outflow → cf += -value
    """
    transactions = list(
        session.execute(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.date >= start_date)
            .where(InvestmentTransaction.date <= end_date)
            .where(InvestmentTransaction.account_id.in_(accts))
        )
        .scalars()
        .all()
    )
    overrides = _load_transaction_overrides(session)
    candidates: list[tuple[InvestmentTransaction, Decimal]] = []
    reasons: set[str] = set()
    for tx in transactions:
        quantity = Decimal(tx.quantity or 0)
        if quantity == 0:
            continue
        subtype = (tx.subtype or "").lower().strip()
        is_external_cash_transfer = tx.type == InvestmentTransactionType.CASH.value and subtype in {
            "external_asset_transfer_in",
            "external_asset_transfer_out",
        }
        is_zero_amount_plain_transfer = (
            tx.type == InvestmentTransactionType.TRANSFER.value
            and Decimal(tx.amount or 0) == 0
            and subtype not in _INTERNAL_TRANSFER_SUBTYPES
        )
        if not is_external_cash_transfer and not is_zero_amount_plain_transfer:
            continue

        override = overrides.get(tx.plaid_investment_transaction_id)
        name_hint = _classify_by_name(tx.name)
        if override == "internal" or name_hint == "internal":
            continue
        if override == "external_in" or name_hint == "external_in":
            quantity = abs(quantity)
        elif override == "external_out" or name_hint == "external_out":
            quantity = -abs(quantity)
        elif subtype == "external_asset_transfer_in":
            quantity = abs(quantity)
        elif subtype == "external_asset_transfer_out":
            quantity = -abs(quantity)
        candidates.append((tx, quantity))

    if not candidates:
        return _CashflowAssessment(cashflows={}, calculation_reason_codes=())

    sids = frozenset(tx.security_id for tx, _ in candidates if tx.security_id is not None)
    security_by_id = {
        sid: ticker
        for sid, ticker in session.execute(
            select(Security.security_id, Security.ticker).where(Security.security_id.in_(sids))
        ).all()
    }
    split_factors = load_split_factors(session, sids)

    # Net split-normalized quantity per (date, ticker), so provider-specific
    # security IDs for the same asset can cancel as an internal transfer.
    net_by: dict[tuple[date, str], Decimal] = defaultdict(lambda: Decimal(0))
    sids_by_ticker: dict[str, set[int]] = defaultdict(set)
    for tx, quantity in candidates:
        sid = tx.security_id
        if sid is None or sid not in security_by_id:
            reasons.add(_EXTERNAL_SHARE_MOVEMENT_MISSING_SECURITY)
            continue
        ticker = security_by_id[sid]
        if ticker is None or not ticker.strip():
            reasons.add(_EXTERNAL_SHARE_MOVEMENT_MISSING_TICKER)
            continue
        ticker_norm = ticker.strip().upper()
        net_by[(tx.date, ticker_norm)] += quantity * split_factors.factor_after(sid, tx.date)
        sids_by_ticker[ticker_norm].add(sid)

    sids_needed = frozenset(sid for ticker_sids in sids_by_ticker.values() for sid in ticker_sids)
    if not net_by or not sids_needed:
        return _CashflowAssessment(cashflows={}, calculation_reason_codes=tuple(sorted(reasons)))
    # Pull a window of prices around the transfer dates so we can
    # forward-fill if the exact date is a non-trading day.
    earliest_d = min(d for (d, _) in net_by) - timedelta(days=14)
    price_rows = session.execute(
        select(Price.security_id, Price.date, Price.close)
        .where(Price.security_id.in_(sids_needed))
        .where(Price.position_price_trade_eligibility_clause())
        .where(Price.date >= earliest_d)
        .where(Price.date <= end_date)
    ).all()
    price_lookup: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for sid, d, c in price_rows:
        price_lookup[sid][d] = Decimal(c)

    out: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for (tx_date, ticker), net_qty in net_by.items():
        if net_qty == 0:
            continue
        freshest: tuple[date, int, Decimal] | None = None
        earliest = tx_date - timedelta(days=14)
        for sid in sorted(sids_by_ticker[ticker]):
            series = price_lookup.get(sid, {})
            price_dates = [
                d for d, price in series.items() if earliest <= d <= tx_date and price > 0
            ]
            if not price_dates:
                continue
            price_date = max(price_dates)
            candidate = (price_date, sid, series[price_date])
            if (
                freshest is None
                or price_date > freshest[0]
                or (price_date == freshest[0] and sid < freshest[1])
            ):
                freshest = candidate
        close = freshest[2] if freshest is not None else None
        if close is None:
            reasons.add(_EXTERNAL_SHARE_MOVEMENT_PRICE_UNAVAILABLE)
            continue
        out[tx_date] += net_qty * close
    return _CashflowAssessment(cashflows=dict(out), calculation_reason_codes=tuple(sorted(reasons)))


def _classify_by_name(name: str | None) -> str | None:
    """Direction hint derived from the transaction's `name` field.

    Some aggregators bury the actual direction (or the fact that a row
    isn't external cashflow at all) in free-text instead of the subtype:
      * Plaid surfaces SoFi/Robinhood DRIPs as `transfer/transfer` with
        name "Dividend reinvestment purchase of N shares" — these aren't
        external cashflow at all, they're internal share moves backed by
        a dividend that's already accounted for elsewhere.
      * SoFi via Plaid emits dividends as `cash/withdrawal` with name
        "cash - DIVIDEND USD" — the subtype is wrong; it's income earned
        inside the portfolio, not a withdrawal.
      * SnapTrade marks outgoing/incoming margin-balance moves as the
        bare `transfer/transfer` subtype but spells out the direction in
        the name ("Completed outgoing margin balance transfer of $-100").

    Without this, those cases get the wrong sign (or wrong category
    entirely) under the heuristic.

    Returns one of `external_in` / `external_out` / `internal` / `None`.
    Returning None means "no name-based opinion; fall through to the
    subtype heuristic."
    """
    return external_flow_ledger.classify_by_name(name)


def _signed_cashflow(
    tx_type: str,
    tx_subtype: str | None,
    amount: Decimal,
    override: str | None = None,
    name: str | None = None,
) -> Decimal:
    """Return the signed cashflow INTO the portfolio for one transaction.

    Returns Decimal(0) for internal events (trades, dividends, fees, etc.).
    Positive return = money entered the portfolio. Negative = money left.

    Precedence:
      1. Explicit user override (transaction_overrides table)
      2. Name-based hint (drip/outgoing/incoming patterns) — only applied
         for cash/transfer types, never for buy/sell/fee
      3. (type, subtype) heuristic with aggregator sign convention
    """
    return external_flow_ledger.signed_cashflow(
        tx_type, tx_subtype, amount, override=override, name=name
    )


def _load_transaction_overrides(session: Session) -> dict[str, str]:
    """Map tx_id -> classification for every row in transaction_overrides."""
    return external_flow_ledger.load_transaction_overrides(session)


def effective_classification(
    tx_type: str,
    tx_subtype: str | None,
    override: str | None,
    amount: Decimal | None = None,
    name: str | None = None,
) -> str | None:
    """Resolve the cashflow classification used by the pipeline.

    Returns one of:
      * `external_in`   — counts as a contribution / deposit
      * `external_out`  — counts as a withdrawal
      * `internal`      — a transfer or cash event explicitly excluded
                          from external cashflow (e.g. dividends, fees,
                          ACATS recognized as internal)
      * `None`          — a non-cashflow row (buy/sell/fee on a security)
                          for which classification doesn't apply

    `amount` is required to direction-resolve ambiguous subtypes (the bare
    `transfer/transfer` and `cash/transfer`, both of which Plaid signs from
    the cash account's perspective). For unambiguous subtypes (contribution,
    deposit, withdrawal) the amount sign is ignored.

    `override` short-circuits the heuristic when supplied.
    """
    return external_flow_ledger.effective_classification(
        tx_type, tx_subtype, override, amount=amount, name=name
    )


def _daily_portfolio_value(  # pyright: ignore[reportUnusedFunction]
    session: Session, start_date: date, end_date: date
) -> dict[date, Decimal]:
    """Daily total portfolio value across two certified sources:

    1. **`holdings_snapshots`** (forward) — real broker data for the date.
       Authoritative whenever it exists. Includes the day's late-arriving
       SnapTrade pulls if the daily-refresh cron has run.
    2. **Transaction walk-back** — reconstructs anything still missing
       before the earliest snapshot, with cash adjustment so V_start
       doesn't collapse on net deployment.

    Unversioned `portfolio_values_daily` backfill rows are deliberately not
    consumed here. They cannot prove which transactions, owner overrides,
    prices, or account universe produced them, so a later correction could
    otherwise leave certified performance using stale modeled values.
    """
    return _daily_portfolio_value_assessment(session, start_date, end_date).values


def _daily_portfolio_value_assessment(
    session: Session, start_date: date, end_date: date
) -> _PortfolioValueAssessment:
    """Return daily values with boundary lineage and fail-closed coverage.

    Observed values exist only when every valued account reported on that
    date. Modeled cache rows retain modeled lineage and are accepted only when
    the current dataset still has a complete full-book reconstruction anchor.
    A partial observed boundary is never papered over with a cache row.
    """
    accts = valued_account_ids(session)
    account_ids = tuple(sorted(accts))
    forward = _forward_values_from_snapshots(session, start_date, end_date)
    merged: dict[date, Decimal] = dict(forward)
    provenance: dict[date, _ValueProvenance] = {d: _OBSERVED_VALUE for d in forward}

    snapshot_candidates = _snapshot_dates(session, start_date, end_date)
    partial = partial_snapshot_dates(session, snapshot_candidates)
    unpriceable = unpriceable_snapshot_dates(session, snapshot_candidates)
    reasons: set[str] = set()
    for unsupported_date in set(partial) | unpriceable:
        merged.pop(unsupported_date, None)
        provenance.pop(unsupported_date, None)
    if start_date in partial:
        reasons.add(_PARTIAL_SNAPSHOT_START_DATE)
    if end_date in partial:
        reasons.add(_PARTIAL_SNAPSHOT_END_DATE)
    if unpriceable:
        reasons.add(_UNPRICEABLE_HOLDING_SNAPSHOT)

    if (
        snapshot_candidates
        and start_date < min(snapshot_candidates)
        and _reconstruction_anchor_date(session) is None
    ):
        reasons.add(_MODELED_OPENING_ACCOUNT_COVERAGE_INCOMPLETE)

    # Backfill is anchored on the earliest *real* snapshot, so figure out
    # whether we still have a gap at the start of the window.
    earliest_known = min(merged.keys()) if merged else None
    if earliest_known is None or earliest_known > start_date:
        backfill_end = (
            earliest_known - timedelta(days=1) if earliest_known is not None else end_date
        )
        backfill = _backfill_values_from_transactions(session, start_date, backfill_end)
        if (
            start_date < (_reconstruction_anchor_date(session) or start_date)
            and start_date not in backfill
        ):
            reasons.add(_MODELED_OPENING_VALUATION_COVERAGE_INCOMPLETE)
        # Don't overwrite observed snapshot rows we already have.
        for d, v in backfill.items():
            merged.setdefault(d, v)
            provenance.setdefault(d, _MODELED_VALUE)

    # A partial broker observation is evidence that the date is incomplete;
    # never replace it with a modeled/cache value after gap filling.
    for unsupported_date in set(partial) | unpriceable:
        merged.pop(unsupported_date, None)
        provenance.pop(unsupported_date, None)

    if (
        provenance.get(start_date) == _MODELED_VALUE
        and _reconstruction_anchor_date(session) is None
    ):
        reasons.add(_MODELED_OPENING_ACCOUNT_COVERAGE_INCOMPLETE)
        merged.pop(start_date, None)
        provenance.pop(start_date, None)

    return _PortfolioValueAssessment(
        values=merged,
        provenance=provenance,
        valuation_account_ids=account_ids,
        calculation_reason_codes=tuple(sorted(reasons)),
    )


def _snapshot_dates(session: Session, start_date: date, end_date: date) -> set[date]:
    accts = valued_account_ids(session)
    if not accts:
        return set()
    return set(
        session.execute(
            select(HoldingSnapshot.snapshot_date)
            .where(HoldingSnapshot.snapshot_date >= start_date)
            .where(HoldingSnapshot.snapshot_date <= end_date)
            .where(HoldingSnapshot.account_id.in_(accts))
            .distinct()
        )
        .scalars()
        .all()
    )


def partial_snapshot_dates(session: Session, candidates: set[date]) -> dict[date, frozenset[int]]:
    """Return snapshot dates missing one or more valued accounts."""
    if not candidates:
        return {}
    accts = valued_account_ids(session)
    reported: dict[date, set[int]] = defaultdict(set)
    for on_date, account_id in session.execute(
        select(HoldingSnapshot.snapshot_date, HoldingSnapshot.account_id)
        .where(HoldingSnapshot.snapshot_date.in_(candidates))
        .where(HoldingSnapshot.account_id.in_(accts))
        .distinct()
    ).all():
        reported[on_date].add(account_id)
    return {
        on_date: frozenset(accts - reported.get(on_date, set()))
        for on_date in candidates
        if accts - reported.get(on_date, set())
    }


def _complete_snapshot_dates(session: Session, candidates: set[date]) -> frozenset[date]:
    return frozenset(
        candidates
        - set(partial_snapshot_dates(session, candidates))
        - unpriceable_snapshot_dates(session, candidates)
    )


def unpriceable_snapshot_dates(session: Session, candidates: set[date]) -> set[date]:
    """Dates containing a nonzero holding with no usable broker valuation."""
    if not candidates:
        return set()
    accts = valued_account_ids(session)
    if not accts:
        return set()
    return set(
        session.execute(
            select(HoldingSnapshot.snapshot_date)
            .where(HoldingSnapshot.snapshot_date.in_(candidates))
            .where(HoldingSnapshot.account_id.in_(accts))
            .where(HoldingSnapshot.quantity != 0)
            .where(HoldingSnapshot.institution_value.is_(None))
            .where(HoldingSnapshot.institution_price.is_(None))
            .distinct()
        )
        .scalars()
        .all()
    )


def _reconstruction_anchor_date(session: Session) -> date | None:
    """Earliest snapshot date covering the complete valued-account universe."""
    candidates = _snapshot_dates(session, date.min, date.max)
    complete = _complete_snapshot_dates(session, candidates)
    return min(complete) if complete else None


def _forward_values_from_snapshots(
    session: Session, start_date: date, end_date: date
) -> dict[date, Decimal]:
    accts = valued_account_ids(session)
    if not accts:
        return {}
    rows = session.execute(
        select(
            HoldingSnapshot.snapshot_date,
            HoldingSnapshot.institution_value,
            HoldingSnapshot.quantity,
            HoldingSnapshot.institution_price,
            HoldingSnapshot.account_id,
        )
        .where(HoldingSnapshot.snapshot_date >= start_date)
        .where(HoldingSnapshot.snapshot_date <= end_date)
        .where(HoldingSnapshot.account_id.in_(accts))
    ).all()

    complete = _complete_snapshot_dates(session, {d for d, _v, _q, _p, _a in rows})

    totals: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for snap_date, value, quantity, price, _account_id in rows:
        if snap_date not in complete:
            continue
        if value is not None:
            totals[snap_date] += Decimal(value)
        elif price is not None:
            totals[snap_date] += Decimal(quantity) * Decimal(price)
    return dict(totals)


def _backfill_values_from_transactions(
    session: Session, start_date: date, end_date: date
) -> dict[date, Decimal]:
    """Reconstruct daily portfolio values walking transactions backward.

    The walk tracks two parallel state machines, both anchored at the first
    holdings snapshot:

      1. **Positions** — share quantities reconstructed by reversing each
         BUY / SELL / TRANSFER, then valued at historical prices. Cash
         equivalents (USD, money-market funds) sit in `positions` at face
         value; they're the *anchor-date* cash, not historical cash.
      2. **Cash adjustment (delta)** — a scalar per day capturing how much
         cash the user held on date *d* relative to anchor cash. Reversing
         a BUY adds the buy amount (cash was higher before the buy). Without
         this term, V_start collapses by the cash deployed during the window
         and the chart shows a fake ramp.

    Daily total = positions × historical_prices  +  cash_adjustment[d].
    """
    accts = valued_account_ids(session)
    if not accts:
        return {}
    anchor_date = _reconstruction_anchor_date(session)
    if anchor_date is None:
        return {}

    positions = _anchor_positions(session, anchor_date)
    cash_equivalent_security_ids = frozenset(
        session.execute(select(Security.security_id).where(Security.is_cash_equivalent.is_(True)))
        .scalars()
        .all()
    )
    tx_overrides = external_flow_ledger.load_transaction_overrides(session)
    # Walk EVERY transaction in window — pure-cash ones (no security_id)
    # are needed for the cash-adjustment series even though they don't
    # affect positions.
    backward_tx = (
        session.execute(
            select(InvestmentTransaction)
            .where(InvestmentTransaction.account_id.in_(accts))
            .where(InvestmentTransaction.date <= anchor_date)
            .where(InvestmentTransaction.date >= start_date)
            .order_by(InvestmentTransaction.date.desc())
        )
        .scalars()
        .all()
    )

    # Normalize quantities to today's split-adjusted units (consistent with the
    # split-adjusted `prices` used by _value_quantities_with_prices): scale the
    # anchor positions and each reversed tx by the split product after its date.
    split_factors = load_split_factors(
        session,
        set(positions) | {tx.security_id for tx in backward_tx if tx.security_id is not None},
    )
    positions = {
        sid: q * split_factors.factor_after(sid, anchor_date) for sid, q in positions.items()
    }

    # Canonical external-flow decisions own both amount and effective date.
    # Suppress those rows from the transaction-date cash walk and apply them
    # once on the ledger date instead. This matters when a statement Activity
    # Date corroborates a provider row that posted several days later.
    reconstruction_ledger = external_flow_ledger.build_external_flow_ledger(
        session,
        date.min,
        anchor_date,
        account_ids=accts,
    )
    canonical_external_transaction_ids = {
        entry.transaction_id
        for entry in reconstruction_ledger.entries
        if entry.source_kind == "transaction"
        and entry.transaction_id is not None
        and entry.classification != "internal"
    }
    canonical_external_by_date: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
    for entry in reconstruction_ledger.entries:
        if (
            entry.source_kind == "transaction"
            and entry.transaction_id is not None
            and entry.classification != "internal"
            and start_date < entry.date <= anchor_date
        ):
            canonical_external_by_date[entry.date] += entry.signed_external_amount

    transactions_by_date: dict[date, list[InvestmentTransaction]] = defaultdict(list)
    for tx in backward_tx:
        transactions_by_date[tx.date].append(tx)

    daily_quantities: dict[date, dict[int, Decimal]] = {}
    daily_cash_adj: dict[date, Decimal] = {}
    rolling_positions = dict(positions)
    rolling_cash_by_account: dict[int, Decimal] = defaultdict(lambda: Decimal(0))

    def _cash_adjustment() -> Decimal:
        # Every reversed transaction changes the prior-day cash state. Do not
        # suppress the adjustment merely because the first retained row is a
        # BUY: that is precisely the deployment whose pre-trade cash must keep
        # the opening book value intact. Certified callers separately require
        # approved source coverage for the requested history window.
        return sum(rolling_cash_by_account.values(), Decimal(0))

    def _reverse_date(activity_date: date) -> None:
        for tx in transactions_by_date.get(activity_date, ()):
            if tx.security_id is not None and tx.security_id not in cash_equivalent_security_ids:
                delta = _reverse_transaction_quantity(tx)
                if delta is not None:
                    delta *= split_factors.factor_after(tx.security_id, tx.date)
                    rolling_positions[tx.security_id] = (
                        rolling_positions.get(tx.security_id, Decimal(0)) + delta
                    )
            if tx.plaid_investment_transaction_id in canonical_external_transaction_ids:
                continue
            rolling_cash_by_account[tx.account_id] += _reverse_transaction_cash_delta(
                tx,
                cash_equivalent_security_ids,
                override=tx_overrides.get(tx.plaid_investment_transaction_id),
            )
        # Whole-portfolio external cash is intentionally scalar: the account
        # assignment is retained in the ledger receipt, while the consolidated
        # valuation needs only the exact signed change on its effective date.
        rolling_cash_by_account[0] -= canonical_external_by_date.get(activity_date, Decimal(0))

    # The anchor is an end-of-day broker observation. Reverse anchor-day
    # activity before materializing the preceding day's modeled value.
    _reverse_date(anchor_date)
    cursor_date = anchor_date - timedelta(days=1)
    while cursor_date >= start_date:
        daily_quantities[cursor_date] = dict(rolling_positions)
        daily_cash_adj[cursor_date] = _cash_adjustment()
        _reverse_date(cursor_date)
        cursor_date -= timedelta(days=1)

    return _value_quantities_with_prices(
        session, daily_quantities, daily_cash_adj, start_date, end_date
    )


def _anchor_positions(session: Session, anchor_date: date) -> dict[int, Decimal]:
    accts = valued_account_ids(session)
    if not accts:
        return {}
    rows = session.execute(
        select(HoldingSnapshot.security_id, HoldingSnapshot.quantity)
        .where(HoldingSnapshot.snapshot_date == anchor_date)
        .where(HoldingSnapshot.account_id.in_(accts))
    ).all()
    positions: dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    for security_id, quantity in rows:
        positions[security_id] += Decimal(quantity)
    return dict(positions)


def transaction_quantity_delta(tx: InvestmentTransaction) -> Decimal | None:
    """Return the forward share-count change caused by ``tx``.

    Sign conventions vary across data sources:
      * Plaid signs the quantity by direction (sell = negative, buy = positive).
      * SnapTrade reports `units` as an unsigned magnitude for buy/sell, but
        SIGNED quantities for transfer-flavored events (ACATS in is
        positive, ACATS out is negative).

    We use the TRANSACTION TYPE — not the sign of `quantity` — to determine
    direction for BUY/SELL, treating `quantity` as an unsigned magnitude.
    Transfer-flavored quantities are already signed.

    Returns None for events with no position effect (qty=0, plain
    cash/dividend/withdrawal/contribution, fees on USD).
    """
    tx_type = tx.type
    quantity = Decimal(tx.quantity)
    magnitude = abs(quantity)
    if magnitude == 0:
        return None
    if tx_type == InvestmentTransactionType.BUY.value:
        return magnitude
    if tx_type == InvestmentTransactionType.SELL.value:
        return -magnitude
    if tx_type == InvestmentTransactionType.TRANSFER.value:
        return quantity
    if tx_type == InvestmentTransactionType.CASH.value:
        subtype = (tx.subtype or "").lower().strip()
        if subtype in _SHARE_MOVING_CASH_SUBTYPES:
            return quantity
    return None


def _reverse_transaction_quantity(tx: InvestmentTransaction) -> Decimal | None:
    """Return the share-count delta needed to undo ``tx``."""
    delta = transaction_quantity_delta(tx)
    return -delta if delta is not None else None


def _is_transfer_shaped_fee(name: str | None) -> bool:
    """Whether a fee row is actually a broker-mislabeled share transfer."""
    if not name:
        return False
    normalized = name.lower()
    return "transfer in" in normalized or "transfer out" in normalized


def _reverse_transaction_cash_delta(
    tx: InvestmentTransaction,
    cash_equivalent_security_ids: frozenset[int],
    override: str | None = None,
) -> Decimal:
    """Cash adjustment to apply when walking back through `tx`.

    Returns `cash_adjustment[t-1] − cash_adjustment[t]` due to this
    transaction. Positive ⇒ the user held *more* cash before the trade.

    Sign convention is **derived from the transaction TYPE, not the sign
    of `amount`**, because data sources disagree on amount signing:

    * **Plaid** signs `amount` from the cash account's perspective: BUY
      and FEE have amount > 0 (cash debited), SELL and dividend have
      amount < 0 (cash credited).
    * **SnapTrade / Fidelity** signs `amount` the opposite way: BUY has
      amount < 0, SELL has amount > 0.

    A formula that just does `cash_adj += tx.amount` works for one source
    and silently inverts for the other, producing huge fake spikes on
    days with cross-source trades (e.g., Fidelity money-market sweeps).
    Use `|amount|` and let the type decide the sign:

    | Type   | Cash on day d  | cash_adj[t-1] − cash_adj[t] |
    |--------|----------------|------------------------------|
    | BUY    | decreased by m | +m                           |
    | SELL   | increased by m | −m                           |
    | FEE    | decreased by m | +m                           |
    | CASH (internal — div/int) | increased by m | −m         |
    | TRANSFER (internal)       | varies; default 0            |

    **External flows** (deposit/withdrawal/external TRANSFER) go through
    `_signed_cashflow`, which already normalizes signs across sources.

    **Cash-equivalent FEE transactions are skipped** (some brokers emit
    paired `fee/interest` ↔ `fee/miscellaneous fee` entries on the USD
    position to represent internal margin-sweeps; they sum to zero in
    theory but leak in practice).
    """
    amount = Decimal(tx.amount)
    magnitude = abs(amount)

    if (
        tx.type == InvestmentTransactionType.FEE.value
        and tx.security_id in cash_equivalent_security_ids
    ):
        return Decimal(0)

    if tx.type == InvestmentTransactionType.FEE.value and _is_transfer_shaped_fee(tx.name):
        return Decimal(0)

    flow_decision = external_flow_ledger.classify_transaction_cashflow(
        tx.type,
        tx.subtype,
        amount,
        override=override,
        name=tx.name,
    )
    if flow_decision is not None and flow_decision.classification != "internal":
        return -flow_decision.signed_external_amount

    if tx.type == InvestmentTransactionType.BUY.value:
        return magnitude
    if tx.type == InvestmentTransactionType.SELL.value:
        return -magnitude
    if tx.type == InvestmentTransactionType.FEE.value:
        return magnitude
    if tx.type == InvestmentTransactionType.CASH.value:
        # Share-moving cash events (ACATS, option assignment/expiration,
        # dividend reinvestment) have either amount=$0 or a paired-but-
        # not-recorded cash event. Treat as cash-neutral so we don't
        # phantom-debit USD on past dates. The position-quantity reversal
        # is handled by `_reverse_transaction_quantity`.
        subtype = (tx.subtype or "").lower().strip()
        if subtype in _SHARE_MOVING_CASH_SUBTYPES:
            return Decimal(0)
        # Internal cash events left after the external-flow check are
        # dividends / interest / similar credits — money entered, so prior
        # cash was lower.
        return -magnitude
    # Internal TRANSFER (assignment / exercise / merger) typically has
    # amount ≈ 0 and no net cash movement; safe default.
    return Decimal(0)


def _value_quantities_with_prices(
    session: Session,
    daily_quantities: dict[date, dict[int, Decimal]],
    daily_cash_adj: dict[date, Decimal],
    start_date: date,
    end_date: date,
) -> dict[date, Decimal]:
    """Multiply reconstructed quantities by historical prices to get $ values.

    Four valuation outcomes per security:
      1. **Cash equivalents** (USD positions, money market funds) → qty × $1.00.
         These don't have yfinance-pulled price histories but their NAV is
         essentially fixed at $1, so face value is the right answer.
      2. **Derivatives** (options, futures) — `type == "derivative"`. Almost
         always missing from yfinance feeds. We deliberately skip the
         `snapshot_price` fallback for them: at expiration they're worth
         $0 (so today's snapshot price is $0 too), and using $0 as the
         intra-life value avoids the "step-up at trade date" artifact that
         a constant non-zero fallback would create. The premium paid /
         received is fully captured by the cash adjustment, so the
         portfolio total stays right at the boundary even if mid-life MTM
         is approximated as zero.
      3. **Securities with yfinance/stooq price history** → forward-fill the
         most recent close on or before `current_date`.
      4. **No eligible historical price on/before the valuation date** → the
         reconstructed date is unavailable. A later snapshot mark is never
         backcast into the past.
    """
    relevant_dates = [d for d in daily_quantities if start_date <= d <= end_date]
    if not relevant_dates:
        return {}
    security_ids = {sid for snap in daily_quantities.values() for sid in snap}
    if not security_ids:
        return {}

    securities = (
        session.execute(select(Security).where(Security.security_id.in_(security_ids)))
        .scalars()
        .all()
    )
    sec_meta: dict[int, Security] = {s.security_id: s for s in securities}

    # Extend the price query backward by ~14 days so non-trading start
    # dates (e.g., YTD = Jan 1, holiday) fall back to the most recent
    # actual close instead of the wrong snapshot_price fallback.
    price_rows = session.execute(
        select(Price.security_id, Price.date, Price.close)
        .where(Price.security_id.in_(security_ids))
        .where(Price.position_price_trade_eligibility_clause())
        .where(Price.close > 0)
        .where(Price.date >= start_date - timedelta(days=14))
        .where(Price.date <= end_date)
    ).all()
    price_lookup: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for security_id, price_date, close in price_rows:
        price_lookup[security_id][price_date] = Decimal(close)

    totals: dict[date, Decimal] = {}
    for current_date in sorted(relevant_dates):
        snap = daily_quantities[current_date]
        total = Decimal(0)
        valuation_complete = True
        for security_id, quantity in snap.items():
            if quantity == 0:
                continue
            sec = sec_meta.get(security_id)
            if sec is not None and sec.is_cash_equivalent:
                # Cash equivalents (USD, money market funds): face value.
                total += quantity * Decimal(1)
                continue
            close = _last_known_price(price_lookup.get(security_id, {}), current_date)
            if close is None:
                valuation_complete = False
                break
            total += quantity * close
        # Add the cash delta induced by trades + external flows after the
        # anchor date. Anchor cash itself is already counted via cash-equiv
        # securities in `positions`; this term is the "and how much MORE
        # cash were they sitting on before the deployment" piece.
        total += daily_cash_adj.get(current_date, Decimal(0))
        if valuation_complete and total > 0:
            totals[current_date] = total
    return totals


def _last_known_price(series: dict[date, Decimal], target_date: date) -> Decimal | None:
    """Forward-fill: most recent price on or before `target_date`."""
    candidates = [d for d in series if d <= target_date]
    if not candidates:
        return None
    return series[max(candidates)]


def _observed_fixed_holiday(on_date: date) -> date:
    if on_date.weekday() == 5:  # Saturday -> Friday
        return on_date - timedelta(days=1)
    if on_date.weekday() == 6:  # Sunday -> Monday
        return on_date + timedelta(days=1)
    return on_date


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter date, used for the NYSE Good Friday closure."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


_SPECIAL_US_MARKET_CLOSURES = frozenset(
    {
        date(2001, 9, 11),
        date(2001, 9, 12),
        date(2001, 9, 13),
        date(2001, 9, 14),
        date(2004, 6, 11),
        date(2007, 1, 2),
        date(2012, 10, 29),
        date(2012, 10, 30),
        date(2018, 12, 5),
        date(2025, 1, 9),
    }
)


def _us_market_holidays(year: int) -> set[date]:
    holidays = {
        _observed_fixed_holiday(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed_fixed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed_fixed_holiday(date(year, 12, 25)),
        # When next New Year's Day is Saturday, its observed closure falls in
        # this calendar year on December 31.
        _observed_fixed_holiday(date(year + 1, 1, 1)),
    }
    if year >= 2022:
        holidays.add(_observed_fixed_holiday(date(year, 6, 19)))
    return holidays


def _is_us_market_session(on_date: date) -> bool:
    if on_date.weekday() >= 5:
        return False
    return (
        on_date not in _us_market_holidays(on_date.year)
        and on_date not in _SPECIAL_US_MARKET_CLOSURES
    )


def _expected_benchmark_source_date(target_date: date) -> date:
    source_date = target_date
    while not _is_us_market_session(source_date):
        source_date -= timedelta(days=1)
    return source_date


def _benchmark_price_point(
    series: dict[date, Decimal], target_date: date
) -> tuple[date, Decimal] | None:
    """Resolve only the exact or immediately preceding US market close."""
    if _is_us_market_session(target_date):
        exact_price = series.get(target_date)
        if exact_price is None or exact_price <= 0:
            return None
        return target_date, exact_price
    source_date = _expected_benchmark_source_date(target_date)
    price = series.get(source_date)
    if price is None or price <= 0:
        return None
    return source_date, price


def _benchmark_price(series: dict[date, Decimal], target_date: date) -> Decimal | None:
    resolved = _benchmark_price_point(series, target_date)
    return resolved[1] if resolved is not None else None


def _resolved_price_inputs(
    series: dict[date, Decimal], required_dates: Iterable[date]
) -> dict[date, tuple[date, Decimal]] | None:
    """Resolve every required mark or return None; partial inputs are unusable."""
    resolved: dict[date, tuple[date, Decimal]] = {}
    for target_date in required_dates:
        price_point = _benchmark_price_point(series, target_date)
        if price_point is None:
            return None
        resolved[target_date] = price_point
    return resolved


def _benchmark_series(
    session: Session, start_date: date, end_date: date
) -> dict[str, dict[date, Decimal]]:
    """Pull benchmark closes covering `[start_date, end_date]`.

    Extends the query backward by seven days so we can anchor a synthetic
    benchmark portfolio whose `start_date` falls on a non-trading day
    (e.g., YTD = Jan 1, a holiday). Without this lookback, `_last_known_price`
    has no candidates ≤ start_date in the filtered dict and returns None,
    which collapses the SPY/QQQ lines to empty.

    Uses `total_return_close` (dividend-reinvested), coalesced to the raw
    `close` for pre-0021 rows, so benchmark daily returns and the matched-flow
    synthetic are total-return — consistent with the position-alpha
    counterfactual and an honest comparison against a buy-and-hold index.
    """
    rows = session.execute(
        select(
            Benchmark.symbol,
            Benchmark.date,
            func.coalesce(Benchmark.total_return_close, Benchmark.close),
        )
        .where(Benchmark.date >= start_date - timedelta(days=_BENCHMARK_QUERY_LOOKBACK_DAYS))
        .where(Benchmark.date <= end_date)
    ).all()
    out: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for symbol, bench_date, close in rows:
        out[symbol][bench_date] = Decimal(close)
    return dict(out)


def _benchmark_return_basis(
    session: Session, start_date: date, end_date: date
) -> dict[str, dict[date, str]]:
    """Expose whether each benchmark mark is adjusted or raw-price fallback."""
    rows = session.execute(
        select(
            Benchmark.symbol,
            Benchmark.date,
            Benchmark.total_return_close,
        )
        .where(Benchmark.date >= start_date - timedelta(days=_BENCHMARK_QUERY_LOOKBACK_DAYS))
        .where(Benchmark.date <= end_date)
    ).all()
    out: dict[str, dict[date, str]] = defaultdict(dict)
    for symbol, bench_date, total_return_close in rows:
        out[symbol][bench_date] = (
            "total_return_adjusted" if total_return_close is not None else "raw_price_fallback"
        )
    return dict(out)
