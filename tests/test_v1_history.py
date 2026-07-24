"""Contract tests for the Slice-2 `/api/v1` history + analytics resources:
cursor pagination (no silent caps), TWR-consistent cash-flow classification,
position-snapshot origin markers, the securities master, structured cursor
errors, and deprecation headers on superseded legacy endpoints.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
    TransactionOverride,
)

_TODAY = date.today()
_FRESH = _TODAY - timedelta(days=1)


def _seed(session):
    item = Item(
        source="plaid",
        plaid_item_id="i1",
        institution_name="Broker",
        is_data_active=True,
        last_refreshed_at=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    session.add(item)
    session.flush()
    acct = Account(
        item_id=item.item_id,
        plaid_account_id="a1",
        name="Brokerage",
        type="brokerage",
        subtype="brokerage",
    )
    session.add(acct)
    session.flush()
    sec = Security(plaid_security_id="s1", ticker="AAPL", type="cs")
    session.add(sec)
    session.flush()

    def tx(txid: str, d: date, type_: str, subtype: str | None, amount: str, name: str | None):
        session.add(
            InvestmentTransaction(
                plaid_investment_transaction_id=txid,
                account_id=acct.account_id,
                security_id=None,
                date=d,
                name=name,
                quantity=Decimal(0),
                amount=Decimal(amount),
                type=type_,
                subtype=subtype,
                currency="USD",
            )
        )

    # 5 transactions across 3 days: two external deposits, one withdrawal,
    # one dividend (internal by name hint), one buy (not cashflow-shaped).
    tx("t5", _FRESH, "cash", "deposit", "1000", "ACH deposit")
    tx("t4", _FRESH, "cash", "withdrawal", "200", "ACH withdrawal")
    tx("t3", _FRESH - timedelta(days=1), "cash", "deposit", "500", "ACH deposit")
    tx("t2", _FRESH - timedelta(days=2), "cash", "withdrawal", "50", "cash - DIVIDEND USD")
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="t1",
            account_id=acct.account_id,
            security_id=sec.security_id,
            date=_FRESH - timedelta(days=2),
            name="Buy AAPL",
            quantity=Decimal(1),
            amount=Decimal(-100),
            type="buy",
            subtype="buy",
            currency="USD",
        )
    )
    session.add(
        HoldingSnapshot(
            snapshot_date=_FRESH,
            account_id=acct.account_id,
            security_id=sec.security_id,
            quantity=Decimal(10),
            institution_value=Decimal(1000),
            cost_basis=Decimal(900),
        )
    )
    session.commit()
    return acct


def _walk_pages(client, path: str, params: dict, items_key: str) -> list[dict]:
    """Follow next_cursor until exhaustion; return every item."""
    items: list[dict] = []
    cursor = None
    for _ in range(20):  # hard stop against accidental infinite loops
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        data = client.get(path, params=q).json()
        items.extend(data[items_key])
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return items


def test_transactions_pagination_no_silent_cap(client, session):
    _seed(session)
    # Page size 2 over 5 rows: the cursor walk must return all 5, newest first.
    rows = _walk_pages(client, "/api/v1/transactions", {"limit": 2}, "transactions")
    assert [r["transaction_id"] for r in rows] == ["t5", "t4", "t3", "t2", "t1"]

    first = client.get("/api/v1/transactions", params={"limit": 2}).json()
    assert first["next_cursor"] is not None
    assert first["meta"]["schema_version"] == "1.0.0"
    # Effective classification mirrors the TWR pipeline.
    by_id = {r["transaction_id"]: r for r in rows}
    assert by_id["t5"]["effective_classification"] == "external_in"
    assert by_id["t4"]["effective_classification"] == "external_out"
    assert by_id["t2"]["effective_classification"] == "internal"  # dividend name hint
    assert by_id["t1"]["effective_classification"] is None  # buy: not cashflow-shaped


def test_cash_flows_match_twr_semantics(client, session):
    _seed(session)
    data = client.get("/api/v1/cash-flows").json()
    flows = {f["transaction_id"]: f for f in data["cash_flows"]}
    # External only by default: deposits +, withdrawal −, dividend/buy excluded.
    assert set(flows) == {"t5", "t4", "t3"}
    assert float(flows["t5"]["signed_external_amount"]) == 1000.0
    assert float(flows["t4"]["signed_external_amount"]) == -200.0
    assert float(data["net_external_cashflow_in"]) == 1300.0
    assert flows["t5"]["classification_source"] == "heuristic"

    with_internal = client.get("/api/v1/cash-flows", params={"include_internal": "true"}).json()
    ids = {f["transaction_id"] for f in with_internal["cash_flows"]}
    assert "t2" in ids  # the dividend, zeroed
    internal = next(f for f in with_internal["cash_flows"] if f["transaction_id"] == "t2")
    assert float(internal["signed_external_amount"]) == 0.0
    assert internal["classification"] == "internal"


def test_cash_flows_respect_override(client, session):
    acct = _seed(session)
    del acct
    session.add(
        TransactionOverride(
            plaid_investment_transaction_id="t5",
            classification="internal",
            notes="test override",
        )
    )
    session.commit()
    data = client.get("/api/v1/cash-flows").json()
    ids = {f["transaction_id"] for f in data["cash_flows"]}
    assert "t5" not in ids  # overridden to internal, excluded from external list
    assert float(data["net_external_cashflow_in"]) == 300.0  # 500 − 200
    with_internal = client.get("/api/v1/cash-flows", params={"include_internal": "true"}).json()
    t5 = next(f for f in with_internal["cash_flows"] if f["transaction_id"] == "t5")
    assert t5["classification_source"] == "override"


def test_cash_flows_pagination_walks_past_filtered_rows(client, session):
    _seed(session)
    rows = _walk_pages(client, "/api/v1/cash-flows", {"limit": 1}, "cash_flows")
    assert [r["transaction_id"] for r in rows] == ["t5", "t4", "t3"]


def test_position_snapshots(client, session):
    _seed(session)
    data = client.get("/api/v1/position-snapshots").json()
    assert len(data["snapshots"]) == 1
    row = data["snapshots"][0]
    assert row["snapshot_date"] == _FRESH.isoformat()
    assert row["origin"] == "broker"
    assert float(row["institution_value"]) == 1000.0


def test_securities_master(client, session):
    _seed(session)
    data = client.get("/api/v1/securities").json()
    assert len(data["securities"]) == 1
    sec = data["securities"][0]
    assert sec["ticker"] == "AAPL"
    assert sec["is_cash_equivalent"] is False
    assert sec["asset_type"] == "Stock"
    assert sec["classification_source"] is None


def test_invalid_cursor_is_structured_error(client, session):
    _seed(session)
    resp = client.get("/api/v1/transactions", params={"cursor": "@@not-base64@@"})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "INVALID_CURSOR"
    assert err["retryable"] is False
    assert err["request_id"]
    assert "cursor" in err["recovery"].lower() or "page" in err["recovery"].lower()


def test_data_quality_enveloped(client, session):
    _seed(session)
    data = client.get("/api/v1/data-quality").json()
    assert data["meta"]["schema_version"] == "1.0.0"
    assert "findings" in data["report"]
    assert "summary_counts" in data["report"]


def test_analytics_wrappers_enveloped(client, session):
    _seed(session)
    perf = client.get("/api/v1/analytics/performance").json()
    assert perf["meta"]["methodology"] == "performance.modified_dietz"
    assert perf["meta"]["as_of"] == _FRESH.isoformat()  # holdings date, not query end
    assert "points" in perf["series"]

    alpha = client.get("/api/v1/analytics/position-performance").json()
    assert alpha["meta"]["methodology"] == "position_alpha.dollar_matched_counterfactual"

    risk = client.get("/api/v1/analytics/risk").json()
    assert risk["meta"]["methodology"] == "risk.beta_drawdown"
    assert "beta" in risk and "drawdown" in risk

    exits = client.get("/api/v1/analytics/exit-quality").json()
    assert exits["meta"]["methodology"] == "exit_quality.repricing"


def test_deprecation_headers_on_legacy_endpoints(client, session):
    _seed(session)
    resp = client.get("/api/portfolio/transactions")
    assert resp.headers.get("Deprecation") == "true"
    assert "/api/v1/transactions" in resp.headers.get("Link", "")
    # v1 endpoints never carry the deprecation marker.
    v1 = client.get("/api/v1/transactions")
    assert v1.headers.get("Deprecation") is None
