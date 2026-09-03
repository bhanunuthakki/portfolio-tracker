"""Unit tests for the pure drawdown / Calmar math."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    Benchmark,
    CashFlowSourceAttestation,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
)
from portfolio_tracker.services.drawdown import compute_drawdown, drawdown_from_index

_START = date(2025, 1, 1)
_END = date(2025, 1, 5)


def _pts(*equities: str) -> list[tuple[date, Decimal]]:
    return [(date(2025, 1, i + 1), Decimal(e)) for i, e in enumerate(equities)]


def test_drawdown_peak_trough_recovery():
    # 1.00 -> 1.20 (peak) -> 0.90 (-25% DD) -> 1.10 (still under) -> 1.30 (recovered).
    res = drawdown_from_index(_START, _END, _pts("1.00", "1.20", "0.90", "1.10", "1.30"))
    assert res.max_drawdown_pct == Decimal("-25.00")
    assert res.peak_date == date(2025, 1, 2)
    assert res.trough_date == date(2025, 1, 3)
    assert res.recovery_date == date(2025, 1, 5)
    assert res.days_to_recovery == 2
    assert res.current_drawdown_pct == Decimal("0.00")  # ends at a new peak
    assert res.annualized_return_pct is not None
    assert res.calmar is not None and res.calmar > 0
    assert res.calculation_status == "available"
    assert res.calculation_reason_codes == []


def test_drawdown_still_underwater_has_no_recovery():
    res = drawdown_from_index(_START, _END, _pts("1.00", "1.20", "0.90"))
    assert res.max_drawdown_pct == Decimal("-25.00")
    assert res.recovery_date is None
    assert res.days_to_recovery is None
    assert res.current_drawdown_pct == Decimal("-25.00")  # still at the trough


def test_drawdown_monotonic_rise_has_zero_drawdown():
    res = drawdown_from_index(_START, _END, _pts("1.00", "1.05", "1.10"))
    assert res.max_drawdown_pct == Decimal("0.00")
    assert res.calmar is None  # no drawdown -> Calmar undefined


def test_drawdown_too_few_points_is_empty():
    res = drawdown_from_index(_START, _END, _pts("1.00"))
    assert res.max_drawdown_pct is None
    assert res.underwater == []
    assert res.calculation_status == "unavailable"
    assert res.calculation_reason_codes == ["insufficient_return_observations"]


def test_drawdown_propagates_unpriceable_external_share_transfer(session):
    item = Item(
        source="plaid",
        plaid_item_id="item-1",
        institution_name="Broker",
        is_data_active=True,
    )
    account = Account(
        item=item,
        plaid_account_id="account-1",
        name="Brokerage",
        type="investment",
    )
    security = Security(plaid_security_id="security-1", ticker="ACME", type="cs")
    session.add_all([item, account, security])
    session.flush()
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=_START,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(0),
                institution_value=Decimal(0),
            ),
            HoldingSnapshot(
                snapshot_date=_END,
                account_id=account.account_id,
                security_id=security.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(1000),
            ),
            InvestmentTransaction(
                plaid_investment_transaction_id="transfer-1",
                account_id=account.account_id,
                security_id=security.security_id,
                date=date(2025, 1, 3),
                type="transfer",
                subtype="transfer",
                quantity=Decimal(10),
                amount=Decimal(0),
            ),
            Benchmark(symbol="SPY", date=date(2024, 12, 31), close=Decimal(100)),
            Benchmark(symbol="SPY", date=_START, close=Decimal(100)),
            Benchmark(symbol="SPY", date=date(2025, 1, 3), close=Decimal(100)),
            Benchmark(symbol="SPY", date=_END, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=date(2024, 12, 31), close=Decimal(100)),
            Benchmark(symbol="QQQ", date=_START, close=Decimal(100)),
            Benchmark(symbol="QQQ", date=date(2025, 1, 3), close=Decimal(100)),
            Benchmark(symbol="QQQ", date=_END, close=Decimal(100)),
            CashFlowSourceAttestation(
                attestation_key="synthetic-drawdown-source",
                account_id=account.account_id,
                coverage_start=date(2025, 1, 2),
                coverage_end=_END,
                source_type="provider_export",
                source_reference="synthetic:drawdown-test",
                source_sha256="e" * 64,
                captured_at=datetime(2025, 2, 1, tzinfo=UTC),
                approved_at=datetime(2025, 2, 2, tzinfo=UTC),
                methodology_version="1",
            ),
        ]
    )
    session.commit()

    result = compute_drawdown(session, _START, _END)

    assert result.calculation_status == "unavailable"
    assert result.calculation_reason_codes == ["external_share_movement_price_unavailable"]
    assert result.max_drawdown_pct is None
    assert result.annualized_return_pct is None
    assert result.underwater == []
