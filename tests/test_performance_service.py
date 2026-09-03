"""Unit + integration tests for services/performance.py.

Three documented bug-prone areas:
  * `_modified_dietz_series` — the cumulative TWR-ish % with cashflow weighting.
  * `_reverse_transaction_quantity` / `_reverse_transaction_cash_delta` — the
    sign machine for the walk-back (many past sign-flip bugs).
  * `_backfill_values_from_transactions` — reconstructs historical V with a
    cash adjustment so V_start doesn't collapse on net deployment.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    Benchmark,
    CashFlowReconciliationDecision,
    CashFlowSourceAttestation,
    CashFlowSourceEvent,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    PortfolioValueDaily,
    Price,
    PriceAdjustmentBasis,
    PriceSource,
    Security,
    StockSplit,
)
from portfolio_tracker.services import performance as performance_service
from portfolio_tracker.services.cashflow_source_coverage import (
    canonical_decision_payload_sha256,
    canonical_source_event_set_sha256,
)
from portfolio_tracker.services.external_flow_ledger import classify_transaction_cashflow
from portfolio_tracker.services.performance import (
    _backfill_values_from_transactions,
    _is_transfer_shaped_fee,
    _modified_dietz_series,
    _money_flow_matched_value,
    _policy_matched_value,
    _reverse_transaction_cash_delta,
    _reverse_transaction_quantity,
    _share_transfer_external_cashflows,
    compute_performance_series,
)


def _tx(
    tx_type: str,
    *,
    quantity: Decimal = Decimal(0),
    amount: Decimal = Decimal(0),
    subtype: str | None = None,
    security_id: int | None = None,
    name: str | None = None,
) -> InvestmentTransaction:
    return InvestmentTransaction(
        type=tx_type,
        quantity=quantity,
        amount=amount,
        subtype=subtype,
        security_id=security_id,
        name=name,
    )


def _approve_source_coverage(session, account: Account, start: date, end: date) -> None:
    session.add(
        CashFlowSourceAttestation(
            attestation_key=f"synthetic-performance-{account.account_id}-{start}-{end}",
            account_id=account.account_id,
            coverage_start=start + timedelta(days=1),
            coverage_end=end,
            source_type="provider_export",
            source_reference="synthetic:performance-test",
            source_sha256="a" * 64,
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
            approved_at=datetime(2026, 1, 2, tzinfo=UTC),
            methodology_version="1",
            account_identity_sha256="b" * 64,
            account_mapping_basis="owner_confirmed",
            account_mapping_confidence="exact",
            source_format="synthetic",
            parser_version="test-v1",
            source_timezone="UTC",
            source_row_count=0,
            cashflow_candidate_count=0,
            source_event_set_sha256=canonical_source_event_set_sha256(()),
            manifest_sha256="d" * 64,
        )
    )


# ---------------------------------------------------------------------------
# _modified_dietz_series
# ---------------------------------------------------------------------------


def test_modified_dietz_no_cashflow():
    d0, d10 = date(2025, 1, 1), date(2025, 1, 11)
    out = _modified_dietz_series(
        [d0, d10],
        {d0: Decimal(1000), d10: Decimal(1100)},
        {},
        Decimal(1000),
    )
    assert out[d0] == Decimal(0)
    assert out[d10] == Decimal(10)  # (1100 - 1000) / 1000 * 100


def test_modified_dietz_weights_midwindow_cashflow():
    d0, mid, d10 = date(2025, 1, 1), date(2025, 1, 6), date(2025, 1, 11)
    out = _modified_dietz_series(
        [d0, d10],
        {d0: Decimal(1000), d10: Decimal(1600)},
        {mid: Decimal(500)},  # deposit 5 days into a 10-day window
        Decimal(1000),
    )
    # num = 1600 - 1000 - 500 = 100
    # weighted_cf = 500 * 5/10 = 250 ; denom = 1000 + 250 = 1250
    # 100 / 1250 * 100 = 8.0
    assert out[d10] == Decimal(8)


def test_modified_dietz_nonpositive_denominator_is_zero():
    d0, d10 = date(2025, 1, 1), date(2025, 1, 11)
    out = _modified_dietz_series(
        [d0, d10],
        {d0: Decimal(100), d10: Decimal(200)},
        {},
        Decimal(0),  # base 0, no cashflow → denom 0
    )
    assert out[d10] == Decimal(0)


def test_money_flow_match_does_not_double_count_start_date_cashflow():
    d0, d10 = date(2025, 1, 2), date(2025, 1, 10)
    out = _money_flow_matched_value(
        [d0, d10],
        Decimal(1000),
        {d0: Decimal(1000)},
        {d0: Decimal(100), d10: Decimal(110)},
    )
    # V_start is an end-of-day opening value. A start-day flow is already in
    # that value and must not create a second benchmark lot.
    assert out[d0] == Decimal(1000)
    assert out[d10] == Decimal(1100)


def test_money_flow_match_rejects_stale_benchmark_endpoint():
    start = date(2025, 1, 1)
    end = date(2025, 6, 1)

    assert (
        _money_flow_matched_value(
            [start, end],
            Decimal(1000),
            {},
            {start: Decimal(100)},
        )
        == {}
    )


def test_policy_match_does_not_double_count_start_date_cashflow():
    d0, d10 = date(2025, 1, 2), date(2025, 1, 10)
    out = _policy_matched_value(
        [d0, d10],
        Decimal(1000),
        {d0: Decimal(1000)},
        {"SPY": {d0: Decimal(100), d10: Decimal(110)}},
        {"SPY": Decimal(1)},
    )
    assert out[d0] == Decimal(1000)
    assert out[d10] == Decimal(1100)


# ---------------------------------------------------------------------------
# Cash-flow-matched benchmark books
# ---------------------------------------------------------------------------


def test_money_flow_matched_value_invests_flow_between_observation_dates():
    start = date(2025, 1, 2)
    flow_date = date(2025, 1, 6)
    end = date(2025, 1, 10)

    out = _money_flow_matched_value(
        [start, end],
        Decimal(1000),
        {flow_date: Decimal(550)},
        {
            start: Decimal(100),
            flow_date: Decimal(110),
            end: Decimal(121),
        },
    )

    # Initial capital buys 10 shares; the intervening flow buys 5 shares.
    assert out[end] == Decimal(1815)


def test_policy_matched_value_invests_flow_between_observation_dates():
    start = date(2025, 1, 2)
    flow_date = date(2025, 1, 6)
    end = date(2025, 1, 10)

    out = _policy_matched_value(
        [start, end],
        Decimal(1000),
        {flow_date: Decimal(550)},
        {
            "SPY": {
                start: Decimal(100),
                flow_date: Decimal(110),
                end: Decimal(121),
            }
        },
        {"SPY": Decimal(1)},
    )

    assert out[end] == Decimal(1815)


def test_money_flow_matched_value_excludes_start_flow_and_includes_end_flow():
    start = date(2025, 1, 2)
    end = date(2025, 1, 3)

    out = _money_flow_matched_value(
        [start, end],
        Decimal(1000),
        {
            start: Decimal(500),
            end: Decimal(-200),
        },
        {
            start: Decimal(100),
            end: Decimal(120),
        },
    )

    # The opening value already contains start-date activity. The end-date
    # withdrawal is part of (start, end] and sells benchmark shares at close.
    assert out[start] == Decimal(1000)
    assert out[end] == Decimal(1000)


def test_policy_matched_value_excludes_start_flow_and_includes_end_flow():
    start = date(2025, 1, 2)
    end = date(2025, 1, 3)

    out = _policy_matched_value(
        [start, end],
        Decimal(1000),
        {
            start: Decimal(500),
            end: Decimal(-200),
        },
        {"SPY": {start: Decimal(100), end: Decimal(120)}},
        {"SPY": Decimal(1)},
    )

    assert out[start] == Decimal(1000)
    assert out[end] == Decimal(1000)


def test_performance_series_uses_one_period_cashflow_set(monkeypatch, session):
    start = date(2025, 1, 2)
    flow_date = date(2025, 1, 4)
    end = date(2025, 1, 6)
    after_end = date(2025, 1, 7)

    monkeypatch.setattr(
        performance_service,
        "_daily_portfolio_value_assessment",
        lambda *_args: performance_service._PortfolioValueAssessment(
            values={start: Decimal(1000), end: Decimal(1300)},
            provenance={
                start: "observed_complete_snapshot",
                end: "observed_complete_snapshot",
            },
            valuation_account_ids=(),
            calculation_reason_codes=(),
        ),
    )
    monkeypatch.setattr(
        performance_service,
        "_daily_external_cashflow_assessment",
        lambda *_args: performance_service._CashflowAssessment(
            cashflows={
                start: Decimal(50),
                flow_date: Decimal(200),
                end: Decimal(-100),
                after_end: Decimal(900),
            },
            calculation_reason_codes=(),
        ),
    )
    closes = {
        start: Decimal(100),
        date(2025, 1, 3): Decimal(100),
        end: Decimal(100),
    }
    monkeypatch.setattr(
        performance_service,
        "_benchmark_series",
        lambda *_args: {"SPY": closes, "QQQ": closes},
    )
    monkeypatch.setattr(
        performance_service, "load_policy_weights", lambda *_args: {"SPY": Decimal(1)}
    )
    monkeypatch.setattr(
        performance_service,
        "_earliest_observed_date",
        lambda *_args: start,
    )

    series = performance_service.compute_performance_series(session, start, end)
    final = series.points[-1]

    # Opening-date and post-ending-date flows are outside the end-of-day
    # window. The same +$100 net flow feeds the bridge and both flat-price
    # benchmark books, whose investment gain is therefore exactly zero.
    assert series.net_external_cashflow_in == Decimal(100)
    assert final.portfolio_value == Decimal(1300)
    assert final.spy_equivalent_value == Decimal(1100)
    assert final.qqq_equivalent_value == Decimal(1100)
    assert final.policy_equivalent_value == Decimal(1100)
    assert final.spy_return_pct == Decimal(0)
    assert final.qqq_return_pct == Decimal(0)
    assert final.portfolio_return_pct == (Decimal(200) / Decimal(1100)) * Decimal(100)

    receipt = series.equation_receipt
    assert receipt is not None
    assert receipt.opening_value == Decimal(1000)
    assert [(flow.date, flow.amount) for flow in receipt.dated_external_cashflows] == [
        (flow_date, Decimal(200)),
        (end, Decimal(-100)),
    ]
    assert all(flow.flow_ids for flow in receipt.dated_external_cashflows)
    assert {flow.date: flow.amount for flow in receipt.operative_external_cashflows} == {
        flow_date: Decimal(200),
        end: Decimal(-100),
    }
    assert receipt.net_external_cashflow_in == Decimal(100)
    assert receipt.ending_value == Decimal(1300)
    assert receipt.investment_gain == Decimal(200)
    assert receipt.modified_dietz_denominator == Decimal(1100)
    assert receipt.portfolio_return_pct == (
        receipt.investment_gain / receipt.modified_dietz_denominator
    ) * Decimal(100)
    assert receipt.portfolio_equation_residual == Decimal(0)
    for benchmark in (receipt.spy, receipt.qqq):
        assert benchmark.ending_value == Decimal(1100)
        assert benchmark.investment_gain == Decimal(0)
        assert benchmark.return_pct == Decimal(0)
        assert benchmark.dollar_alpha == Decimal(200)
        assert benchmark.percentage_point_alpha == receipt.portfolio_return_pct
        assert benchmark.equation_residual == Decimal(0)
        assert all(row.return_basis == "raw_price_fallback" for row in benchmark.price_inputs)
    assert receipt.policy is not None
    assert receipt.policy.ending_value == Decimal(1100)
    assert receipt.policy.investment_gain == Decimal(0)
    assert receipt.policy.return_pct == Decimal(0)
    assert receipt.policy.dollar_alpha == Decimal(200)
    assert receipt.policy.percentage_point_alpha == receipt.portfolio_return_pct
    assert receipt.policy.equation_residual == Decimal(0)
    assert receipt.calculation_id.startswith("sha256:")
    assert receipt.external_flow_ledger_id.startswith("sha256:")
    assert receipt.portfolio_valuation_input_id.startswith("sha256:")
    assert receipt.spy.price_input_id.startswith("sha256:")
    assert receipt.qqq.price_input_id.startswith("sha256:")


def test_performance_series_requires_exact_requested_valuation_boundaries(monkeypatch, session):
    start = date(2025, 1, 1)
    end = date(2025, 1, 3)
    monkeypatch.setattr(
        performance_service,
        "_daily_portfolio_value_assessment",
        lambda *_args: performance_service._PortfolioValueAssessment(
            values={
                start + timedelta(days=1): Decimal(1000),
                end - timedelta(days=1): Decimal(1000),
            },
            provenance={},
            valuation_account_ids=(),
            calculation_reason_codes=(),
        ),
    )
    closes = {
        date(2024, 12, 31): Decimal(100),
        date(2025, 1, 2): Decimal(100),
        end: Decimal(100),
    }
    monkeypatch.setattr(
        performance_service,
        "_benchmark_series",
        lambda *_args: {"SPY": closes, "QQQ": closes},
    )
    monkeypatch.setattr(performance_service, "load_policy_weights", lambda *_args: {})

    series = performance_service.compute_performance_series(session, start, end)

    assert series.calculation_status == "unavailable"
    assert series.calculation_reason_codes == [
        "portfolio_end_value_unavailable",
        "portfolio_start_value_unavailable",
    ]
    assert series.equation_receipt is None


def test_performance_series_fails_closed_when_required_benchmark_marks_are_missing(
    monkeypatch, session, client
):
    start = date(2025, 1, 1)
    flow_date = date(2025, 1, 20)
    end = date(2025, 1, 21)
    monkeypatch.setattr(
        performance_service,
        "_daily_portfolio_value_assessment",
        lambda *_args: performance_service._PortfolioValueAssessment(
            values={start: Decimal(1000), end: Decimal(1100)},
            provenance={
                start: "observed_complete_snapshot",
                end: "observed_complete_snapshot",
            },
            valuation_account_ids=(),
            calculation_reason_codes=(),
        ),
    )
    monkeypatch.setattr(
        performance_service,
        "_daily_external_cashflow_assessment",
        lambda *_args: performance_service._CashflowAssessment(
            cashflows={flow_date: Decimal(100)}, calculation_reason_codes=()
        ),
    )
    monkeypatch.setattr(
        performance_service,
        "_benchmark_series",
        lambda *_args: {
            # The ending mark cannot be used retrospectively for the flow;
            # SPY's prior mark is 19 days old at deployment.
            "SPY": {start: Decimal(100), end: Decimal(101)},
            # QQQ's only mark is non-positive and must not be accepted.
            "QQQ": {start: Decimal(0), flow_date: Decimal(0), end: Decimal(0)},
        },
    )
    monkeypatch.setattr(performance_service, "load_policy_weights", lambda *_args: {})

    series = performance_service.compute_performance_series(session, start, end)

    assert series.calculation_status == "unavailable"
    assert series.calculation_reason_codes == [
        "qqq_benchmark_price_unavailable",
        "spy_benchmark_price_unavailable",
    ]
    assert series.equation_receipt is None
    assert all(point.spy_return_pct is None for point in series.points)
    assert all(point.qqq_return_pct is None for point in series.points)

    response = client.get(
        "/api/v1/analytics/performance",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["series"]["calculation_status"] == "unavailable"
    calculation_warnings = [
        warning
        for warning in payload["meta"]["warnings"]
        if warning["code"] == "CALCULATION_UNAVAILABLE"
    ]
    assert calculation_warnings == [
        {
            "code": "CALCULATION_UNAVAILABLE",
            "message": (
                "Whole-portfolio performance is unavailable: "
                "qqq_benchmark_price_unavailable, spy_benchmark_price_unavailable"
            ),
            "scope": "performance",
        }
    ]


def test_performance_series_requires_every_configured_policy_component(monkeypatch, session):
    start = date(2025, 1, 2)
    end = date(2025, 1, 3)
    monkeypatch.setattr(
        performance_service,
        "_daily_portfolio_value_assessment",
        lambda *_args: performance_service._PortfolioValueAssessment(
            values={start: Decimal(1000), end: Decimal(1100)},
            provenance={
                start: "observed_complete_snapshot",
                end: "observed_complete_snapshot",
            },
            valuation_account_ids=(),
            calculation_reason_codes=(),
        ),
    )
    monkeypatch.setattr(
        performance_service,
        "_daily_external_cashflow_assessment",
        lambda *_args: performance_service._CashflowAssessment(
            cashflows={}, calculation_reason_codes=()
        ),
    )
    closes = {start: Decimal(100), end: Decimal(110)}
    monkeypatch.setattr(
        performance_service,
        "_benchmark_series",
        lambda *_args: {"SPY": closes, "QQQ": closes},
    )
    monkeypatch.setattr(
        performance_service,
        "load_policy_weights",
        lambda *_args: {"SPY": Decimal("0.5"), "VWO": Decimal("0.5")},
    )

    series = performance_service.compute_performance_series(session, start, end)

    assert series.calculation_status == "unavailable"
    assert series.calculation_reason_codes == ["policy_benchmark_price_unavailable"]
    assert series.equation_receipt is None


def test_performance_series_rejects_a_missing_market_session_close(monkeypatch, session):
    start = date(2025, 1, 6)  # Monday
    end = date(2025, 1, 10)  # Friday
    monkeypatch.setattr(
        performance_service,
        "_daily_portfolio_value_assessment",
        lambda *_args: performance_service._PortfolioValueAssessment(
            values={start: Decimal(1000), end: Decimal(1100)},
            provenance={
                start: "observed_complete_snapshot",
                end: "observed_complete_snapshot",
            },
            valuation_account_ids=(),
            calculation_reason_codes=(),
        ),
    )
    monkeypatch.setattr(
        performance_service,
        "_daily_external_cashflow_assessment",
        lambda *_args: performance_service._CashflowAssessment(
            cashflows={}, calculation_reason_codes=()
        ),
    )
    monkeypatch.setattr(
        performance_service,
        "_benchmark_series",
        lambda *_args: {
            "SPY": {start: Decimal(100)},
            "QQQ": {start: Decimal(100)},
        },
    )
    monkeypatch.setattr(performance_service, "load_policy_weights", lambda *_args: {})

    series = performance_service.compute_performance_series(session, start, end)

    assert series.calculation_status == "unavailable"
    assert series.calculation_reason_codes == [
        "qqq_benchmark_price_unavailable",
        "spy_benchmark_price_unavailable",
    ]
    assert series.equation_receipt is None


def test_performance_receipt_exposes_prior_market_close_resolution(monkeypatch, session):
    prior_close = date(2025, 1, 3)  # Friday
    start = date(2025, 1, 4)  # Saturday
    end = date(2025, 1, 5)  # Sunday
    monkeypatch.setattr(
        performance_service,
        "_daily_portfolio_value_assessment",
        lambda *_args: performance_service._PortfolioValueAssessment(
            values={start: Decimal(1000), end: Decimal(1000)},
            provenance={
                start: "observed_complete_snapshot",
                end: "observed_complete_snapshot",
            },
            valuation_account_ids=(),
            calculation_reason_codes=(),
        ),
    )
    monkeypatch.setattr(
        performance_service,
        "_daily_external_cashflow_assessment",
        lambda *_args: performance_service._CashflowAssessment(
            cashflows={}, calculation_reason_codes=()
        ),
    )
    monkeypatch.setattr(
        performance_service,
        "_benchmark_series",
        lambda *_args: {
            "SPY": {prior_close: Decimal(100)},
            "QQQ": {prior_close: Decimal(200)},
        },
    )
    monkeypatch.setattr(performance_service, "load_policy_weights", lambda *_args: {})

    series = performance_service.compute_performance_series(session, start, end)

    assert series.calculation_status == "available"
    receipt = series.equation_receipt
    assert receipt is not None
    assert receipt.benchmark_price_resolution_policy == "same_day_or_previous_us_market_close"
    assert [
        (row.target_date, row.source_date, row.resolution) for row in receipt.spy.price_inputs
    ] == [
        (start, prior_close, "previous_market_close"),
        (end, prior_close, "previous_market_close"),
    ]


def test_benchmark_resolution_uses_prior_close_for_known_market_holiday():
    prior_close = date(2025, 1, 17)
    mlk_day = date(2025, 1, 20)

    resolved = performance_service._resolved_price_inputs({prior_close: Decimal(100)}, [mlk_day])

    assert resolved == {mlk_day: (prior_close, Decimal(100))}


def test_benchmark_resolution_ignores_erroneous_non_session_row():
    prior_close = date(2025, 1, 3)
    saturday = date(2025, 1, 4)

    resolved = performance_service._resolved_price_inputs(
        {
            prior_close: Decimal(100),
            saturday: Decimal(123),
        },
        [saturday],
    )

    assert resolved == {saturday: (prior_close, Decimal(100))}


def test_boundary_failure_also_reports_independent_flow_and_benchmark_gaps(monkeypatch, session):
    start = date(2025, 1, 6)
    end = date(2025, 1, 10)
    monkeypatch.setattr(
        performance_service,
        "_daily_portfolio_value_assessment",
        lambda *_args: performance_service._PortfolioValueAssessment(
            values={},
            provenance={},
            valuation_account_ids=(),
            calculation_reason_codes=(),
        ),
    )
    monkeypatch.setattr(
        performance_service,
        "_daily_external_cashflow_assessment",
        lambda *_args: performance_service._CashflowAssessment(
            cashflows={},
            calculation_reason_codes=("external_share_movement_price_unavailable",),
        ),
    )
    monkeypatch.setattr(performance_service, "_benchmark_series", lambda *_args: {})
    monkeypatch.setattr(performance_service, "load_policy_weights", lambda *_args: {})

    series = performance_service.compute_performance_series(session, start, end)

    assert series.calculation_status == "unavailable"
    assert series.calculation_reason_codes == [
        "external_share_movement_price_unavailable",
        "no_portfolio_values",
        "portfolio_end_value_unavailable",
        "portfolio_start_value_unavailable",
        "qqq_benchmark_price_unavailable",
        "spy_benchmark_price_unavailable",
    ]


# ---------------------------------------------------------------------------
# _reverse_transaction_quantity
# ---------------------------------------------------------------------------


def test_reverse_quantity_buy_and_sell_use_type_not_sign():
    assert _reverse_transaction_quantity(_tx("buy", quantity=Decimal(30))) == Decimal(-30)
    assert _reverse_transaction_quantity(_tx("sell", quantity=Decimal(30))) == Decimal(30)


def test_reverse_quantity_transfer_uses_signed_quantity():
    # ACATS out (negative units) reverses to a positive add-back.
    assert _reverse_transaction_quantity(_tx("transfer", quantity=Decimal(-10))) == Decimal(10)
    assert _reverse_transaction_quantity(_tx("transfer", quantity=Decimal(10))) == Decimal(-10)


def test_reverse_quantity_share_moving_cash_subtypes():
    assert _reverse_transaction_quantity(
        _tx("cash", quantity=Decimal(-5), subtype="external_asset_transfer_out")
    ) == Decimal(5)
    assert _reverse_transaction_quantity(
        _tx("cash", quantity=Decimal(2), subtype="rei")
    ) == Decimal(-2)


def test_reverse_quantity_no_position_effect_returns_none():
    assert _reverse_transaction_quantity(_tx("buy", quantity=Decimal(0))) is None
    assert _reverse_transaction_quantity(_tx("fee", quantity=Decimal(1))) is None
    # Plain (non share-moving) cash event leaves positions untouched.
    assert (
        _reverse_transaction_quantity(_tx("cash", quantity=Decimal(7), subtype="deposit")) is None
    )


# ---------------------------------------------------------------------------
# _reverse_transaction_cash_delta
# ---------------------------------------------------------------------------


def test_reverse_cash_delta_buy_sell_use_magnitude():
    # BUY: cash was higher before → +m. SELL: cash was lower before → -m.
    assert _reverse_transaction_cash_delta(_tx("buy", amount=Decimal(500)), frozenset()) == Decimal(
        500
    )
    assert _reverse_transaction_cash_delta(
        _tx("sell", amount=Decimal(500)), frozenset()
    ) == Decimal(-500)


def test_reverse_cash_delta_fee_skipped_for_cash_equivalent():
    fee = _tx("fee", amount=Decimal(10), security_id=1)
    assert _reverse_transaction_cash_delta(fee, frozenset()) == Decimal(10)
    # A fee booked against the USD/MMF position is an internal sweep — skip it.
    assert _reverse_transaction_cash_delta(fee, frozenset({1})) == Decimal(0)


def test_reverse_cash_delta_internal_dividend_credit():
    # cash/dividend is income earned inside the portfolio → -m (cash was lower).
    assert _reverse_transaction_cash_delta(
        _tx("cash", amount=Decimal(20), subtype="dividend"), frozenset()
    ) == Decimal(-20)


def test_reverse_cash_delta_share_moving_is_cash_neutral():
    assert _reverse_transaction_cash_delta(
        _tx("cash", amount=Decimal(0), subtype="external_asset_transfer_in"), frozenset()
    ) == Decimal(0)


def test_reverse_cash_delta_external_flows():
    # Deposit: before it, cash was lower → -m.
    assert _reverse_transaction_cash_delta(
        _tx("cash", amount=Decimal(1000), subtype="deposit"), frozenset()
    ) == Decimal(-1000)
    # Withdrawal: before it, cash was higher → +m.
    assert _reverse_transaction_cash_delta(
        _tx("cash", amount=Decimal(200), subtype="withdrawal"), frozenset()
    ) == Decimal(200)


def test_reverse_cash_delta_uses_owner_approved_flow_direction():
    for name in ("ACAT Reimbursement", "Account Promo Reward"):
        tx = _tx("cash", amount=Decimal(100), subtype="withdrawal", name=name)
        decision = classify_transaction_cashflow(
            tx.type,
            tx.subtype,
            Decimal(tx.amount),
            name=tx.name,
        )

        assert decision is not None
        assert decision.classification == "external_in"
        assert decision.signed_external_amount == Decimal(100)
        assert _reverse_transaction_cash_delta(tx, frozenset()) == Decimal(-100)


# ---------------------------------------------------------------------------
# _backfill_values_from_transactions — cash-adjustment keeps V_start intact
# ---------------------------------------------------------------------------


def test_backfill_cash_adjustment_prevents_vstart_collapse(session):
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id, plaid_account_id="a-1", name="Taxable", type="investment"
    )
    session.add(account)
    session.flush()
    aapl = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs", is_cash_equivalent=False)
    session.add(aapl)
    session.flush()

    anchor = date(2025, 1, 10)
    session.add(
        HoldingSnapshot(
            snapshot_date=anchor,
            account_id=account.account_id,
            security_id=aapl.security_id,
            quantity=Decimal(10),
            institution_price=Decimal(100),
            institution_value=Decimal(1000),
        )
    )
    # Bought 4 shares for $400 on Jan 5. Walking back, positions drop to 6 but
    # the +$400 cash adjustment compensates: at a flat $100 price, V stays
    # $1000 every day (no fake ramp from the deployment).
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="tx-buy",
            account_id=account.account_id,
            security_id=aapl.security_id,
            date=date(2025, 1, 5),
            type="buy",
            quantity=Decimal(4),
            amount=Decimal(400),
        )
    )
    for day in range(1, 11):
        session.add(
            Price(
                security_id=aapl.security_id,
                date=date(2025, 1, day),
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            )
        )
    session.commit()

    result = _backfill_values_from_transactions(session, date(2025, 1, 1), date(2025, 1, 9))

    expected_dates = {date(2025, 1, day) for day in range(1, 10)}
    assert set(result.keys()) == expected_dates
    assert all(v == Decimal(1000) for v in result.values())
    # Pre-deployment day still values at 1000 (positions 6 × 100 + 400 cash).
    assert result[date(2025, 1, 4)] == Decimal(1000)
    # Post-deployment day: positions 10 × 100, cash adj 0.
    assert result[date(2025, 1, 9)] == Decimal(1000)


def test_backfill_applies_statement_activity_date_not_later_provider_posting(session):
    item = Item(
        source="plaid",
        plaid_item_id="shifted-flow-item",
        institution_name="Broker",
        is_data_active=True,
    )
    account = Account(
        item=item,
        plaid_account_id="shifted-flow-account",
        name="Shifted flow",
        type="investment",
    )
    security = Security(
        plaid_security_id="shifted-flow-security",
        ticker="SHIFT",
        type="cs",
        is_cash_equivalent=False,
    )
    session.add_all([item, account, security])
    session.flush()
    activity_date = date(2025, 1, 5)
    provider_date = date(2025, 1, 8)
    anchor_date = date(2025, 1, 10)
    transaction_id = "provider-shifted-deposit"
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=anchor_date,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(11),
                institution_price=Decimal(100),
                institution_value=Decimal(1100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id=transaction_id,
                account_id=account.account_id,
                security_id=None,
                date=provider_date,
                name="Incoming transfer",
                quantity=Decimal(0),
                amount=Decimal(-100),
                type="cash",
                subtype="transfer",
                currency="USD",
            ),
        ]
    )
    for day in range(1, 11):
        session.add(
            Price(
                security_id=security.security_id,
                date=date(2025, 1, day),
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            )
        )
    attestation = CashFlowSourceAttestation(
        attestation_key="shifted-flow-attestation",
        account_id=account.account_id,
        coverage_start=activity_date,
        coverage_end=activity_date,
        source_type="brokerage_statement",
        source_reference="private:shifted-flow-statement",
        source_sha256="1" * 64,
        captured_at=datetime(2025, 1, 11, tzinfo=UTC),
        approved_at=datetime(2025, 1, 12, tzinfo=UTC),
        methodology_version="2",
        account_identity_sha256="2" * 64,
        account_mapping_basis="owner_confirmed",
        account_mapping_confidence="exact",
        source_format="synthetic",
        parser_version="test-v1",
        source_timezone="America/New_York",
        source_row_count=1,
        cashflow_candidate_count=1,
        source_event_set_sha256="3" * 64,
        manifest_sha256="4" * 64,
    )
    session.add(attestation)
    session.flush()
    event = CashFlowSourceEvent(
        source_event_id="5" * 64,
        attestation_id=attestation.attestation_id,
        source_locator_kind="row",
        source_locator="row:1",
        source_row_ordinal=1,
        source_row_sha256="6" * 64,
        activity_date=activity_date,
        source_amount=Decimal(100),
        source_amount_sign_basis="statement_printed",
        currency="USD",
    )
    decision = CashFlowReconciliationDecision(
        decision_key="7" * 64,
        source_event_id=event.source_event_id,
        target_transaction_id=transaction_id,
        resolution_kind="provider_exact",
        classification="external_in",
        signed_external_amount=Decimal(100),
        effective_date=activity_date,
        effective_date_basis="source_activity",
        effective_timezone="America/New_York",
        decision_authority="brokerage_statement",
        confidence="exact",
        assumption_code="statement_activity_date",
        methodology_version="2",
        decision_payload_sha256="8" * 64,
        approved_at=datetime(2025, 1, 12, tzinfo=UTC),
    )
    attestation.source_event_set_sha256 = canonical_source_event_set_sha256((event,))
    decision.decision_payload_sha256 = canonical_decision_payload_sha256(decision)
    session.add_all([event, decision])
    session.commit()

    result = _backfill_values_from_transactions(
        session, date(2025, 1, 1), anchor_date - timedelta(days=1)
    )

    assert result[date(2025, 1, 4)] == Decimal(1000)
    assert result[activity_date] == Decimal(1100)
    assert result[date(2025, 1, 6)] == Decimal(1100)


def test_preperiod_in_kind_transfer_remains_in_opening_positions(session):
    item = Item(
        source="plaid",
        plaid_item_id="preperiod-in-kind-item",
        institution_name="Broker",
        is_data_active=True,
    )
    account = Account(
        item=item,
        plaid_account_id="preperiod-in-kind-account",
        name="Preperiod in-kind",
        type="investment",
    )
    security = Security(
        plaid_security_id="preperiod-in-kind-security",
        ticker="INKIND",
        type="cs",
        is_cash_equivalent=False,
    )
    session.add_all([item, account, security])
    session.flush()
    start_date = date(2025, 1, 1)
    anchor_date = date(2025, 1, 10)
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=anchor_date,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(10),
                institution_price=Decimal(100),
                institution_value=Decimal(1000),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="preperiod-in-kind-transfer",
                account_id=account.account_id,
                security_id=security.security_id,
                date=start_date - timedelta(days=1),
                name="Incoming account transfer",
                quantity=Decimal(10),
                amount=Decimal(0),
                type="cash",
                subtype="external_asset_transfer_in",
                currency="USD",
            ),
        ]
    )
    for offset in range(0, 11):
        session.add(
            Price(
                security_id=security.security_id,
                date=start_date - timedelta(days=1) + timedelta(days=offset),
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            )
        )
    session.commit()

    result = _backfill_values_from_transactions(
        session, start_date, anchor_date - timedelta(days=1)
    )

    assert result[start_date] == Decimal(1000)
    assert all(value == Decimal(1000) for value in result.values())


def test_modeled_opening_rejects_future_snapshot_price_as_fallback(session):
    item = Item(source="plaid", plaid_item_id="future-mark-item", is_data_active=True)
    account = Account(
        item=item,
        plaid_account_id="future-mark-account",
        name="Future mark",
        type="investment",
    )
    security = Security(
        plaid_security_id="future-mark-security",
        ticker="FUTURE",
        type="cs",
        is_cash_equivalent=False,
    )
    session.add_all([item, account, security])
    session.flush()
    session.add(
        HoldingSnapshot(
            snapshot_date=date(2025, 1, 10),
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal(10),
            institution_price=Decimal(100),
            institution_value=Decimal(1000),
        )
    )
    session.commit()

    result = _backfill_values_from_transactions(session, date(2025, 1, 1), date(2025, 1, 9))

    assert result == {}


def test_modeled_values_ignore_stale_cache_after_transaction_mutation(session):
    item = Item(source="plaid", plaid_item_id="cache-item", is_data_active=True)
    account = Account(
        item=item,
        plaid_account_id="cache-account",
        name="Cache test",
        type="investment",
    )
    security = Security(
        plaid_security_id="cache-security",
        ticker="CACHE",
        type="cs",
        is_cash_equivalent=False,
    )
    session.add_all([item, account, security])
    session.flush()
    start = date(2025, 1, 1)
    anchor = date(2025, 1, 10)
    transaction = InvestmentTransaction(
        plaid_investment_transaction_id="cache-buy",
        account_id=account.account_id,
        security_id=security.security_id,
        date=date(2025, 1, 5),
        type="buy",
        quantity=Decimal(4),
        amount=Decimal(400),
    )
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=anchor,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1000),
            ),
            transaction,
            PortfolioValueDaily(
                date=start,
                total_value=Decimal(1000),
                total_cost_basis=None,
                source="backfill",
            ),
        ]
    )
    for day in range(1, 11):
        session.add(
            Price(
                security_id=security.security_id,
                date=date(2025, 1, day),
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            )
        )
    session.commit()

    transaction.amount = Decimal(500)
    session.commit()
    assessment = performance_service._daily_portfolio_value_assessment(session, start, anchor)

    assert assessment.values[start] == Decimal(1100)
    assert assessment.provenance[start] == "modeled_transaction_walkback"


def test_share_transfer_cashflow_nets_provider_ids_after_split_normalization(session):
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    session.add(item)
    session.flush()
    first = Account(item_id=item.item_id, plaid_account_id="a-1", name="Taxable", type="investment")
    second = Account(item_id=item.item_id, plaid_account_id="a-2", name="IRA", type="investment")
    old_sid = Security(plaid_security_id="plaid:aapl", ticker="aapl", type="cs")
    new_sid = Security(plaid_security_id="snaptrade:aapl", ticker="AAPL", type="cs")
    session.add_all([first, second, old_sid, new_sid])
    session.flush()
    movement_date = date(2025, 5, 15)
    session.add_all(
        [
            StockSplit(
                security_id=old_sid.security_id,
                split_date=date(2025, 5, 20),
                ratio=Decimal(2),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-out",
                account_id=first.account_id,
                security_id=old_sid.security_id,
                date=movement_date,
                type="cash",
                subtype="external_asset_transfer_out",
                quantity=Decimal(-10),
                amount=Decimal(0),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-in",
                account_id=second.account_id,
                security_id=new_sid.security_id,
                date=movement_date,
                type="cash",
                subtype="external_asset_transfer_in",
                quantity=Decimal(20),
                amount=Decimal(0),
            ),
        ]
    )
    session.commit()

    assert (
        _share_transfer_external_cashflows(
            session,
            date(2025, 5, 1),
            date(2025, 6, 1),
            frozenset({first.account_id, second.account_id}),
        )
        == {}
    )


def test_share_transfer_cashflow_requires_eligible_split_adjusted_price(session):
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    account = Account(item=item, plaid_account_id="a-1", name="Taxable", type="investment")
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add_all([item, account, security])
    session.flush()
    movement_date = date(2025, 5, 15)
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-in",
                account_id=account.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="cash",
                subtype="external_asset_transfer_in",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
            Price(
                security_id=security.security_id,
                date=movement_date,
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
        ]
    )
    session.commit()

    assert _share_transfer_external_cashflows(
        session,
        date(2025, 5, 1),
        date(2025, 6, 1),
        frozenset({account.account_id}),
    ) == {movement_date: Decimal(1000)}


def test_share_transfer_cashflow_uses_freshest_eligible_ticker_price(session):
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    account = Account(item=item, plaid_account_id="a-1", name="Taxable", type="investment")
    older = Security(plaid_security_id="plaid:nu", ticker="NU", type="cs")
    fresher = Security(plaid_security_id="snaptrade:nu", ticker="NU", type="cs")
    session.add_all([item, account, older, fresher])
    session.flush()
    movement_date = date(2025, 5, 15)
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-in-old-id",
                account_id=account.account_id,
                security_id=older.security_id,
                date=movement_date,
                type="cash",
                subtype="external_asset_transfer_in",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-in-new-id",
                account_id=account.account_id,
                security_id=fresher.security_id,
                date=movement_date,
                type="cash",
                subtype="external_asset_transfer_in",
                quantity=Decimal(1),
                amount=Decimal(0),
            ),
            Price(
                security_id=older.security_id,
                date=movement_date - timedelta(days=8),
                close=Decimal(90),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
            Price(
                security_id=fresher.security_id,
                date=movement_date,
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
        ]
    )
    session.commit()

    assert _share_transfer_external_cashflows(
        session,
        date(2025, 5, 1),
        date(2025, 6, 1),
        frozenset({account.account_id}),
    ) == {movement_date: Decimal(1100)}


def test_plain_zero_amount_share_transfer_is_valued_as_external_cashflow(session):
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    account = Account(item=item, plaid_account_id="a-1", name="Taxable", type="investment")
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add_all([item, account, security])
    session.flush()
    movement_date = date(2025, 5, 15)
    session.add_all(
        [
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-plain-transfer-in",
                account_id=account.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="transfer",
                subtype="transfer",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
            Price(
                security_id=security.security_id,
                date=movement_date,
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
        ]
    )
    session.commit()

    assert _share_transfer_external_cashflows(
        session,
        date(2025, 5, 1),
        date(2025, 6, 1),
        frozenset({account.account_id}),
    ) == {movement_date: Decimal(1000)}


def test_whole_account_performance_fails_closed_on_unpriceable_share_transfer(client, session):
    start = date(2025, 5, 1)
    movement_date = date(2025, 5, 20)
    end = date(2025, 6, 1)
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    account = Account(item=item, plaid_account_id="a-1", name="Taxable", type="investment")
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add_all([item, account, security])
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=Decimal(100),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(11),
                institution_value=Decimal(1100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-unpriceable-transfer",
                account_id=account.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="transfer",
                subtype="transfer",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=date(2025, 5, 30), close=Decimal(110)),
            Benchmark(symbol="SPY", date=end, close=Decimal(110)),
            Benchmark(symbol="QQQ", date=start, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=date(2025, 5, 30), close=Decimal(110)),
            Benchmark(symbol="QQQ", date=end, close=Decimal(110)),
        ]
    )
    _approve_source_coverage(session, account, start, end)
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["external_share_movement_price_unavailable"]
    assert result.net_external_cashflow_in is None
    assert all(point.portfolio_return_pct is None for point in result.points)
    assert all(point.spy_return_pct is None for point in result.points)
    assert all(point.spy_equivalent_value is None for point in result.points)

    response = client.get(
        "/api/v1/analytics/performance",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
    )
    assert response.status_code == 200
    wire_series = response.json()["series"]
    assert wire_series["calculation_status"] == "unavailable"
    assert wire_series["calculation_reason_codes"] == ["external_share_movement_price_unavailable"]
    assert wire_series["net_external_cashflow_in"] is None


def test_whole_account_performance_uses_priceable_share_transfer_cashflow(session):
    start = date(2025, 5, 1)
    movement_date = date(2025, 5, 20)
    end = date(2025, 6, 1)
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    account = Account(item=item, plaid_account_id="a-1", name="Taxable", type="investment")
    security = Security(plaid_security_id="s-aapl", ticker="AAPL", type="cs")
    session.add_all([item, account, security])
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=Decimal(100),
            ),
            HoldingSnapshot(
                snapshot_date=movement_date,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(11),
                institution_value=Decimal(1100),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(11),
                institution_value=Decimal(1100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="tx-priceable-transfer",
                account_id=account.account_id,
                security_id=security.security_id,
                date=movement_date,
                type="transfer",
                subtype="transfer",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
            Price(
                security_id=security.security_id,
                date=movement_date,
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=date(2025, 5, 30), close=Decimal(110)),
            Benchmark(symbol="SPY", date=movement_date, close=Decimal(105)),
            Benchmark(symbol="SPY", date=end, close=Decimal(110)),
            Benchmark(symbol="QQQ", date=start, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=date(2025, 5, 30), close=Decimal(110)),
            Benchmark(symbol="QQQ", date=movement_date, close=Decimal(105)),
            Benchmark(symbol="QQQ", date=end, close=Decimal(110)),
        ]
    )
    _approve_source_coverage(session, account, start, end)
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.calculation_status == "available"
    assert result.calculation_reason_codes == []
    assert result.net_external_cashflow_in == Decimal(1000)
    assert result.points[-1].portfolio_return_pct == 0
    assert result.points[-1].spy_return_pct is not None


def test_whole_account_performance_rejects_nonpositive_dietz_denominator(session):
    start = date(2025, 5, 1)
    end = date(2025, 5, 11)
    item = Item(source="plaid", plaid_item_id="itm-1", institution_name="RH", is_data_active=True)
    account = Account(item=item, plaid_account_id="a-1", name="Taxable", type="investment")
    cash = Security(plaid_security_id="cash", ticker="CUR:USD", type="cash")
    session.add_all([item, account, cash])
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=cash.security_id,
                quantity=Decimal(0),
                institution_value=Decimal(0),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=cash.security_id,
                quantity=Decimal(1100),
                institution_value=Decimal(1100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="deposit-at-end",
                account_id=account.account_id,
                security_id=cash.security_id,
                date=end,
                type="cash",
                subtype="deposit",
                quantity=Decimal(1000),
                amount=Decimal(1000),
            ),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=date(2025, 5, 9), close=Decimal(100)),
            Benchmark(symbol="SPY", date=end, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=start, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=date(2025, 5, 9), close=Decimal(100)),
            Benchmark(symbol="QQQ", date=end, close=Decimal(100)),
        ]
    )
    _approve_source_coverage(session, account, start, end)
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["nonpositive_dietz_denominator"]
    assert result.net_external_cashflow_in is None
    assert all(point.portfolio_return_pct is None for point in result.points)


def test_nonpositive_denominator_is_reported_with_missing_source_coverage(session):
    start = date(2025, 1, 2)
    flow_date = date(2025, 1, 3)
    end = date(2025, 1, 6)
    item = Item(source="plaid", plaid_item_id="denominator-item", is_data_active=True)
    account = Account(
        item=item,
        plaid_account_id="denominator-account",
        name="Denominator",
        type="investment",
    )
    cash = Security(plaid_security_id="denominator-cash", ticker="CUR:USD", type="cash")
    session.add_all([item, account, cash])
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=cash.security_id,
                quantity=Decimal(100),
                institution_value=Decimal(100),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=cash.security_id,
                quantity=Decimal(100),
                institution_value=Decimal(100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="large-withdrawal",
                account_id=account.account_id,
                security_id=cash.security_id,
                date=flow_date,
                type="cash",
                subtype="withdrawal",
                quantity=Decimal(1000),
                amount=Decimal(1000),
            ),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=flow_date, close=Decimal(100)),
            Benchmark(symbol="SPY", date=end, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=start, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=flow_date, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=end, close=Decimal(100)),
        ]
    )
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == [
        "external_flow_source_coverage_incomplete",
        "nonpositive_dietz_denominator",
    ]
    assert result.equation_receipt is None


def test_performance_rejects_partial_requested_end_boundary(session):
    item = Item(source="plaid", plaid_item_id="partial-item", is_data_active=True)
    session.add(item)
    session.flush()
    accounts = [
        Account(
            item_id=item.item_id,
            plaid_account_id=f"partial-account-{index}",
            name=f"Account {index}",
            type="investment",
        )
        for index in range(2)
    ]
    security = Security(
        plaid_security_id="partial-security",
        ticker="AAA",
        type="equity",
        is_cash_equivalent=False,
    )
    session.add_all([*accounts, security])
    session.flush()
    start = date(2026, 1, 1)
    end = date(2026, 1, 2)
    for account in accounts:
        session.add(
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_price=Decimal(100),
                institution_value=Decimal(100),
            )
        )
    session.add(
        HoldingSnapshot(
            snapshot_date=end,
            account_id=accounts[0].account_id,
            security_id=security.security_id,
            quantity=Decimal(1),
            institution_price=Decimal(101),
            institution_value=Decimal(101),
        )
    )
    # A stale cache row must not disguise the partial observed boundary.
    session.add(
        PortfolioValueDaily(
            date=end,
            total_value=Decimal(202),
            total_cost_basis=None,
            source="backfill",
        )
    )
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.calculation_status == "unavailable"
    assert "partial_snapshot_end_date" in result.calculation_reason_codes
    assert result.ending_value_provenance is None


def test_unpriceable_holding_makes_observed_boundary_unavailable(session):
    item = Item(source="plaid", plaid_item_id="unpriceable-item", is_data_active=True)
    account = Account(
        item=item,
        plaid_account_id="unpriceable-account",
        name="Unpriceable",
        type="investment",
    )
    priced = Security(
        plaid_security_id="priced-security",
        ticker="GOOD",
        type="equity",
        is_cash_equivalent=False,
    )
    missing = Security(
        plaid_security_id="missing-security",
        ticker="MISS",
        type="equity",
        is_cash_equivalent=False,
    )
    session.add_all([item, account, priced, missing])
    session.flush()
    start = date(2026, 1, 1)
    end = date(2026, 1, 2)
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=priced.security_id,
                quantity=Decimal(1),
                institution_value=Decimal(100),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=priced.security_id,
                quantity=Decimal(1),
                institution_value=Decimal(110),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=missing.security_id,
                quantity=Decimal(1),
                institution_value=None,
                institution_price=None,
            ),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=end, close=Decimal(110)),
            Benchmark(symbol="QQQ", date=start, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=end, close=Decimal(110)),
        ]
    )
    _approve_source_coverage(session, account, start, end)
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.calculation_status == "unavailable"
    assert "unpriceable_holding_snapshot" in result.calculation_reason_codes
    assert "portfolio_end_value_unavailable" in result.calculation_reason_codes
    assert result.ending_value_provenance is None
    assert result.equation_receipt is None


def test_performance_reports_observed_boundary_provenance(session):
    item = Item(source="plaid", plaid_item_id="observed-item", is_data_active=True)
    account = Account(
        item=item,
        plaid_account_id="observed-account",
        name="Observed",
        type="investment",
    )
    security = Security(
        plaid_security_id="observed-security",
        ticker="AAA",
        type="equity",
        is_cash_equivalent=False,
    )
    session.add_all([item, account, security])
    session.flush()
    start = date(2026, 1, 1)
    end = date(2026, 1, 2)
    for on_date, value in ((start, Decimal(100)), (end, Decimal(110))):
        session.add(
            HoldingSnapshot(
                snapshot_date=on_date,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_price=value,
                institution_value=value,
            )
        )
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.opening_value_provenance == "observed_complete_snapshot"
    assert result.ending_value_provenance == "observed_complete_snapshot"
    assert result.valuation_account_ids == [account.account_id]


def test_performance_reports_supported_modeled_opening_provenance(session):
    item = Item(source="plaid", plaid_item_id="modeled-item", is_data_active=True)
    account = Account(
        item=item,
        plaid_account_id="modeled-account",
        name="Modeled",
        type="investment",
    )
    security = Security(
        plaid_security_id="modeled-security",
        ticker="AAA",
        type="equity",
        is_cash_equivalent=False,
    )
    session.add_all([item, account, security])
    session.flush()
    start = date(2026, 1, 1)
    end = date(2026, 1, 3)
    session.add(
        HoldingSnapshot(
            snapshot_date=end,
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal(1),
            institution_price=Decimal(110),
            institution_value=Decimal(110),
        )
    )
    session.add(
        Price(
            security_id=security.security_id,
            date=start,
            close=Decimal(100),
            source=PriceSource.YFINANCE.value,
            adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
        )
    )
    _approve_source_coverage(session, account, start, end)
    session.add_all(
        [
            Benchmark(symbol="SPY", date=date(2025, 12, 31), close=Decimal(100)),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=date(2026, 1, 2), close=Decimal(105)),
            Benchmark(symbol="SPY", date=end, close=Decimal(110)),
            Benchmark(symbol="QQQ", date=date(2025, 12, 31), close=Decimal(100)),
            Benchmark(symbol="QQQ", date=start, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=date(2026, 1, 2), close=Decimal(105)),
            Benchmark(symbol="QQQ", date=end, close=Decimal(110)),
        ]
    )
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.calculation_status == "available"
    assert result.opening_value_provenance == "modeled_transaction_walkback"
    assert result.reconstruction_certification == "modeled_provisional"
    assert result.ending_value_provenance == "observed_complete_snapshot"
    assert result.backfill_start_unreliable is True


def test_modeled_opening_requires_full_account_anchor(session):
    item = Item(source="plaid", plaid_item_id="anchor-item", is_data_active=True)
    accounts = [
        Account(
            item=item,
            plaid_account_id=f"anchor-account-{index}",
            name=f"Anchor {index}",
            type="investment",
        )
        for index in range(2)
    ]
    securities = [
        Security(
            plaid_security_id=f"anchor-security-{index}",
            ticker=f"A{index}",
            type="equity",
            is_cash_equivalent=False,
        )
        for index in range(2)
    ]
    session.add_all([item, *accounts, *securities])
    session.flush()
    start = date(2025, 12, 31)
    end = date(2026, 1, 3)
    # Each account has snapshot evidence, but never on the same date. There is
    # no full-book anchor from which the opening can be reconstructed.
    for index, on_date in enumerate((date(2026, 1, 2), end)):
        session.add(
            HoldingSnapshot(
                snapshot_date=on_date,
                account_id=accounts[index].account_id,
                security_id=securities[index].security_id,
                quantity=Decimal(1),
                institution_price=Decimal(100),
                institution_value=Decimal(100),
            )
        )
        session.add(
            Price(
                security_id=securities[index].security_id,
                date=start,
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            )
        )
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.calculation_status == "unavailable"
    assert "modeled_opening_account_coverage_incomplete" in result.calculation_reason_codes
    assert result.opening_value_provenance is None


def test_modeled_opening_fails_closed_when_anchor_position_cannot_be_valued(session):
    item = Item(source="plaid", plaid_item_id="unpriced-item", is_data_active=True)
    account = Account(
        item=item,
        plaid_account_id="unpriced-account",
        name="Unpriced",
        type="investment",
    )
    security = Security(
        plaid_security_id="unpriced-security",
        ticker="NOQUOTE",
        type="equity",
        is_cash_equivalent=False,
    )
    session.add_all([item, account, security])
    session.flush()
    start = date(2026, 1, 1)
    end = date(2026, 1, 3)
    session.add(
        HoldingSnapshot(
            snapshot_date=end,
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal(1),
            institution_price=None,
            institution_value=Decimal(100),
        )
    )
    session.commit()

    result = compute_performance_series(session, start, end)

    assert result.calculation_status == "unavailable"
    assert "modeled_opening_valuation_coverage_incomplete" in result.calculation_reason_codes
    assert result.opening_value_provenance is None


def test_backfill_does_not_reverse_cash_equivalent_quantity_and_cash_twice(session):
    item = Item(source="plaid", plaid_item_id="cash-item", is_data_active=True)
    account = Account(
        item=item,
        plaid_account_id="cash-account",
        name="Cash",
        type="investment",
    )
    cash = Security(
        plaid_security_id="cash-security",
        ticker="CUR:USD",
        type="cash",
        is_cash_equivalent=True,
    )
    session.add_all([item, account, cash])
    session.flush()
    start = date(2026, 1, 1)
    anchor = date(2026, 1, 3)
    session.add(
        HoldingSnapshot(
            snapshot_date=anchor,
            account_id=account.account_id,
            security_id=cash.security_id,
            quantity=Decimal(1500),
            institution_price=Decimal(1),
            institution_value=Decimal(1500),
        )
    )
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="cash-deposit",
            account_id=account.account_id,
            security_id=cash.security_id,
            date=date(2026, 1, 2),
            name="deposit",
            quantity=Decimal(-500),
            amount=Decimal(-500),
            type="cash",
            subtype="deposit",
        )
    )
    session.commit()

    values = _backfill_values_from_transactions(session, start, date(2026, 1, 2))

    assert values[start] == Decimal(1000)


def test_transfer_shaped_fee_is_cash_neutral():
    assert _is_transfer_shaped_fee("fee - TRANSFER IN VTI")
    assert _is_transfer_shaped_fee("fee - TRANSFER OUT BN")
    assert not _is_transfer_shaped_fee("fee - MARGIN INTEREST USD")
    assert not _is_transfer_shaped_fee(None)
    assert _reverse_transaction_cash_delta(
        _tx("fee", amount=Decimal(134957), name="fee - TRANSFER IN VTI"),
        frozenset(),
    ) == Decimal(0)
