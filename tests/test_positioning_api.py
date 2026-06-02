"""HTTP surface for positioning + the security-classification override."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    HoldingSnapshot,
    Item,
    Security,
    SecurityClassification,
)


def _seed_min(session) -> int:
    item = Item(source="plaid", plaid_item_id="i1", institution_name="RH", is_data_active=True)
    session.add(item)
    session.flush()
    acct = Account(
        item_id=item.item_id,
        plaid_account_id="a1",
        name="Robinhood Individual",
        type="investment",
        subtype="INDIVIDUAL",
    )
    session.add(acct)
    session.flush()
    nvda = Security(plaid_security_id="s1", ticker="NVDA", type="cs", is_cash_equivalent=False)
    cash = Security(plaid_security_id="s2", ticker="SGOV", type="oef", is_cash_equivalent=True)
    session.add_all([nvda, cash])
    session.flush()
    session.add(
        SecurityClassification(
            security_id=nvda.security_id, sector="Technology", region="US", source="auto"
        )
    )
    d = date(2025, 6, 2)
    session.add_all(
        [
            HoldingSnapshot(
                snapshot_date=d,
                account_id=acct.account_id,
                security_id=nvda.security_id,
                quantity=Decimal(10),
                institution_value=Decimal(3000),
            ),
            HoldingSnapshot(
                snapshot_date=d,
                account_id=acct.account_id,
                security_id=cash.security_id,
                quantity=Decimal(1000),
                institution_value=Decimal(1000),
            ),
        ]
    )
    session.commit()
    return nvda.security_id


def test_get_positioning(client, session):
    _seed_min(session)
    resp = client.get("/api/portfolio/positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert float(data["total_value"]) == 4000.0
    by_type = {b["label"]: float(b["value"]) for b in data["by_asset_type"]}
    assert by_type["Stock"] == 3000.0
    assert by_type["Cash"] == 1000.0
    assert data["concentration"]["num_positions"] == 2
    corr_tickers = {r["ticker"] for r in data["correlations"]}
    assert "SGOV" not in corr_tickers  # cash excluded


def test_get_positioning_empty_book(client):
    resp = client.get("/api/portfolio/positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert float(data["total_value"]) == 0.0
    assert data["by_asset_type"] == []


def test_security_classification_override_crud(client, session):
    sid = _seed_min(session)
    put = client.put(
        "/api/overrides/security-classification",
        json={"security_id": sid, "sector": "Semiconductors", "region": "US", "notes": "n"},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["sector"] == "Semiconductors"
    assert body["source"] == "manual"
    assert body["ticker"] == "NVDA"

    listed = client.get("/api/overrides/security-classification").json()
    assert any(x["security_id"] == sid for x in listed)

    deleted = client.delete(f"/api/overrides/security-classification/{sid}")
    assert deleted.status_code == 204
    after = client.get("/api/overrides/security-classification").json()
    assert all(x["security_id"] != sid for x in after)


def test_put_classification_unknown_security_404(client, session):
    _seed_min(session)
    resp = client.put(
        "/api/overrides/security-classification",
        json={"security_id": 999999, "sector": "X"},
    )
    assert resp.status_code == 404
