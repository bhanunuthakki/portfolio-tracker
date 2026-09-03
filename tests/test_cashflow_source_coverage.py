from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    Benchmark,
    CashFlowSourceAttestation,
    CashFlowSourceGap,
    HoldingSnapshot,
    Item,
    Security,
)
from portfolio_tracker.services.cashflow_source_coverage import assess_cashflow_source_coverage
from portfolio_tracker.services.performance import compute_performance_series


_CAPTURED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
_APPROVED_AT = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _valued_account(session, suffix: str) -> Account:
    item = Item(
        source="plaid",
        plaid_item_id=f"item-{suffix}",
        institution_name=f"Broker {suffix}",
        is_data_active=True,
    )
    account = Account(
        item=item,
        plaid_account_id=f"account-{suffix}",
        name=f"Account {suffix}",
        type="investment",
    )
    security = Security(plaid_security_id=f"security-{suffix}", ticker=f"T{suffix}")
    session.add_all([item, account, security])
    session.flush()
    session.add(
        HoldingSnapshot(
            snapshot_date=date(2026, 9, 1),
            account_id=account.account_id,
            security_id=security.security_id,
            quantity=Decimal(1),
            institution_value=Decimal(100),
        )
    )
    return account


def _attestation(
    session,
    account: Account,
    *,
    key: str,
    start: date,
    end: date,
    approved: bool = True,
    digest_char: str = "a",
) -> CashFlowSourceAttestation:
    row = CashFlowSourceAttestation(
        attestation_key=key,
        account_id=account.account_id,
        coverage_start=start,
        coverage_end=end,
        source_type="brokerage_statement",
        source_reference=f"synthetic:{key}",
        source_sha256=digest_char * 64,
        captured_at=_CAPTURED_AT,
        approved_at=_APPROVED_AT if approved else None,
        methodology_version="1",
    )
    session.add(row)
    session.flush()
    return row


def test_missing_attestation_is_not_inferred_complete(session):
    account = _valued_account(session, "missing")
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({account.account_id}),
    )

    assert result.is_complete is False
    assert result.status == "missing"
    assert result.required_start_date == date(2026, 1, 2)
    assert result.required_end_date == date(2026, 1, 31)
    assert result.accounts[0].uncovered_ranges == ((date(2026, 1, 2), date(2026, 1, 31)),)
    assert result.attestations == ()


def test_partial_and_explicit_gap_remain_uncovered(session):
    account = _valued_account(session, "partial")
    row = _attestation(
        session,
        account,
        key="partial-attestation",
        start=date(2026, 1, 2),
        end=date(2026, 1, 20),
    )
    session.add(
        CashFlowSourceGap(
            attestation_id=row.attestation_id,
            gap_start=date(2026, 1, 10),
            gap_end=date(2026, 1, 12),
            reason_code="provider_history_unavailable",
        )
    )
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({account.account_id}),
    )

    assert result.is_complete is False
    assert result.status == "partial"
    assert result.accounts[0].covered_ranges == (
        (date(2026, 1, 2), date(2026, 1, 9)),
        (date(2026, 1, 13), date(2026, 1, 20)),
    )
    assert result.accounts[0].uncovered_ranges == (
        (date(2026, 1, 10), date(2026, 1, 12)),
        (date(2026, 1, 21), date(2026, 1, 31)),
    )
    assert result.attestations[0].source_sha256 == "a" * 64
    assert result.attestations[0].gaps[0].reason_code == "provider_history_unavailable"


def test_superseded_attestation_does_not_count(session):
    account = _valued_account(session, "superseded")
    old = _attestation(
        session,
        account,
        key="old-attestation",
        start=date(2026, 1, 2),
        end=date(2026, 1, 31),
    )
    replacement = _attestation(
        session,
        account,
        key="replacement-attestation",
        start=date(2026, 1, 15),
        end=date(2026, 1, 31),
        digest_char="b",
    )
    old.superseded_at = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    old.superseded_by_attestation_id = replacement.attestation_id
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({account.account_id}),
    )

    assert result.is_complete is False
    assert result.accounts[0].uncovered_ranges == ((date(2026, 1, 2), date(2026, 1, 14)),)
    assert [row.attestation_key for row in result.attestations] == [
        "old-attestation",
        "replacement-attestation",
    ]
    assert result.attestations[0].lifecycle_status == "superseded"
    assert result.attestations[0].superseded_by_attestation_key == "replacement-attestation"


def test_all_accounts_must_be_fully_attested(session):
    first = _valued_account(session, "first")
    second = _valued_account(session, "second")
    for account, key, digest in (
        (first, "first-statement", "c"),
        (second, "second-provider-export", "d"),
    ):
        _attestation(
            session,
            account,
            key=key,
            start=date(2026, 1, 2),
            end=date(2026, 1, 31),
            digest_char=digest,
        )
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({first.account_id, second.account_id}),
    )

    assert result.is_complete is True
    assert result.status == "complete"
    assert all(not account.uncovered_ranges for account in result.accounts)


def test_draft_attestation_does_not_count(session):
    account = _valued_account(session, "draft")
    _attestation(
        session,
        account,
        key="draft-attestation",
        start=date(2026, 1, 2),
        end=date(2026, 1, 31),
        approved=False,
    )
    session.commit()

    result = assess_cashflow_source_coverage(
        session,
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_ids=frozenset({account.account_id}),
    )

    assert result.is_complete is False
    assert result.status == "missing"
    assert len(result.attestations) == 1
    assert result.attestations[0].lifecycle_status == "draft"


def test_cash_flow_api_distinguishes_structural_and_source_completeness(client, session):
    account = _valued_account(session, "api")
    session.commit()

    response = client.get(
        "/api/v1/cash-flows",
        params={"start_date": "2026-01-01", "end_date": "2026-01-31"},
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["structural_is_complete"] is True
    assert payload["source_coverage"]["status"] == "missing"
    assert payload["source_coverage"]["is_complete"] is False
    assert payload["is_complete"] is False
    assert payload["net_external_cashflow_in"] is None

    _attestation(
        session,
        account,
        key="api-attestation",
        start=date(2026, 1, 2),
        end=date(2026, 1, 31),
    )
    session.commit()

    payload = client.get(
        "/api/v1/cash-flows",
        params={"start_date": "2026-01-01", "end_date": "2026-01-31"},
    ).json()
    assert payload["structural_is_complete"] is True
    assert payload["source_coverage"]["status"] == "complete"
    assert payload["is_complete"] is True
    assert Decimal(payload["net_external_cashflow_in"]) == 0


def test_performance_fails_closed_until_source_window_is_attested(session):
    account = _valued_account(session, "performance")
    security_id = session.query(Security.security_id).filter_by(ticker="Tperformance").scalar()
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=start,
                account_id=account.account_id,
                security_id=security_id,
                quantity=Decimal(1),
                institution_value=Decimal(100),
            ),
            HoldingSnapshot(
                snapshot_date=end,
                account_id=account.account_id,
                security_id=security_id,
                quantity=Decimal(1),
                institution_value=Decimal(110),
            ),
            Benchmark(symbol="SPY", date=date(2025, 12, 31), close=Decimal(100)),
            Benchmark(symbol="SPY", date=start, close=Decimal(100)),
            Benchmark(symbol="SPY", date=date(2026, 1, 30), close=Decimal(110)),
            Benchmark(symbol="SPY", date=end, close=Decimal(110)),
            Benchmark(symbol="QQQ", date=date(2025, 12, 31), close=Decimal(100)),
            Benchmark(symbol="QQQ", date=start, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=date(2026, 1, 30), close=Decimal(110)),
            Benchmark(symbol="QQQ", date=end, close=Decimal(110)),
        ]
    )
    session.commit()

    unavailable = compute_performance_series(session, start, end)

    assert unavailable.calculation_status == "unavailable"
    assert unavailable.calculation_reason_codes == ["external_flow_source_coverage_incomplete"]
    assert unavailable.source_coverage.status == "missing"
    assert unavailable.net_external_cashflow_in is None

    _attestation(
        session,
        account,
        key="performance-attestation",
        start=start + timedelta(days=1),
        end=end,
    )
    session.commit()

    available = compute_performance_series(session, start, end)
    assert available.calculation_status == "available"
    assert available.calculation_reason_codes == []
    assert available.source_coverage.is_complete is True
    assert available.net_external_cashflow_in == 0
