"""Option quantity, liability, descriptor and account-attribution regressions."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from portfolio_tracker import plaid_client, snaptrade_client
from portfolio_tracker.api.routes.portfolio import _consolidate_holdings
from portfolio_tracker.jobs._helpers import upsert_security
from portfolio_tracker.models import Account, HoldingSnapshot, Item, Security
from portfolio_tracker.provider_delivery import ProviderPayloadError
from portfolio_tracker.schemas import ConsolidatedHoldingOut, HoldingByAccountOut
from portfolio_tracker.services.external_flow_ledger import classify_transaction_cashflow
from portfolio_tracker.services.option_contracts import option_from_occ, stored_option
from portfolio_tracker.services.performance import modified_dietz_series
from portfolio_tracker.services.positions_v1 import build_positions_result


def _symbol(mini=False):
    return {
        "option_symbol": {
            "id": "option-id",
            "ticker": "XYZ   261218C00120000",
            "option_type": "CALL",
            "strike_price": 120,
            "expiration_date": "2026-12-18",
            "is_mini_option": mini,
            "underlying_symbol": {"symbol": "XYZ", "currency": {"code": "USD"}},
        }
    }


@pytest.mark.parametrize(("mini", "units", "value"), [(False, -200, -600), (True, -20, -60)])
def test_snaptrade_consumes_option_positions_and_preserves_signed_value(
    monkeypatch, mini, units, value
):
    raw = {
        "symbol": _symbol(mini),
        "units": -2,
        "price": 3,
        "average_purchase_price": 300 if not mini else 30,
    }
    provider = SimpleNamespace(
        account_information=SimpleNamespace(
            get_user_holdings=lambda **kwargs: {"positions": [], "option_positions": [raw]}
        )
    )
    monkeypatch.setattr(snaptrade_client, "get_client", lambda: provider)
    result = snaptrade_client.get_holdings(
        snaptrade_client.SnapTradeUserCredentials(user_id="synthetic", user_secret=str(uuid4())),
        "account",
    )
    holding = result.holdings[0]
    assert holding.quantity == Decimal(units)
    assert holding.quantity_unit == "underlying_units"
    assert holding.institution_value == Decimal(value)
    assert holding.cost_basis == Decimal(value)
    assert result.securities[0].option_contract.multiplier == Decimal(10 if mini else 100)


def test_missing_snaptrade_multiplier_does_not_guess_contract_deliverables():
    symbol = _symbol()
    del symbol["option_symbol"]["is_mini_option"]
    security = snaptrade_client._option_security_from_snaptrade(symbol)
    assert security.option_contract.multiplier is None
    with pytest.raises(ProviderPayloadError, match="multiplier"):
        snaptrade_client._option_holding_from_snaptrade(
            {"units": -1, "price": 3}, "account", security
        )


def test_plaid_option_quantity_is_not_multiplied_again(monkeypatch):
    raw_security = {
        "security_id": "option",
        "ticker_symbol": "XYZ261218C00120000",
        "type": "derivative",
        "option_contract": {
            "underlying_security_ticker": "XYZ",
            "contract_type": "call",
            "expiration_date": "2026-12-18",
            "strike_price": 120,
        },
    }
    response = SimpleNamespace(
        item=SimpleNamespace(to_dict=lambda: {"item_id": "synthetic"}),
        accounts=[],
        securities=[SimpleNamespace(to_dict=lambda: raw_security)],
        holdings=[
            SimpleNamespace(
                to_dict=lambda: {
                    "account_id": "account",
                    "security_id": "option",
                    "quantity": -200,
                    "institution_price": 3,
                    "institution_value": -600,
                    "cost_basis": -600,
                }
            )
        ],
    )
    monkeypatch.setattr(
        plaid_client,
        "get_client",
        lambda: SimpleNamespace(investments_holdings_get=lambda request: response),
    )
    result = plaid_client.get_holdings("synthetic")
    assert result.holdings[0].quantity == Decimal(-200)
    assert result.holdings[0].institution_value == Decimal(-600)
    assert result.holdings[0].quantity_unit == "underlying_units"
    assert result.securities[0].option_contract.multiplier is None


def test_legacy_occ_metadata_does_not_invent_multiplier():
    descriptor = option_from_occ("XYZ   261218C00120000")
    assert descriptor.underlying_ticker == "XYZ"
    assert descriptor.strike_price == Decimal(120)
    assert descriptor.multiplier is None
    assert option_from_occ("XYZ1261218C00120000") is None
    assert option_from_occ("XYZ261332C00120000") is None
    assert option_from_occ("XYZ") is None


def test_same_option_in_two_accounts_preserves_lot_units_and_contracts():
    descriptor = snaptrade_client._option_security_from_snaptrade(_symbol()).option_contract
    lots = [
        HoldingByAccountOut(
            account_id=i,
            account_name="Synthetic",
            quantity=Decimal(-100),
            quantity_unit="underlying_units",
            institution_value=Decimal(-300),
            cost_basis=Decimal(-300),
        )
        for i in (1, 2)
    ]
    option = ConsolidatedHoldingOut(
        snapshot_date=date(2026, 10, 2),
        security_id=1,
        ticker="XYZ261218C00120000",
        name="Synthetic call",
        total_quantity=Decimal(-200),
        option_contract=descriptor,
        total_value=Decimal(-600),
        total_cost_basis=Decimal(-600),
        weighted_avg_cost_per_share=None,
        unrealized_pnl=Decimal(0),
        accounts=lots,
        currency="USD",
    )
    cash = option.model_copy(
        update={
            "security_id": 2,
            "ticker": "CASH",
            "option_contract": None,
            "total_quantity": Decimal(10600),
            "total_value": Decimal(10600),
            "accounts": [],
        }
    )
    result = build_positions_result(option.snapshot_date, [option, cash], {1: "taxable", 2: "roth"})
    assert result.total_market_value == Decimal(10000)
    assert result.positions[0].contract_quantity == Decimal(-2)
    assert result.positions[0].percent_of_portfolio == Decimal(-6)
    assert [lot.contract_quantity for lot in result.positions[0].accounts] == [
        Decimal(-1),
        Decimal(-1),
    ]
    assert result.positions[0].option_contract == descriptor


def test_descriptor_persistence_preserves_legacy_unknown_quantity_units(session):
    security = upsert_security(session, snaptrade_client._option_security_from_snaptrade(_symbol()))
    assert stored_option(security.option_contract_json, security.ticker).multiplier == Decimal(100)
    item = Item(
        plaid_item_id="synthetic",
        institution_name="Synthetic",
        plaid_access_token_encrypted=str(uuid4()),
    )
    session.add(item)
    session.flush()
    account = Account(
        item_id=item.item_id, plaid_account_id="account", name="Synthetic", type="investment"
    )
    session.add(account)
    session.flush()
    holding = HoldingSnapshot(
        snapshot_date=date(2026, 10, 2),
        account_id=account.account_id,
        security_id=security.security_id,
        quantity=Decimal(-100),
        institution_value=Decimal(-300),
    )
    session.add(holding)
    session.flush()
    result = _consolidate_holdings(holding.snapshot_date, [(holding, account, security)], {})
    positions = build_positions_result(holding.snapshot_date, result, {})
    assert positions.positions[0].accounts[0].quantity_unit == "unknown"
    assert positions.positions[0].contract_quantity is None
    assert session.execute(select(Security)).scalar_one().option_contract_json is not None


def test_opening_option_premium_and_liability_are_neutral_for_net_return():
    # $300 received is offset by the written option liability. Premium is not
    # an owner contribution or an immediate $300 profit.
    opening = Decimal(10000)
    cash_after_sale = opening + Decimal(300)
    liability = Decimal(-300)
    day = date(2026, 10, 2)
    end = date(2026, 10, 3)
    assert modified_dietz_series(
        [day, end], {day: opening, end: cash_after_sale + liability}, {}, opening
    )[end] == Decimal(0)


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_option_trades_are_not_external_owner_cashflows(side):
    assert (
        classify_transaction_cashflow(side, None, Decimal(300), name="Synthetic option trade")
        is None
    )


def test_option_schema_migration_retains_old_rows_without_guessing_units(tmp_path, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from portfolio_tracker.config import get_settings

    path = tmp_path / "synthetic-options.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    get_settings.cache_clear()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(config, "0031")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO items (item_id, source, plaid_item_id) VALUES (1, 'plaid', 'synthetic')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO accounts (account_id,item_id,plaid_account_id,name,type,currency) VALUES (1,1,'synthetic','Synthetic','investment','USD')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO securities (security_id,plaid_security_id,ticker,type,currency,is_cash_equivalent) VALUES (1,'synthetic','XYZ261218C00120000','derivative','USD',0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO holdings_snapshots (snapshot_date,account_id,security_id,quantity,institution_value,currency) VALUES ('2026-10-02',1,1,-100,-300,'USD')"
            )
        )
    engine.dispose()
    command.upgrade(config, "0032")
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        security_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(securities)"))}
        holding_columns = {
            row[1]: row for row in conn.execute(text("PRAGMA table_info(holdings_snapshots)"))
        }
        assert "option_contract_json" in security_columns
        assert holding_columns["quantity_unit"][4] == "'unknown'"
        retained = conn.execute(
            text("SELECT quantity,institution_value,quantity_unit FROM holdings_snapshots")
        ).one()
        assert tuple(retained) == (-100, -300, "unknown")
    engine.dispose()
    command.downgrade(config, "0031")
    get_settings.cache_clear()


@pytest.mark.parametrize("raw_type", ["OPTIONEXPIRATION", "OPTIONASSIGNMENT"])
def test_option_lifecycle_transactions_do_not_become_external_cash(raw_type):
    tx = snaptrade_client._transaction_from_snaptrade(
        {
            "id": "synthetic-event",
            "trade_date": "2026-10-02",
            "type": raw_type,
            "amount": 0,
            "units": 1,
        },
        "synthetic-account",
        "synthetic-option",
    )
    assert classify_transaction_cashflow(tx.type, tx.subtype, tx.amount) is None


@pytest.mark.parametrize("ticker", ["XYZ1261218C00120000", "ABC261218C00120000"])
def test_snaptrade_standard_flag_cannot_prove_adjusted_or_conflicting_deliverables(ticker):
    symbol = _symbol()
    symbol["option_symbol"]["ticker"] = ticker
    security = snaptrade_client._option_security_from_snaptrade(symbol)
    assert security.option_contract.multiplier is None
    with pytest.raises(ProviderPayloadError, match="multiplier"):
        snaptrade_client._option_holding_from_snaptrade(
            {"units": -1, "price": 3}, "account", security
        )


def test_snaptrade_mini_occ_suffix_seven_uses_provider_ten_share_multiplier():
    symbol = _symbol(mini=True)
    symbol["option_symbol"]["ticker"] = "AAPL7 261218C00120000"
    symbol["option_symbol"]["underlying_symbol"]["symbol"] = "AAPL"
    security = snaptrade_client._option_security_from_snaptrade(symbol)
    assert security.option_contract.multiplier == Decimal(10)
    holding = snaptrade_client._option_holding_from_snaptrade(
        {"units": -2, "price": 3, "average_purchase_price": 30}, "account", security
    )
    assert holding.quantity == Decimal(-20)
    assert holding.institution_value == Decimal(-60)
    assert holding.cost_basis == Decimal(-60)


@pytest.mark.parametrize(
    ("mini", "underlying", "ticker"),
    [
        (False, "AAPL", "AAPL7 261218C00120000"),
        (True, "AAPL", "AAPL1 261218C00120000"),
        (True, "MSFT", "AAPL7 261218C00120000"),
    ],
)
def test_mini_suffix_does_not_override_missing_or_contradictory_provider_evidence(
    mini, underlying, ticker
):
    symbol = _symbol(mini=mini)
    symbol["option_symbol"]["ticker"] = ticker
    symbol["option_symbol"]["underlying_symbol"]["symbol"] = underlying
    security = snaptrade_client._option_security_from_snaptrade(symbol)
    assert security.option_contract.multiplier is None
