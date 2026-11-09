from datetime import UTC, date, datetime
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    AccountValuationSourceKind,
    HoldingSnapshot,
    Item,
    PortfolioValueDaily,
    Security,
)
from portfolio_tracker.services.account_valuations import (
    NewAccountValuationObservation,
    record_account_valuation_observation,
)


def test_weekly_values_keep_actual_dates_and_separate_coverage(client, session):
    monday = date(2026, 5, 11)
    thursday = date(2026, 5, 14)
    friday = date(2026, 5, 15)
    following_monday = date(2026, 5, 18)
    plaid = Item(source="plaid", plaid_item_id="plaid", is_data_active=True)
    snaptrade = Item(source="snaptrade", plaid_item_id="snaptrade", is_data_active=True)
    account = Account(item=plaid, plaid_account_id="a", name="a", type="investment")
    other = Account(item=snaptrade, plaid_account_id="b", name="b", type="investment")
    stock = Security(plaid_security_id="stock")
    unmarked = Security(plaid_security_id="unmarked")
    session.add_all([account, other, stock, unmarked])
    session.flush()

    for day, owner, security, value in [
        (monday, account, stock, Decimal("800")),
        (thursday, account, stock, Decimal("950")),
        (thursday, other, stock, Decimal("400")),
        (thursday, other, unmarked, None),
        (following_monday, other, stock, Decimal("1800")),
    ]:
        session.add(
            HoldingSnapshot(
                snapshot_date=day,
                account_id=owner.account_id,
                security_id=security.security_id,
                quantity=Decimal(1),
                institution_value=value,
            )
        )
    for day, owner, value in [
        (thursday, account, Decimal("1100")),
        (friday, account, Decimal("1200")),
        (friday, other, Decimal("500")),
    ]:
        record_account_valuation_observation(
            session,
            NewAccountValuationObservation(
                account_id=owner.account_id,
                as_of_date=day,
                total_value=value,
                cash_value=None,
                currency="USD",
                source_kind=AccountValuationSourceKind.PROVIDER_API,
                source_provider=owner.item.source,
                source_reference="test",
                source_record_id=f"{owner.account_id}:{day}",
                fetched_at=datetime(2026, 5, 16, tzinfo=UTC),
                is_complete=True,
                is_empty=False,
            ),
        )
    session.add_all(
        [
            PortfolioValueDaily(date=monday, total_value=Decimal("1500"), source="backfill"),
            PortfolioValueDaily(date=thursday, total_value=Decimal("1400"), source="snapshot"),
        ]
    )
    session.commit()

    response = client.get("/api/v1/weekly-values", params={"end_date": str(friday)})
    assert response.status_code == 200
    result = response.json()
    assert result["expected_account_ids"] == [account.account_id, other.account_id]
    assert {point["week_start"] for point in result["points"]} == {str(monday)}
    points = {point["basis"]: point for point in result["points"]}
    assert set(points) == {
        "whole_account",
        "marked_positions",
        "legacy_cached_snapshot",
        "legacy_cached_model",
    }
    observed = points["whole_account"]
    assert observed["as_of"] == str(friday)
    assert Decimal(observed["value"]) == Decimal("1700")
    assert len(observed["valuation_observation_keys"]) == 2
    positions = points["marked_positions"]
    assert positions["as_of"] == str(thursday)
    assert Decimal(positions["value"]) == Decimal("950")
    assert positions["covered_account_ids"] == [account.account_id]
    assert positions["source_providers"] == ["plaid"]
    assert points["legacy_cached_model"]["as_of"] == str(monday)
    assert points["legacy_cached_model"]["covered_account_ids"] is None
