import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from portfolio_tracker.jobs import register_plaid_short_calls, snapshot
from portfolio_tracker.models import Account, HoldingSnapshot, Item, Security
from portfolio_tracker.plaid_client import (
    HoldingsResponse,
    PlaidAccount,
    PlaidHolding,
    PlaidSecurity,
)
from portfolio_tracker.services.plaid_short_calls import load_short_call_overrides

CALL = "TEST270319C00055000"
OTHER_CALL = "TEST270319C00065000"


def test_plaid_snapshots_reapply_confirmed_short_signs(session, monkeypatch, tmp_path):
    path = tmp_path / "plaid_short_calls.json"
    path.write_text(
        json.dumps(
            [
                {
                    "plaid_account_id": "confirmed",
                    "occ_symbol": CALL,
                    "confirmed_on": "2026-11-10",
                    "evidence": "Owner confirmed the position was sold to open",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        snapshot, "load_short_call_overrides", lambda: load_short_call_overrides(path)
    )
    item = Item(
        source="plaid",
        plaid_item_id="synthetic",
        plaid_access_token_encrypted="synthetic",
    )
    session.add(item)
    session.flush()
    monkeypatch.setattr(snapshot, "decrypt_token", lambda _: "synthetic")

    def provider_holding(account, security, quantity=200, value=82):
        return PlaidHolding(
            plaid_account_id=account,
            plaid_security_id=security,
            quantity=Decimal(quantity),
            quantity_unit="underlying_units",
            institution_price=Decimal("0.41"),
            institution_value=Decimal(value) if value is not None else None,
            cost_basis=Decimal(120),
        )

    response = HoldingsResponse(
        accounts=[
            PlaidAccount(plaid_account_id=key, name=key, type="investment", mask=mask)
            for key, mask in (("confirmed", "0001"), ("unrelated", "0002"))
        ],
        securities=[
            PlaidSecurity(plaid_security_id="target", ticker="TEST  270319C00055000"),
            PlaidSecurity(plaid_security_id="other", ticker=OTHER_CALL),
        ],
        holdings=[
            provider_holding("confirmed", "target"),
            provider_holding("unrelated", "target"),
            provider_holding("confirmed", "other"),
        ],
        item_id="synthetic",
    )
    monkeypatch.setattr(snapshot.plaid_client, "get_holdings", lambda _: response)

    def ingest(day):
        assert snapshot._snapshot_item(session, item, day) == 3
        session.commit()
        return {
            (account, security): (
                row.quantity,
                row.institution_value,
                row.institution_price,
                row.cost_basis,
            )
            for row, account, security in session.execute(
                select(HoldingSnapshot, Account.plaid_account_id, Security.plaid_security_id)
                .join(Account, HoldingSnapshot.account_id == Account.account_id)
                .join(Security, HoldingSnapshot.security_id == Security.security_id)
                .where(HoldingSnapshot.snapshot_date == day)
            )
        }

    target = ("confirmed", "target")
    price = Decimal("0.41")
    day = date(2026, 11, 10)
    monkeypatch.setattr(register_plaid_short_calls, "OVERRIDES_PATH", path)
    configured = load_short_call_overrides(path)
    path.unlink()
    assert ingest(day)[target] == (200, 82, price, 120)
    rules = register_plaid_short_calls.verified_rules(
        session, "0001", [CALL], day, configured[0].evidence
    )
    assert register_plaid_short_calls.register(rules) == 1
    assert register_plaid_short_calls.register(rules) == 0
    with pytest.raises(ValueError, match="missing from the account"):
        register_plaid_short_calls.verified_rules(session, "0002", [OTHER_CALL], day, "Synthetic")
    assert ingest(day)[target] == (-200, -82, price, -120)
    assert ingest(day) == {
        target: (-200, -82, price, -120),
        ("unrelated", "target"): (200, 82, price, 120),
        ("confirmed", "other"): (200, 82, price, 120),
    }
    response.holdings[0] = provider_holding("confirmed", "target", -100, -41)
    assert ingest(date(2026, 11, 11))[target] == (-100, -41, price, -120)
    response.holdings[0] = provider_holding("confirmed", "target", 100, None)
    assert ingest(date(2026, 11, 12))[target] == (-100, -41, price, -120)
    response.holdings[0] = provider_holding("confirmed", "target")
    assert ingest(date(2027, 3, 20))[target] == (200, 82, price, 120)


def test_invalid_private_rule_fails_without_echoing_its_contents(tmp_path):
    path = tmp_path / "plaid_short_calls.json"
    assert load_short_call_overrides(path) == ()
    path.write_text('[{"plaid_account_id": "private-account", "occ_symbol": "not-an-option"}]')
    with pytest.raises(ValueError, match=r"^Invalid private Plaid short-call overrides$"):
        load_short_call_overrides(path)
