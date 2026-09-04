"""Default and explicit boundary behavior for performance API windows."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    AccountValuationObservation,
    AccountValuationSourceKind,
    Benchmark,
    CashFlowSourceAttestation,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Price,
    PriceAdjustmentBasis,
    PriceSource,
    Security,
)
from portfolio_tracker.services.account_valuations import (
    NewAccountValuationObservation,
    account_valuation_observation_key,
)
from portfolio_tracker.services.cashflow_source_coverage import (
    canonical_source_event_set_sha256,
)
from portfolio_tracker.services.performance import (
    _complete_account_valuations_by_date,
    _daily_portfolio_value_assessment,
    resolve_performance_window,
)


def _seed_snapshot_account(session, *, account_key: str) -> Account:
    item = Item(
        source="plaid",
        plaid_item_id=f"item-{account_key}",
        institution_name="Broker",
        is_data_active=True,
    )
    account = Account(
        item=item,
        plaid_account_id=f"account-{account_key}",
        name=account_key,
        type="investment",
    )
    session.add(account)
    session.flush()
    return account


def _valuation_row(
    *,
    account_id: int,
    as_of_date: date,
    total_value: Decimal,
    source_provider: str,
    source_reference: str,
    source_record_id: str,
    fetched_at: datetime,
    is_complete: bool = True,
) -> AccountValuationObservation:
    value = NewAccountValuationObservation(
        account_id=account_id,
        as_of_date=as_of_date,
        total_value=total_value,
        cash_value=Decimal(0),
        currency="USD",
        source_kind=AccountValuationSourceKind.PROVIDER_API,
        source_provider=source_provider,
        source_reference=source_reference,
        source_record_id=source_record_id,
        source_payload_sha256=None,
        fetched_at=fetched_at,
        is_complete=is_complete,
        is_empty=False,
    )
    return AccountValuationObservation(
        observation_key=account_valuation_observation_key(value),
        account_id=value.account_id,
        as_of_date=value.as_of_date,
        total_value=value.total_value,
        cash_value=value.cash_value,
        currency=value.currency,
        source_kind=value.source_kind.value,
        source_provider=value.source_provider,
        source_reference=value.source_reference,
        source_record_id=value.source_record_id,
        source_payload_sha256=value.source_payload_sha256,
        normalization_version="1",
        fetched_at=value.fetched_at,
        is_complete=value.is_complete,
        is_empty=value.is_empty,
    )


def test_omitted_end_uses_latest_complete_portfolio_observation(session) -> None:
    complete_date = date(2026, 8, 28)
    partial_later_date = date(2026, 9, 1)
    security = Security(plaid_security_id="window-security", ticker="WINDOW", type="cs")
    session.add(security)
    session.flush()
    first = _seed_snapshot_account(session, account_key="first")
    second = _seed_snapshot_account(session, account_key="second")
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=complete_date,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=Decimal(100),
            )
            for account in (first, second)
        ]
        + [
            HoldingSnapshot(
                snapshot_date=partial_later_date,
                account_id=first.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=Decimal(101),
            )
        ]
    )
    session.commit()

    start, end = resolve_performance_window(session, None, None, include_backfill=False)

    assert end == complete_date
    assert start == complete_date


def test_single_observation_returns_unavailable_instead_of_crashing(client, session) -> None:
    observed = date(2026, 8, 28)
    security = Security(plaid_security_id="single-window-security", ticker="ONE", type="cs")
    account = _seed_snapshot_account(session, account_key="single-window")
    session.add(security)
    session.flush()
    session.add(
        HoldingSnapshot(
            snapshot_date=observed,
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal(1),
            institution_value=Decimal(100),
        )
    )
    session.commit()

    response = client.get("/api/portfolio/performance")

    assert response.status_code == 200
    payload = response.json()
    assert payload["calculation_status"] == "unavailable"
    assert "performance_window_too_short" in payload["calculation_reason_codes"]


def test_include_backfill_preserves_two_year_transaction_walkback_default(session) -> None:
    observed = date(2026, 8, 28)
    transaction_date = observed - timedelta(days=900)
    security = Security(plaid_security_id="backfill-window-security", ticker="BACK", type="cs")
    account = _seed_snapshot_account(session, account_key="backfill-window")
    session.add(security)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=observed,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=Decimal(100),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="backfill-window-transaction",
                account_id=account.account_id,
                date=transaction_date,
                name="Buy",
                quantity=Decimal(1),
                amount=Decimal(50),
                type="buy",
                currency="USD",
            ),
        ]
    )
    session.commit()

    start, end = resolve_performance_window(session, None, None, include_backfill=True)

    assert end == observed
    assert start == observed - timedelta(days=730)


def test_omitted_window_selects_one_matched_observation_basis(session) -> None:
    snapshot_start = date(2026, 7, 1)
    snapshot_end = date(2026, 8, 31)
    total_date = date(2026, 9, 1)
    security = Security(plaid_security_id="matched-window-security", ticker="MATCH", type="cs")
    account = _seed_snapshot_account(session, account_key="matched-window")
    session.add(security)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=as_of,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=value,
            )
            for as_of, value in (
                (snapshot_start, Decimal(100)),
                (snapshot_end, Decimal(110)),
            )
        ]
        + [
            AccountValuationObservation(
                observation_key="d" * 64,
                account_id=account.account_id,
                as_of_date=total_date,
                total_value=Decimal(111),
                cash_value=Decimal(1),
                currency="USD",
                source_kind="provider_api",
                source_provider="plaid",
                source_reference="accounts[].balances.current",
                source_record_id="matched-window",
                fetched_at=datetime(2026, 9, 1, 20, tzinfo=UTC),
                is_complete=True,
                is_empty=False,
            )
        ]
    )
    session.commit()

    start, end = resolve_performance_window(session, None, None, include_backfill=False)

    assert (start, end) == (snapshot_start, snapshot_end)


def test_modeled_explicit_start_uses_latest_holdings_end_not_newer_total(session) -> None:
    modeled_start = date(2024, 9, 3)
    snapshot_end = date(2026, 8, 31)
    total_date = date(2026, 9, 1)
    security = Security(plaid_security_id="modeled-end-security", ticker="MODEL", type="cs")
    account = _seed_snapshot_account(session, account_key="modeled-end")
    session.add(security)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=snapshot_end,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=Decimal(110),
            ),
            AccountValuationObservation(
                observation_key="e" * 64,
                account_id=account.account_id,
                as_of_date=total_date,
                total_value=Decimal(111),
                cash_value=Decimal(1),
                currency="USD",
                source_kind="provider_api",
                source_provider="plaid",
                source_reference="accounts[].balances.current",
                source_record_id="modeled-end",
                fetched_at=datetime(2026, 9, 1, 20, tzinfo=UTC),
                is_complete=True,
                is_empty=False,
            ),
        ]
    )
    session.commit()

    start, end = resolve_performance_window(session, modeled_start, None, include_backfill=False)

    assert (start, end) == (modeled_start, snapshot_end)


def test_performance_end_remains_exact_when_caller_supplies_it(client, session) -> None:
    observed = date(2026, 8, 28)
    requested_end = date(2026, 9, 2)
    requested_start = date(2026, 8, 1)
    security = Security(plaid_security_id="explicit-security", ticker="EXACT", type="cs")
    session.add(security)
    session.flush()
    account = _seed_snapshot_account(session, account_key="explicit")
    session.add(
        HoldingSnapshot(
            snapshot_date=observed,
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal(1),
            institution_value=Decimal(100),
        )
    )
    session.commit()

    params = {
        "start_date": requested_start.isoformat(),
        "end_date": requested_end.isoformat(),
    }
    legacy = client.get("/api/portfolio/performance", params=params)
    versioned = client.get("/api/v1/analytics/performance", params=params)

    assert legacy.status_code == 200
    assert versioned.status_code == 200
    assert legacy.json()["end_date"] == requested_end.isoformat()
    assert versioned.json()["series"] == legacy.json()


def test_legacy_and_v1_default_performance_windows_are_identical(client, session) -> None:
    observed = date(2026, 8, 28)
    security = Security(plaid_security_id="parity-security", ticker="PARITY", type="cs")
    session.add(security)
    session.flush()
    account = _seed_snapshot_account(session, account_key="parity")
    session.add(
        HoldingSnapshot(
            snapshot_date=observed,
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal(1),
            institution_value=Decimal(100),
        )
    )
    session.commit()

    legacy = client.get("/api/portfolio/performance")
    versioned = client.get("/api/v1/analytics/performance")

    assert legacy.status_code == 200
    assert versioned.status_code == 200
    assert legacy.json()["end_date"] == observed.isoformat()
    assert versioned.json()["series"] == legacy.json()


def test_complete_account_totals_supply_exact_boundaries_and_receipt_keys(client, session) -> None:
    start = date(2026, 8, 3)
    end = date(2026, 8, 28)
    item = Item(
        source="snaptrade",
        plaid_item_id="account-total-item",
        institution_name="Broker",
        is_data_active=True,
    )
    account = Account(
        item=item,
        plaid_account_id="account-total-account",
        name="Explicitly empty-capable account",
        type="investment",
    )
    session.add(account)
    session.flush()
    opening = _valuation_row(
        account_id=account.account_id,
        as_of_date=start,
        total_value=Decimal(1000),
        source_provider="snaptrade",
        source_reference="account.balance.total",
        source_record_id=f"balance-{start.isoformat()}",
        fetched_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    closing = _valuation_row(
        account_id=account.account_id,
        as_of_date=end,
        total_value=Decimal(1100),
        source_provider="snaptrade",
        source_reference="account.balance.total",
        source_record_id=f"balance-{end.isoformat()}",
        fetched_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    session.add_all([opening, closing])
    session.add(
        CashFlowSourceAttestation(
            attestation_key="account-total-performance-window",
            account_id=account.account_id,
            coverage_start=start + timedelta(days=1),
            coverage_end=end,
            source_type="provider_export",
            source_reference="synthetic:account-total-window",
            source_sha256="c" * 64,
            captured_at=datetime(2026, 8, 29, tzinfo=UTC),
            approved_at=datetime(2026, 8, 29, tzinfo=UTC),
            methodology_version="1",
            account_identity_sha256="d" * 64,
            account_mapping_basis="owner_confirmed",
            account_mapping_confidence="exact",
            source_format="synthetic",
            parser_version="test-v1",
            source_timezone="UTC",
            source_row_count=0,
            cashflow_candidate_count=0,
            source_event_set_sha256=canonical_source_event_set_sha256(()),
            manifest_sha256="e" * 64,
        )
    )
    session.add_all(
        [
            Benchmark(
                symbol=ticker,
                date=as_of,
                close=price,
                total_return_close=price,
            )
            for ticker in ("SPY", "QQQ")
            for as_of, price in ((start, Decimal(100)), (end, Decimal(110)))
        ]
    )
    session.commit()

    resolved_start, resolved_end = resolve_performance_window(
        session, start, None, include_backfill=False
    )
    assert (resolved_start, resolved_end) == (start, end)

    params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    legacy = client.get("/api/portfolio/performance", params=params)
    versioned = client.get("/api/v1/analytics/performance", params=params)

    assert legacy.status_code == 200
    assert versioned.status_code == 200
    payload = legacy.json()
    assert versioned.json()["series"] == payload
    assert payload["base_value"] == "1000.000000"
    assert payload["calculation_status"] == "available"
    assert payload["opening_value_provenance"] == "observed_account_valuation"
    assert payload["ending_value_provenance"] == "observed_account_valuation"
    assert payload["opening_valuation_observation_keys"] == [opening.observation_key]
    assert payload["ending_valuation_observation_keys"] == [closing.observation_key]
    assert payload["valuation_account_ids"] == [account.account_id]
    assert payload["equation_receipt"]["opening_valuation_observation_keys"] == [
        opening.observation_key
    ]
    assert payload["equation_receipt"]["ending_valuation_observation_keys"] == [
        closing.observation_key
    ]
    assert versioned.json()["meta"]["as_of"] == end.isoformat()
    assert versioned.json()["meta"]["account_coverage"]["included_account_ids"] == [
        account.account_id
    ]

    provenance = client.get(f"/api/v1/valuation-observations/{opening.observation_key}")
    assert provenance.status_code == 200
    provenance_payload = provenance.json()
    assert provenance_payload["source_provider"] == "snaptrade"
    assert provenance_payload["has_source_record_id"] is True
    assert "source_reference" not in provenance_payload
    assert "source_record_id" not in provenance_payload
    assert "total_value" not in provenance_payload


def test_tampered_account_valuation_is_rejected_by_calculation_and_receipt_api(
    client, session
) -> None:
    start = date(2026, 8, 3)
    end = date(2026, 8, 28)
    account = _seed_snapshot_account(session, account_key="tampered-total")
    opening = _valuation_row(
        account_id=account.account_id,
        as_of_date=start,
        total_value=Decimal(1000),
        source_provider="plaid",
        source_reference="accounts[].balances.current",
        source_record_id="tampered-total-start",
        fetched_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    closing = _valuation_row(
        account_id=account.account_id,
        as_of_date=end,
        total_value=Decimal(1100),
        source_provider="plaid",
        source_reference="accounts[].balances.current",
        source_record_id="tampered-total-end",
        fetched_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    session.add_all([opening, closing])
    session.commit()
    opening.total_value = Decimal(9999)
    session.commit()

    complete = _complete_account_valuations_by_date(session, start, end)
    assert start not in complete
    assert end in complete

    provenance = client.get(f"/api/v1/valuation-observations/{opening.observation_key}")
    assert provenance.status_code == 409
    assert provenance.json()["detail"] == "valuation observation failed integrity validation"

    assessment = _daily_portfolio_value_assessment(session, start, end)
    assert start not in assessment.values
    assert "account_valuation_integrity_invalid" in assessment.calculation_reason_codes


def test_partial_account_total_boundary_has_precise_unavailable_reason(client, session) -> None:
    start = date(2026, 8, 3)
    end = date(2026, 8, 28)
    item = Item(
        source="snaptrade",
        plaid_item_id="partial-total-item",
        institution_name="Broker",
        is_data_active=True,
    )
    account = Account(
        item=item,
        plaid_account_id="partial-total-account",
        name="Partially observed account",
        type="investment",
    )
    session.add(account)
    session.flush()
    session.add_all(
        [
            AccountValuationObservation(
                observation_key="f" * 64,
                account_id=account.account_id,
                as_of_date=start,
                total_value=Decimal(1000),
                cash_value=Decimal(0),
                currency="USD",
                source_kind="provider_api",
                source_provider="snaptrade",
                source_reference="account.balance.total",
                source_record_id="complete-start",
                fetched_at=datetime(2026, 8, 29, tzinfo=UTC),
                is_complete=True,
                is_empty=False,
            ),
            AccountValuationObservation(
                observation_key="9" * 64,
                account_id=account.account_id,
                as_of_date=end,
                total_value=Decimal(1100),
                cash_value=Decimal(0),
                currency="USD",
                source_kind="provider_api",
                source_provider="snaptrade",
                source_reference="account.balance.total",
                source_record_id="partial-end",
                fetched_at=datetime(2026, 8, 29, tzinfo=UTC),
                is_complete=False,
                is_empty=False,
            ),
        ]
    )
    session.commit()

    response = client.get(
        "/api/portfolio/performance",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["calculation_status"] == "unavailable"
    assert "partial_account_valuation_end_date" in payload["calculation_reason_codes"]
    assert "portfolio_end_value_unavailable" in payload["calculation_reason_codes"]


def test_newer_incomplete_observation_invalidates_same_day_complete_value(session) -> None:
    boundary = date(2026, 8, 28)
    account = _seed_snapshot_account(session, account_key="freshness-invalidates")
    session.add_all(
        [
            AccountValuationObservation(
                observation_key=key,
                account_id=account.account_id,
                as_of_date=boundary,
                total_value=value,
                cash_value=Decimal(0),
                currency="USD",
                source_kind="provider_api",
                source_provider="snaptrade",
                source_reference="account.balance.total",
                source_record_id="freshness-invalidates",
                fetched_at=fetched_at,
                is_complete=is_complete,
                is_empty=False,
            )
            for key, value, fetched_at, is_complete in (
                ("6" * 64, Decimal(1000), datetime(2026, 8, 28, 9, tzinfo=UTC), True),
                ("7" * 64, Decimal(1000), datetime(2026, 8, 28, 10, tzinfo=UTC), False),
            )
        ]
    )
    session.commit()

    complete = _complete_account_valuations_by_date(session, boundary, boundary)

    assert boundary not in complete


def test_incomplete_account_total_does_not_poison_matched_holdings_boundaries(session) -> None:
    start = date(2026, 8, 3)
    end = date(2026, 8, 28)
    account = _seed_snapshot_account(session, account_key="holdings-with-partial-total")
    security = Security(
        plaid_security_id="holdings-with-partial-total-security",
        ticker="HOLD",
        type="equity",
    )
    session.add(security)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=as_of,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=value,
            )
            for as_of, value in ((start, Decimal(100)), (end, Decimal(110)))
        ]
        + [
            _valuation_row(
                account_id=account.account_id,
                as_of_date=end,
                total_value=Decimal(110),
                source_provider="plaid",
                source_reference="accounts[].balances.current",
                source_record_id="partial-optional-total",
                fetched_at=datetime(2026, 8, 29, tzinfo=UTC),
                is_complete=False,
            )
        ]
    )
    session.commit()

    assessment = _daily_portfolio_value_assessment(session, start, end)

    assert assessment.provenance[start] == "observed_complete_snapshot"
    assert assessment.provenance[end] == "observed_complete_snapshot"
    assert "partial_account_valuation_end_date" not in assessment.calculation_reason_codes
    assert "portfolio_account_universe_coverage_incomplete" not in (
        assessment.calculation_reason_codes
    )


def test_incomplete_current_total_does_not_poison_modeled_opening(session) -> None:
    start = date(2026, 8, 3)
    end = date(2026, 8, 28)
    account = _seed_snapshot_account(session, account_key="modeled-with-partial-total")
    security = Security(
        plaid_security_id="modeled-with-partial-total-security",
        ticker="MODEL",
        type="equity",
    )
    session.add(security)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=Decimal(110),
            ),
            Price(
                security_id=security.security_id,
                date=start,
                close=Decimal(100),
                source=PriceSource.YFINANCE.value,
                adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
            ),
            _valuation_row(
                account_id=account.account_id,
                as_of_date=end,
                total_value=Decimal(110),
                source_provider="plaid",
                source_reference="accounts[].balances.current",
                source_record_id="partial-current-total",
                fetched_at=datetime(2026, 8, 29, tzinfo=UTC),
                is_complete=False,
            ),
        ]
    )
    session.commit()

    assessment = _daily_portfolio_value_assessment(session, start, end)

    assert assessment.provenance[start] == "modeled_transaction_walkback"
    assert assessment.provenance[end] == "observed_complete_snapshot"
    assert "partial_account_valuation_end_date" not in assessment.calculation_reason_codes
    assert "portfolio_account_universe_coverage_incomplete" not in (
        assessment.calculation_reason_codes
    )


def test_active_investment_account_without_value_evidence_cannot_disappear(client, session) -> None:
    start = date(2026, 8, 3)
    end = date(2026, 8, 28)
    observed = _seed_snapshot_account(session, account_key="observed-total")
    missing = _seed_snapshot_account(session, account_key="missing-total")
    session.add_all(
        [
            AccountValuationObservation(
                observation_key=key,
                account_id=observed.account_id,
                as_of_date=as_of,
                total_value=value,
                cash_value=Decimal(0),
                currency="USD",
                source_kind="provider_api",
                source_provider="plaid",
                source_reference="accounts[].balances.current",
                source_record_id="observed-account",
                fetched_at=datetime(2026, 8, 29, tzinfo=UTC),
                is_complete=True,
                is_empty=False,
            )
            for key, as_of, value in (
                ("1" * 64, start, Decimal(1000)),
                ("2" * 64, end, Decimal(1100)),
            )
        ]
        + [
            InvestmentTransaction(
                plaid_investment_transaction_id="missing-account-deposit",
                account_id=missing.account_id,
                date=start + timedelta(days=10),
                name="Deposit",
                quantity=Decimal(0),
                amount=Decimal(-500),
                type="cash",
                subtype="deposit",
                currency="USD",
            )
        ]
    )
    session.commit()

    response = client.get(
        "/api/portfolio/performance",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["calculation_status"] == "unavailable"
    assert payload["valuation_account_ids"] == sorted([observed.account_id, missing.account_id])
    assert "portfolio_account_universe_coverage_incomplete" in payload["calculation_reason_codes"]
    assert [row["account_id"] for row in payload["source_coverage"]["accounts"]] == sorted(
        [observed.account_id, missing.account_id]
    )

    cash_flows = client.get(
        "/api/v1/cash-flows",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
    ).json()
    assert cash_flows["is_complete"] is False
    assert [row["account_id"] for row in cash_flows["source_coverage"]["accounts"]] == sorted(
        [observed.account_id, missing.account_id]
    )


def test_account_total_on_only_one_boundary_keeps_matched_holdings_basis(client, session) -> None:
    start = date(2026, 8, 3)
    end = date(2026, 8, 28)
    security = Security(plaid_security_id="basis-security", ticker="BASIS", type="cs")
    account = _seed_snapshot_account(session, account_key="basis-account")
    session.add(security)
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=as_of,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=value,
            )
            for as_of, value in ((start, Decimal(900)), (end, Decimal(1100)))
        ]
        + [
            AccountValuationObservation(
                observation_key="3" * 64,
                account_id=account.account_id,
                as_of_date=start,
                total_value=Decimal(1000),
                cash_value=Decimal(100),
                currency="USD",
                source_kind="provider_api",
                source_provider="plaid",
                source_reference="accounts[].balances.current",
                source_record_id="basis-account",
                fetched_at=datetime(2026, 8, 4, tzinfo=UTC),
                is_complete=True,
                is_empty=False,
            )
        ]
    )
    session.commit()

    payload = client.get(
        "/api/portfolio/performance",
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
    ).json()

    assert payload["opening_value_provenance"] == "observed_complete_snapshot"
    assert payload["ending_value_provenance"] == "observed_complete_snapshot"
    assert payload["base_value"] == "900.000000"
    assert "account_valuation_boundary_basis_mismatch" not in payload["calculation_reason_codes"]


def test_account_total_only_book_rejects_index_exclusion(client, session) -> None:
    start = date(2026, 8, 3)
    end = date(2026, 8, 28)
    account = _seed_snapshot_account(session, account_key="total-only-index")
    session.add_all(
        [
            _valuation_row(
                account_id=account.account_id,
                as_of_date=as_of,
                total_value=value,
                source_provider="plaid",
                source_reference="accounts[].balances.current",
                source_record_id="total-only-index",
                fetched_at=datetime(2026, 8, 29, tzinfo=UTC),
            )
            for as_of, value in ((start, Decimal(1000)), (end, Decimal(1100)))
        ]
    )
    session.commit()

    payload = client.get(
        "/api/portfolio/performance",
        params={
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "exclude_index_etfs": True,
        },
    ).json()

    assert payload["calculation_status"] == "unavailable"
    assert "account_valuation_index_exclusion_unsupported" in payload["calculation_reason_codes"]


def test_drawdown_and_risk_defaults_share_performance_observation_window(client, session) -> None:
    observed = date(2026, 8, 28)
    security = Security(plaid_security_id="risk-window-security", ticker="RISK", type="cs")
    account = _seed_snapshot_account(session, account_key="risk-window")
    session.add(security)
    session.flush()
    session.add(
        HoldingSnapshot(
            snapshot_date=observed,
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal(1),
            institution_value=Decimal(100),
        )
    )
    session.commit()

    legacy = client.get("/api/portfolio/drawdown").json()
    combined = client.get("/api/v1/analytics/risk").json()
    drawdown = client.get("/api/v1/analytics/drawdown").json()

    assert legacy["start_date"] == legacy["end_date"] == observed.isoformat()
    assert combined["drawdown"]["start_date"] == observed.isoformat()
    assert combined["drawdown"]["end_date"] == observed.isoformat()
    assert drawdown["drawdown"] == combined["drawdown"]

    explicit_end = observed + timedelta(days=3)
    explicit = client.get(
        "/api/v1/analytics/drawdown",
        params={"start_date": observed.isoformat(), "end_date": explicit_end.isoformat()},
    ).json()
    assert explicit["drawdown"]["start_date"] == observed.isoformat()
    assert explicit["drawdown"]["end_date"] == explicit_end.isoformat()
