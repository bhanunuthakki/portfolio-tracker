"""Contract tests for the Slice-1 `/api/v1` resources.

Covers the shared envelope (staleness, partial coverage, warnings), the
canonical-account + detailed-Tax-treatment contract on `/api/v1/accounts`,
the bulk `/api/v1/portfolio-snapshot` (five-way buckets + equity fraction),
`/api/v1/analytics/positioning`, and `/api/v1/health`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from portfolio_tracker.models import Account, HoldingSnapshot, Item, Security

# Relative to the real clock: the envelope's staleness check runs against
# today, so pinned calendar dates would rot as time passes.
_TODAY = date.today()
_FRESH = _TODAY - timedelta(days=1)
_LAGGING = _TODAY - timedelta(days=10)


def _mk_item(session, *, source: str, name: str, active: bool = True) -> Item:
    item = Item(
        source=source,
        plaid_item_id=f"pi-{name}" if source == "plaid" else None,
        snaptrade_authorization_id=f"sa-{name}" if source == "snaptrade" else None,
        institution_name=name,
        is_data_active=active,
        last_refreshed_at=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )
    session.add(item)
    session.flush()
    return item


def _mk_account(session, item: Item, *, pid: str, name: str, type_: str, subtype: str | None):
    acct = Account(
        item_id=item.item_id,
        plaid_account_id=pid,
        name=name,
        type=type_,
        subtype=subtype,
    )
    session.add(acct)
    session.flush()
    return acct


def _mk_security(session, *, pid: str, ticker: str, type_: str = "cs", cash: bool = False):
    sec = Security(plaid_security_id=pid, ticker=ticker, type=type_, is_cash_equivalent=cash)
    session.add(sec)
    session.flush()
    return sec


def _snap(session, acct, sec, *, d: date, qty: str, value: str):
    session.add(
        HoldingSnapshot(
            snapshot_date=d,
            account_id=acct.account_id,
            security_id=sec.security_id,
            quantity=Decimal(qty),
            institution_value=Decimal(value),
            cost_basis=Decimal(value),
        )
    )


def _seed_two_provider_book(session):
    """A fresh Plaid Roth + a fresh SnapTrade brokerage + a retired Plaid item."""
    plaid_item = _mk_item(session, source="plaid", name="Fidelity")
    snap_item = _mk_item(session, source="snaptrade", name="Robinhood")
    retired = _mk_item(session, source="plaid", name="Robinhood (Plaid)", active=False)

    roth = _mk_account(
        session, plaid_item, pid="a1", name="Roth IRA", type_="investment", subtype="roth ira"
    )
    hsa = _mk_account(
        session, plaid_item, pid="a2", name="Health Savings", type_="investment", subtype="hsa"
    )
    brokerage = _mk_account(
        session, snap_item, pid="a3", name="RH Brokerage", type_="brokerage", subtype="brokerage"
    )
    old = _mk_account(
        session, retired, pid="a4", name="RH via Plaid", type_="brokerage", subtype="brokerage"
    )

    aapl = _mk_security(session, pid="s1", ticker="AAPL")
    mm = _mk_security(session, pid="s2", ticker="FDRXX", type_="oef", cash=True)

    _snap(session, roth, aapl, d=_FRESH, qty="10", value="6000")
    _snap(session, hsa, aapl, d=_FRESH, qty="2", value="1000")
    _snap(session, brokerage, aapl, d=_FRESH, qty="5", value="2000")
    _snap(session, brokerage, mm, d=_FRESH, qty="1000", value="1000")
    # The retired account still has an old snapshot — must not enter totals.
    _snap(session, old, aapl, d=_LAGGING, qty="5", value="1900")
    session.commit()
    return {"roth": roth, "hsa": hsa, "brokerage": brokerage, "old": old}


def test_accounts_contract(client, session):
    accts = _seed_two_provider_book(session)
    resp = client.get("/api/v1/accounts")
    assert resp.status_code == 200
    data = resp.json()

    meta = data["meta"]
    assert meta["schema_version"] == "1.0.0"
    assert meta["as_of"] == _FRESH.isoformat()
    assert sorted(meta["source_providers"]) == ["plaid", "snaptrade"]
    assert meta["is_partial"] is False
    assert meta["currency"] == "USD"

    by_id = {a["account_id"]: a for a in data["accounts"]}
    roth = by_id[accts["roth"].account_id]
    assert roth["tax_treatment"] == "roth"
    assert roth["tax_treatment_evidence"] == "subtype:roth ira"
    assert roth["tax_treatment_confidence"] == "high"
    assert roth["included_in_totals"] is True
    assert roth["canonical_account_id"] == roth["account_id"]
    assert roth["exclusion_reason"] is None
    assert float(roth["value"]) == 6000.0
    assert roth["holdings_as_of"] == _FRESH.isoformat()

    hsa = by_id[accts["hsa"].account_id]
    assert hsa["tax_treatment"] == "hsa"

    old = by_id[accts["old"].account_id]
    assert old["included_in_totals"] is False
    assert old["exclusion_reason"] == "operator_excluded"
    assert old["canonical_account_id"] is None
    assert any(w["code"] == "NO_CANONICAL_LINK" for w in old["warnings"])
    # Its stale value is still reported, dated — never merged into totals.
    assert float(old["value"]) == 1900.0

    coverage = meta["account_coverage"]
    assert accts["old"].account_id in coverage["excluded_account_ids"]
    assert accts["roth"].account_id in coverage["included_account_ids"]


def test_portfolio_snapshot_buckets_and_equity_fraction(client, session):
    _seed_two_provider_book(session)
    resp = client.get("/api/v1/portfolio-snapshot")
    assert resp.status_code == 200
    data = resp.json()

    # Included book = 6000 roth + 1000 hsa + 2000 taxable + 1000 cash-equivalent.
    assert float(data["total_market_value"]) == 10000.0
    buckets = {k: float(v) for k, v in data["by_tax_treatment"].items()}
    assert buckets == {
        "taxable": 3000.0,
        "pretax": 0.0,
        "roth": 6000.0,
        "hsa": 1000.0,
        "unknown": 0.0,
    }

    ef = data["equity_fraction"]
    assert ef["unit"] == "fraction"
    assert float(ef["equity_value"]) == 9000.0
    assert float(ef["denominator_value"]) == 10000.0
    assert float(ef["equity_fraction"]) == 0.9
    assert ef["methodology"] == "equity_fraction.cash_equivalent"
    assert ef["methodology_version"] == "1"
    assert ef["holdings_as_of"] == _FRESH.isoformat()

    # The retired account's 1900 must not appear anywhere in totals.
    assert all(float(v) <= 10000.0 for v in [data["total_market_value"]])
    assert data["meta"]["links"]["accounts"] == "/api/v1/accounts"


def test_partial_coverage_when_one_account_lags(client, session):
    """A lagging included account (its provider stopped refreshing) must mark
    the response partial — never silently blend as complete."""
    item = _mk_item(session, source="plaid", name="Fidelity")
    fresh_acct = _mk_account(
        session, item, pid="a1", name="Brokerage", type_="brokerage", subtype="brokerage"
    )
    lag_acct = _mk_account(
        session, item, pid="a2", name="Roth IRA", type_="investment", subtype="roth ira"
    )
    aapl = _mk_security(session, pid="s1", ticker="AAPL")
    _snap(session, fresh_acct, aapl, d=_FRESH, qty="1", value="1000")
    _snap(session, lag_acct, aapl, d=_LAGGING, qty="1", value="1000")
    session.commit()

    data = client.get("/api/v1/accounts").json()
    assert data["meta"]["is_partial"] is True
    assert any(w["code"] == "PARTIAL_COVERAGE" for w in data["meta"]["warnings"])
    assert lag_acct.account_id in data["meta"]["account_coverage"]["lagging_account_ids"]

    snap_data = client.get("/api/v1/portfolio-snapshot").json()
    assert snap_data["meta"]["is_partial"] is True


def test_empty_book_reports_no_data_not_zero(client):
    data = client.get("/api/v1/portfolio-snapshot").json()
    assert data["meta"]["as_of"] is None
    assert data["meta"]["is_stale"] is False
    assert any(w["code"] == "NO_DATA" for w in data["meta"]["warnings"])
    assert data["equity_fraction"]["equity_fraction"] is None
    assert any(w["code"] == "CALCULATION_UNAVAILABLE" for w in data["equity_fraction"]["warnings"])


def test_unknown_tax_treatment_is_flagged_not_guessed(client, session):
    item = _mk_item(session, source="plaid", name="SoFi")
    mystery = _mk_account(
        session, item, pid="a1", name="Mystery Account", type_="investment", subtype=None
    )
    aapl = _mk_security(session, pid="s1", ticker="AAPL")
    _snap(session, mystery, aapl, d=_FRESH, qty="1", value="1000")
    session.commit()

    data = client.get("/api/v1/accounts").json()
    acct = data["accounts"][0]
    assert acct["tax_treatment"] == "unknown"
    assert any(w["code"] == "UNKNOWN_TAX_TREATMENT" for w in acct["warnings"])
    assert any(w["code"] == "UNKNOWN_TAX_TREATMENT" for w in data["meta"]["warnings"])


def test_analytics_positioning_v1(client, session):
    _seed_two_provider_book(session)
    resp = client.get("/api/v1/analytics/positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert data["meta"]["schema_version"] == "1.0.0"
    assert data["meta"]["methodology"] == "positioning.value_weighted_cuts"
    assert float(data["positioning"]["total_value"]) == 10000.0
    assert float(data["equity_fraction"]["equity_fraction"]) == 0.9


def test_health(client, session):
    _seed_two_provider_book(session)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["schema_version"] == "1.0.0"
    assert data["database_ok"] is True
    # Schema was created by Base.metadata.create_all — no alembic_version table;
    # health reports that honestly instead of guessing.
    assert data["migration_version"] is None
    providers = {p["name"]: p for p in data["providers"]}
    assert providers["plaid"]["items_linked"] == 2
    assert providers["plaid"]["items_active"] == 1
    assert providers["snaptrade"]["items_linked"] == 1
    assert data["latest_snapshot_date"] == _FRESH.isoformat()
    # No balances anywhere in the health payload.
    text = resp.text
    assert "6000" not in text and "1900" not in text


def test_health_empty_db(client):
    data = client.get("/api/v1/health").json()
    assert data["database_ok"] is True
    assert data["active_account_count"] == 0
    assert data["latest_snapshot_date"] is None
