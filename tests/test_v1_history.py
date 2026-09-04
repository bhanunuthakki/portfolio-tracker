"""Contract tests for the Slice-2 `/api/v1` history + analytics resources:
cursor pagination (no silent caps), canonical cash-flow classification,
position-snapshot origin markers, the securities master, structured cursor
errors, and deprecation headers on superseded legacy endpoints.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from portfolio_tracker.models import (
    Account,
    CashFlowReconciliationDecision,
    CashFlowSourceAttestation,
    CashFlowSourceEvent,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Price,
    PriceAdjustmentBasis,
    PriceSource,
    Security,
    TransactionOverride,
)
from portfolio_tracker.services.cashflow_source_coverage import (
    canonical_decision_payload_sha256,
    canonical_source_event_set_sha256,
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
    session.add(
        CashFlowSourceAttestation(
            attestation_key="synthetic-v1-history",
            account_id=acct.account_id,
            coverage_start=_TODAY - timedelta(days=729),
            coverage_end=_TODAY,
            source_type="provider_export",
            source_reference="synthetic:v1-history",
            source_sha256="b" * 64,
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
            approved_at=datetime(2026, 1, 2, tzinfo=UTC),
            methodology_version="1",
            account_identity_sha256="c" * 64,
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
    assert first["meta"]["schema_version"] == "1.4.0"
    # Effective classification mirrors the TWR pipeline.
    by_id = {r["transaction_id"]: r for r in rows}
    assert by_id["t5"]["effective_classification"] == "external_in"
    assert by_id["t4"]["effective_classification"] == "external_out"
    assert by_id["t2"]["effective_classification"] == "internal"  # dividend name hint
    assert by_id["t1"]["effective_classification"] is None  # buy: not cashflow-shaped


def test_transaction_resources_project_current_provenance_classification(client, session):
    account = _seed(session)
    attestation = session.query(CashFlowSourceAttestation).one()
    event = CashFlowSourceEvent(
        source_event_id="1" * 64,
        attestation_id=attestation.attestation_id,
        source_record_id="t4",
        source_locator_kind="provider_record",
        source_locator="provider:t4",
        source_row_ordinal=None,
        source_page=None,
        source_line=None,
        source_row_sha256="2" * 64,
        activity_date=_FRESH,
        process_date=None,
        settlement_date=None,
        source_amount=Decimal(200),
        source_amount_sign_basis="provider_reported",
        currency="USD",
        source_code="withdrawal",
    )
    decision = CashFlowReconciliationDecision(
        decision_key="3" * 64,
        source_event_id=event.source_event_id,
        target_transaction_id="t4",
        resolution_kind="excluded",
        classification="excluded",
        signed_external_amount=Decimal(0),
        effective_date=_FRESH,
        effective_date_basis="provider_posting",
        effective_timezone="UTC",
        decision_authority="owner_approved",
        confidence="exact",
        assumption_code="owner_resolved_excluded_event",
        methodology_version="2",
        decision_payload_sha256="4" * 64,
        approved_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    decision.decision_payload_sha256 = canonical_decision_payload_sha256(decision)
    attestation.source_row_count = 1
    attestation.cashflow_candidate_count = 1
    attestation.source_event_set_sha256 = canonical_source_event_set_sha256((event,))
    session.add_all([event, decision])
    session.commit()

    legacy = client.get("/api/portfolio/transactions").json()
    versioned = client.get("/api/v1/transactions").json()["transactions"]
    legacy_t4 = next(row for row in legacy if row["plaid_investment_transaction_id"] == "t4")
    versioned_t4 = next(row for row in versioned if row["transaction_id"] == "t4")

    assert account.account_id == versioned_t4["account_id"]
    assert legacy_t4["override_classification"] is None
    assert versioned_t4["override_classification"] is None
    assert legacy_t4["effective_classification"] == "excluded"
    assert versioned_t4["effective_classification"] == "excluded"


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


def test_cash_flow_window_total_is_not_page_local(client, session):
    _seed(session)

    first = client.get("/api/v1/cash-flows", params={"limit": 1}).json()
    second = client.get(
        "/api/v1/cash-flows",
        params={"limit": 1, "cursor": first["next_cursor"]},
    ).json()

    # The total describes the requested window, not whichever page happens to
    # be visible. This is the number performance subtracts from portfolio gain.
    assert Decimal(first["net_external_cashflow_in"]) == Decimal("1300")
    assert Decimal(second["net_external_cashflow_in"]) == Decimal("1300")


def test_synthesized_share_transfer_is_visible_in_cash_flow_ledger(client, session):
    acct = _seed(session)
    security = session.query(Security).filter_by(ticker="AAPL").one()
    transfer_date = _FRESH - timedelta(days=4)
    session.add(
        InvestmentTransaction(
            plaid_investment_transaction_id="acat-in-aapl",
            account_id=acct.account_id,
            security_id=security.security_id,
            date=transfer_date,
            name="External asset transfer in",
            quantity=Decimal("2"),
            amount=Decimal(0),
            type="cash",
            subtype="external_asset_transfer_in",
            currency="USD",
        )
    )
    session.add(
        Price(
            security_id=security.security_id,
            date=transfer_date,
            close=Decimal("125"),
            source=PriceSource.YFINANCE.value,
            adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
        )
    )
    session.commit()

    payload = client.get(
        "/api/v1/cash-flows",
        params={
            "start_date": (transfer_date - timedelta(days=1)).isoformat(),
            "end_date": transfer_date.isoformat(),
        },
    ).json()
    synthetic = next(
        row for row in payload["cash_flows"] if row["source_kind"] == "share_transfer_valuation"
    )

    assert synthetic["transaction_id"] is None
    assert synthetic["component_transaction_ids"] == ["acat-in-aapl"]
    assert synthetic["classification"] == "external_in"
    assert synthetic["classification_source"] == "derived_share_transfer_net"
    assert synthetic["valuation_price"] == "125.000000"
    assert synthetic["valuation_price_date"] == transfer_date.isoformat()
    assert synthetic["valuation_price_source"] == "historical_close"
    assert Decimal(synthetic["signed_external_amount"]) == Decimal("250")
    assert Decimal(payload["net_external_cashflow_in"]) == Decimal("250")

    # Both consumers must read the same canonical derived ledger.
    from portfolio_tracker.services.performance import _daily_external_cashflows

    assert _daily_external_cashflows(session, transfer_date - timedelta(days=1), transfer_date) == {
        transfer_date: Decimal("250")
    }


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
    assert data["meta"]["schema_version"] == "1.4.0"
    assert "findings" in data["report"]
    assert "summary_counts" in data["report"]


def test_analytics_wrappers_enveloped(client, session):
    _seed(session)
    perf = client.get("/api/v1/analytics/performance").json()
    assert perf["meta"]["methodology"] == "performance.modified_dietz"
    assert perf["meta"]["methodology_version"] == "2"
    assert perf["series"]["methodology"] == perf["meta"]["methodology"]
    assert perf["series"]["methodology_version"] == perf["meta"]["methodology_version"]
    assert perf["meta"]["as_of"] == _FRESH.isoformat()  # holdings date, not query end
    assert "points" in perf["series"]

    alpha = client.get("/api/v1/analytics/position-performance").json()
    assert (
        alpha["meta"]["methodology"] == "position_alpha.split_normalized_price_trade_modified_dietz"
    )
    assert alpha["meta"]["methodology_version"] == "3"
    assert alpha["result"]["methodology"] == alpha["meta"]["methodology"]
    assert alpha["result"]["methodology_version"] == alpha["meta"]["methodology_version"]

    risk = client.get("/api/v1/analytics/risk").json()
    assert risk["meta"]["methodology"] == "risk.beta_drawdown"
    assert risk["meta"]["methodology_version"] == "2"
    assert "beta" in risk and "drawdown" in risk
    for raw_result in (risk["beta"], risk["drawdown"]):
        assert raw_result["methodology"] == risk["meta"]["methodology"]
        assert raw_result["methodology_version"] == risk["meta"]["methodology_version"]

    exits = client.get("/api/v1/analytics/exit-quality").json()
    assert exits["meta"]["methodology"] == "exit_quality.repricing"


def test_risk_split_resources_match_the_combined_read(client, session):
    """`/analytics/beta` and `/analytics/drawdown` are the two halves of
    `/analytics/risk` — split so a consumer needing only one doesn't pay for
    the other. All three must agree over the same window."""
    _seed(session)
    combined = client.get("/api/v1/analytics/risk").json()
    beta_only = client.get("/api/v1/analytics/beta").json()
    drawdown_only = client.get("/api/v1/analytics/drawdown").json()

    assert beta_only["beta"] == combined["beta"]
    assert drawdown_only["drawdown"] == combined["drawdown"]
    # The split resources carry the same envelope contract.
    for payload in (beta_only, drawdown_only):
        assert payload["meta"]["schema_version"] == "1.4.0"
        assert payload["meta"]["methodology"] == "risk.beta_drawdown"
        assert payload["meta"]["methodology_version"] == "2"
    # Each half returns only its own half — that is the point of the split.
    assert "drawdown" not in beta_only
    assert "beta" not in drawdown_only


def test_legacy_analytics_results_embed_methodology_markers(client, session):
    _seed(session)
    params = {
        "start_date": (_FRESH - timedelta(days=365)).isoformat(),
        "end_date": _FRESH.isoformat(),
    }
    expected = {
        "/api/portfolio/performance": ("performance.modified_dietz", "2"),
        "/api/portfolio/position-alpha": (
            "position_alpha.split_normalized_price_trade_modified_dietz",
            "3",
        ),
        "/api/portfolio/beta": ("risk.beta_drawdown", "2"),
        "/api/portfolio/drawdown": ("risk.beta_drawdown", "2"),
    }

    for path, (methodology, version) in expected.items():
        response = client.get(path, params=params)
        assert response.status_code == 200
        payload = response.json()
        assert payload["methodology"] == methodology
        assert payload["methodology_version"] == version


def test_deprecation_headers_on_legacy_endpoints(client, session):
    _seed(session)
    resp = client.get("/api/portfolio/transactions")
    assert resp.headers.get("Deprecation") == "true"
    assert "/api/v1/transactions" in resp.headers.get("Link", "")
    # v1 endpoints never carry the deprecation marker.
    v1 = client.get("/api/v1/transactions")
    assert v1.headers.get("Deprecation") is None
