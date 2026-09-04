"""Regression test for `api/routes/snaptrade.sync()`.

The holdings leg of a SnapTrade sync must mirror `jobs/snapshot.py`: clear
today's `holdings_snapshots` rows for an account before writing the fresh
set. Without the delete, a position the user fully exited between two
same-day syncs lingers as a phantom snapshot row (the upsert only ever
touches securities present in the *new* response).

Everything external is monkeypatched to Plaid-shaped fakes:
  * `_load_credentials` (no real SnapTrade user / secret needed)
  * the four `snaptrade_client.*` network calls
  * `daily_values.run` — it opens its own `SessionLocal()` against the
    real engine, which has no schema in tests, so it must be neutralized.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from portfolio_tracker import snaptrade_client
from portfolio_tracker.models import (
    Account,
    AccountValuationObservation,
    CashFlowSourceGap,
    CashFlowSourceGapReason,
    HoldingSnapshot,
    Item,
    Security,
)
from portfolio_tracker.plaid_client import PlaidAccount, PlaidHolding, PlaidSecurity
from portfolio_tracker.provider_delivery import build_provider_delivery_metadata
from portfolio_tracker.snaptrade_client import (
    SnapTradeBrokerageAuthorization,
    SnapTradeHoldingsResponse,
    SnapTradeTransactionsResponse,
    SnapTradeUserCredentials,
)

_AUTH_ID = "auth-1"
_ST_ACCOUNT_ID = "st-acct-1"


def _fake_creds(*_args: object) -> SnapTradeUserCredentials:
    return SnapTradeUserCredentials(user_id="u-test", user_secret="secret-test")


def _fake_auths(_creds: object) -> list[SnapTradeBrokerageAuthorization]:
    return [SnapTradeBrokerageAuthorization(authorization_id=_AUTH_ID, brokerage_name="Fidelity")]


def _fake_accounts(_creds: object, _auth_id: object) -> list[tuple[str, PlaidAccount]]:
    return [
        (
            _ST_ACCOUNT_ID,
            PlaidAccount(
                plaid_account_id=_ST_ACCOUNT_ID,
                name="Fidelity Brokerage",
                type="investment",
                provider_total_value=Decimal("1234.56"),
                provider_holdings_last_successful_sync=datetime(
                    date.today().year,
                    date.today().month,
                    date.today().day,
                    12,
                    tzinfo=UTC,
                ),
            ),
        )
    ]


def _fake_holdings(_creds: object, _account_id: object) -> SnapTradeHoldingsResponse:
    """Current holdings: AAPL only. NU (pre-seeded for today) is absent."""
    return SnapTradeHoldingsResponse(
        accounts=[],
        securities=[PlaidSecurity(plaid_security_id="snaptrade:AAPL", ticker="AAPL", type="cs")],
        holdings=[
            PlaidHolding(
                plaid_account_id=_ST_ACCOUNT_ID,
                plaid_security_id="snaptrade:AAPL",
                quantity=Decimal(5),
                institution_price=Decimal(200),
                institution_value=Decimal(1000),
                cost_basis=Decimal(900),
            )
        ],
    )


def _fake_activities(
    _creds: object, _account_id: object, _start: object, _end: object
) -> SnapTradeTransactionsResponse:
    assert isinstance(_start, date)
    assert isinstance(_end, date)
    return SnapTradeTransactionsResponse(
        accounts=[],
        securities=[],
        transactions=[],
        total_transactions=0,
        delivery=build_provider_delivery_metadata(
            provider="snaptrade",
            source_format="snaptrade_account_activities_api",
            parser_version="snaptrade_account_activity.v1",
            requested_start_date=_start,
            requested_end_date=_end,
            page_count=1,
            provider_reported_total=0,
            record_ids=[],
            normalized_records=[],
        ),
    )


def _fake_holdings_with_direct_cash(
    _creds: object, _account_id: object
) -> SnapTradeHoldingsResponse:
    response = _fake_holdings(_creds, _account_id)
    return response.model_copy(
        update={
            "accounts": [
                PlaidAccount(
                    plaid_account_id=_ST_ACCOUNT_ID,
                    name="Fidelity Brokerage",
                    type="investment",
                    provider_total_value=None,
                    provider_available_cash=Decimal("78.90"),
                )
            ]
        }
    )


def _patch_snaptrade(monkeypatch) -> object:
    import portfolio_tracker.api.routes.snaptrade as route_mod
    import portfolio_tracker.jobs.daily_values as daily_values

    monkeypatch.setattr(route_mod, "_load_credentials", _fake_creds)
    monkeypatch.setattr(snaptrade_client, "list_brokerage_authorizations", _fake_auths)
    monkeypatch.setattr(snaptrade_client, "list_user_accounts", _fake_accounts)
    monkeypatch.setattr(snaptrade_client, "get_holdings", _fake_holdings)
    monkeypatch.setattr(snaptrade_client, "get_account_activities", _fake_activities)
    # daily_values.run() uses its own SessionLocal() (a schemaless test DB);
    # neutralize it — this test is only about the holdings snapshot leg.
    monkeypatch.setattr(daily_values, "run", lambda *a, **k: 0)
    return route_mod


def _seed_account_with_stale_position(session, today: date) -> tuple[Account, Security]:
    """Pre-create the Item/Account sync will reuse (matched by ids) and a
    stale today-snapshot for NU — a position the new sync no longer reports."""
    item = Item(
        source="snaptrade",
        snaptrade_user_id="u-test",
        snaptrade_authorization_id=_AUTH_ID,
        institution_name="Fidelity",
    )
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id,
        plaid_account_id=_ST_ACCOUNT_ID,
        name="Fidelity Brokerage",
        type="investment",
    )
    session.add(account)
    session.flush()
    nu = Security(plaid_security_id="snaptrade:NU", ticker="NU", type="cs")
    session.add(nu)
    session.flush()
    session.add(
        HoldingSnapshot(
            snapshot_date=today,
            account_id=account.account_id,
            security_id=nu.security_id,
            quantity=Decimal(100),
            institution_price=Decimal(50),
            institution_value=Decimal(5000),
        )
    )
    session.commit()
    return account, nu


def test_sync_deletes_fully_exited_position_on_same_day_resync(session, monkeypatch):
    today = date.today()
    account, nu = _seed_account_with_stale_position(session, today)
    route_mod = _patch_snaptrade(monkeypatch)

    result = route_mod.sync(session, route_mod.SnapTradeProfile.PRIMARY)

    # The exited NU position must be gone — not lingering from the earlier sync.
    nu_snap = session.get(HoldingSnapshot, (today, account.account_id, nu.security_id))
    assert nu_snap is None

    # The current AAPL holding was written for today.
    aapl = session.execute(select(Security).where(Security.ticker == "AAPL")).scalar_one()
    aapl_snap = session.get(HoldingSnapshot, (today, account.account_id, aapl.security_id))
    assert aapl_snap is not None
    assert aapl_snap.quantity == Decimal(5)
    assert aapl_snap.institution_value == Decimal(1000)

    # Only the current holding remains for this account today.
    remaining = (
        session.execute(
            select(HoldingSnapshot)
            .where(HoldingSnapshot.snapshot_date == today)
            .where(HoldingSnapshot.account_id == account.account_id)
        )
        .scalars()
        .all()
    )
    assert {s.security_id for s in remaining} == {aapl.security_id}
    assert result.holdings_written == 1
    assert result.accounts_synced == 1
    assert result.items_synced == 1

    valuation = session.execute(select(AccountValuationObservation)).scalar_one()
    assert valuation.account_id == account.account_id
    assert valuation.as_of_date == today
    assert valuation.total_value == Decimal("1234.56")
    assert valuation.cash_value is None
    assert valuation.source_provider == "snaptrade"
    assert valuation.source_record_id == _ST_ACCOUNT_ID
    assert valuation.is_complete is True
    assert valuation.is_empty is False
    assert len(valuation.source_payload_sha256 or "") == 64


def test_holding_from_snaptrade_cost_basis_is_total_not_per_share():
    # SnapTrade gives average_purchase_price PER SHARE; cost_basis must be
    # stored as TOTAL dollars (Plaid convention) = avg × units, else
    # unrealized P&L is overstated by (units − 1) × avg.
    raw = {
        "units": "10",
        "price": "150",
        "average_purchase_price": "100",
        "currency": {"code": "USD"},
    }
    holding = snaptrade_client._holding_from_snaptrade(raw, "acct", "sec")
    assert holding.quantity == Decimal(10)
    assert holding.institution_value == Decimal(1500)
    assert holding.cost_basis == Decimal(1000)  # 100/share × 10, NOT 100


def test_sync_combines_account_list_total_with_holdings_cash(session, monkeypatch):
    route_mod = _patch_snaptrade(monkeypatch)
    monkeypatch.setattr(snaptrade_client, "get_holdings", _fake_holdings_with_direct_cash)

    route_mod.sync(session, route_mod.SnapTradeProfile.PRIMARY)

    valuation = session.execute(select(AccountValuationObservation)).scalar_one()
    assert valuation.total_value == Decimal("1234.56")
    assert valuation.cash_value == Decimal("78.90")
    assert valuation.source_reference == (
        "account_information.list_user_accounts[].balance.total+get_user_holdings.balances[].cash;"
        "as_of=sync_status.holdings.last_successful_sync"
    )


def test_sync_total_without_provider_as_of_is_complete_on_capture_date(session, monkeypatch):
    route_mod = _patch_snaptrade(monkeypatch)
    monkeypatch.setattr(
        snaptrade_client,
        "list_user_accounts",
        lambda _creds, _auth_id: [
            (
                _ST_ACCOUNT_ID,
                PlaidAccount(
                    plaid_account_id=_ST_ACCOUNT_ID,
                    name="Fidelity Brokerage",
                    type="investment",
                    provider_total_value=Decimal("1234.56"),
                ),
            )
        ],
    )

    route_mod.sync(session, route_mod.SnapTradeProfile.PRIMARY)

    valuation = session.execute(select(AccountValuationObservation)).scalar_one()
    assert valuation.is_complete is True
    assert valuation.is_empty is False
    assert valuation.as_of_at is None
    assert "cached_as_fetched_no_provider_as_of" in valuation.source_reference


def test_sync_does_not_infer_account_total_from_holdings(session, monkeypatch):
    route_mod = _patch_snaptrade(monkeypatch)
    monkeypatch.setattr(
        snaptrade_client,
        "list_user_accounts",
        lambda _creds, _auth_id: [
            (
                _ST_ACCOUNT_ID,
                PlaidAccount(
                    plaid_account_id=_ST_ACCOUNT_ID,
                    name="Fidelity Brokerage",
                    type="investment",
                    provider_total_value=None,
                ),
            )
        ],
    )

    route_mod.sync(session, route_mod.SnapTradeProfile.PRIMARY)

    assert session.scalar(select(AccountValuationObservation)) is None


def test_holding_from_snaptrade_cost_basis_none_when_no_avg_price():
    raw = {"units": "10", "price": "150", "currency": {"code": "USD"}}
    holding = snaptrade_client._holding_from_snaptrade(raw, "acct", "sec")
    assert holding.cost_basis is None


def test_disabled_authorization_never_writes_complete_current_holdings(session, monkeypatch):
    route_mod = _patch_snaptrade(monkeypatch)
    monkeypatch.setattr(
        snaptrade_client,
        "list_brokerage_authorizations",
        lambda _creds: [
            SnapTradeBrokerageAuthorization(
                authorization_id=_AUTH_ID,
                brokerage_name="Fidelity",
                disabled=True,
                disabled_at=datetime(2026, 9, 1, tzinfo=UTC),
            )
        ],
    )

    result = route_mod.sync(session, route_mod.SnapTradeProfile.PRIMARY)

    valuation = session.execute(select(AccountValuationObservation)).scalar_one()
    assert valuation.is_complete is False
    assert valuation.is_empty is False
    assert "provider_state_unavailable" in valuation.source_reference
    assert session.scalar(select(HoldingSnapshot)) is None
    gap = session.execute(select(CashFlowSourceGap)).scalar_one()
    assert gap.reason_code == CashFlowSourceGapReason.PROVIDER_HISTORY_UNAVAILABLE
    assert result.holdings_written == 0
