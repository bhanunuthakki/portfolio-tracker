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

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from portfolio_tracker import snaptrade_client
from portfolio_tracker.models import Account, HoldingSnapshot, Item, Security
from portfolio_tracker.plaid_client import PlaidAccount, PlaidHolding, PlaidSecurity
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
    return SnapTradeTransactionsResponse(accounts=[], securities=[], transactions=[])


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


def test_holding_from_snaptrade_cost_basis_none_when_no_avg_price():
    raw = {"units": "10", "price": "150", "currency": {"code": "USD"}}
    holding = snaptrade_client._holding_from_snaptrade(raw, "acct", "sec")
    assert holding.cost_basis is None
