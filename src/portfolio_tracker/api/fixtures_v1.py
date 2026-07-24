"""Generate the sanitized `/api/v1` consumer fixtures (Phase 0 ruling SC-6).

The JSON files under `docs/api/fixtures/v1/` are the single fixture suite both
earnings-summary and wealthplan contract tests load. Every value is synthetic —
built from a scripted in-memory database, never from live data (PRD §8) — and
every timestamp is pinned so regeneration is byte-stable.

Scenarios:

  * ``portfolio-snapshot.json``          — current, complete two-provider book
  * ``portfolio-snapshot.partial.json``  — one included account lagging
  * ``portfolio-snapshot.stale.json``    — whole book older than the staleness budget
  * ``accounts.json``                    — canonical accounts incl. an excluded item
  * ``positioning.json``                 — Positioning cuts + equity fraction
  * ``health.json``                      — service health shape
  * ``transactions.json``                — cursor-paginated normalized transactions
  * ``cash-flows.json``                  — TWR-classified external flows
  * ``securities.json``                  — security master + Classification

Regenerate after changing any v1 model:

    python -m portfolio_tracker.api.fixtures_v1
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from portfolio_tracker.models import (
    Account,
    Base,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Security,
)

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "docs" / "api" / "fixtures" / "v1"

# Pinned clock — fixtures must regenerate byte-identical.
FIXTURE_TODAY = date(2026, 7, 23)
FIXTURE_GENERATED_AT = datetime(2026, 7, 23, 6, 0, 0, tzinfo=UTC)
_FRESH = FIXTURE_TODAY - timedelta(days=1)
_LAGGING = FIXTURE_TODAY - timedelta(days=9)
_ANCIENT = FIXTURE_TODAY - timedelta(days=30)
_SYNC_AT = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)


def _seed(
    session: Session,
    *,
    holdings_date: date,
    lag_one_account: bool = False,
) -> None:
    """A synthetic two-provider book: Plaid Roth + HSA, SnapTrade brokerage
    with a money-market position, and a retired Plaid item (canonical-account
    exclusion example)."""
    plaid_item = Item(
        source="plaid",
        plaid_item_id="fixture-plaid-1",
        institution_name="Example Broker A",
        is_data_active=True,
        last_refreshed_at=_SYNC_AT,
    )
    snap_item = Item(
        source="snaptrade",
        snaptrade_authorization_id="fixture-snaptrade-1",
        institution_name="Example Broker B",
        is_data_active=True,
        last_refreshed_at=_SYNC_AT,
    )
    retired_item = Item(
        source="plaid",
        plaid_item_id="fixture-plaid-retired",
        institution_name="Example Broker B (retired link)",
        is_data_active=False,
        last_refreshed_at=_SYNC_AT,
    )
    session.add_all([plaid_item, snap_item, retired_item])
    session.flush()

    roth = Account(
        item_id=plaid_item.item_id,
        plaid_account_id="fx-roth",
        name="Example Roth IRA",
        type="investment",
        subtype="roth ira",
    )
    hsa = Account(
        item_id=plaid_item.item_id,
        plaid_account_id="fx-hsa",
        name="Example HSA",
        type="investment",
        subtype="hsa",
    )
    brokerage = Account(
        item_id=snap_item.item_id,
        plaid_account_id="fx-brokerage",
        name="Example Brokerage",
        type="brokerage",
        subtype="brokerage",
    )
    retired = Account(
        item_id=retired_item.item_id,
        plaid_account_id="fx-retired",
        name="Example Brokerage (retired link)",
        type="brokerage",
        subtype="brokerage",
    )
    session.add_all([roth, hsa, brokerage, retired])
    session.flush()

    stock_a = Security(plaid_security_id="fx-s1", ticker="AAAA", name="Alpha Corp", type="cs")
    stock_b = Security(plaid_security_id="fx-s2", ticker="BBBB", name="Beta Corp", type="cs")
    mmf = Security(
        plaid_security_id="fx-s3",
        ticker="CCCC",
        name="Cash Reserves Fund",
        type="oef",
        is_cash_equivalent=True,
    )
    session.add_all([stock_a, stock_b, mmf])
    session.flush()

    def snap(acct: Account, sec: Security, d: date, qty: str, value: str) -> None:
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

    roth_date = _LAGGING if lag_one_account else holdings_date
    snap(roth, stock_a, roth_date, "100", "12000")
    snap(hsa, stock_a, holdings_date, "10", "1200")
    snap(brokerage, stock_b, holdings_date, "50", "5800")
    snap(brokerage, mmf, holdings_date, "1000", "1000")
    snap(retired, stock_b, _ANCIENT, "50", "5500")

    def tx(
        txid: str,
        d: date,
        type_: str,
        subtype: str | None,
        amount: str,
        name: str | None,
        security: Security | None = None,
        qty: str = "0",
    ) -> None:
        session.add(
            InvestmentTransaction(
                plaid_investment_transaction_id=txid,
                account_id=brokerage.account_id,
                security_id=security.security_id if security is not None else None,
                date=d,
                name=name,
                quantity=Decimal(qty),
                amount=Decimal(amount),
                type=type_,
                subtype=subtype,
                currency="USD",
            )
        )

    tx("fx-t4", holdings_date, "cash", "deposit", "1000", "ACH deposit")
    tx("fx-t3", holdings_date - timedelta(days=3), "cash", "withdrawal", "250", "ACH withdrawal")
    tx(
        "fx-t2",
        holdings_date - timedelta(days=5),
        "cash",
        "withdrawal",
        "12",
        "cash - DIVIDEND USD",
    )
    tx(
        "fx-t1",
        holdings_date - timedelta(days=7),
        "buy",
        "buy",
        "-580",
        "Buy Beta Corp",
        security=stock_b,
        qty="5",
    )
    session.commit()


def _with_session(fn: Callable[[Session], dict[str, Any]]) -> dict[str, Any]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            return fn(session)
    finally:
        engine.dispose()


def build_fixture_payloads() -> dict[str, dict[str, Any]]:
    from portfolio_tracker.api.routes.v1 import HealthV1, ProviderHealthV1
    from portfolio_tracker.services.v1_accounts import build_accounts_result
    from portfolio_tracker.services.v1_common import V1_SCHEMA_VERSION
    from portfolio_tracker.services.v1_history import (
        build_cash_flows_page,
        build_securities_result,
        build_transactions_page,
    )
    from portfolio_tracker.services.v1_snapshot import (
        build_portfolio_snapshot,
        build_positioning_v1,
    )

    def current(session: Session) -> dict[str, Any]:
        _seed(session, holdings_date=_FRESH)
        window_start = FIXTURE_TODAY - timedelta(days=30)
        return {
            "accounts.json": build_accounts_result(
                session, today=FIXTURE_TODAY, generated_at=FIXTURE_GENERATED_AT
            ).model_dump(mode="json"),
            "portfolio-snapshot.json": build_portfolio_snapshot(
                session, today=FIXTURE_TODAY, generated_at=FIXTURE_GENERATED_AT
            ).model_dump(mode="json"),
            "positioning.json": build_positioning_v1(
                session,
                FIXTURE_TODAY - timedelta(days=365),
                FIXTURE_TODAY,
                today=FIXTURE_TODAY,
                generated_at=FIXTURE_GENERATED_AT,
            ).model_dump(mode="json"),
            "transactions.json": build_transactions_page(
                session,
                start_date=window_start,
                end_date=FIXTURE_TODAY,
                limit=500,
                cursor=None,
                generated_at=FIXTURE_GENERATED_AT,
            ).model_dump(mode="json"),
            "cash-flows.json": build_cash_flows_page(
                session,
                start_date=window_start,
                end_date=FIXTURE_TODAY,
                include_internal=False,
                limit=500,
                cursor=None,
                generated_at=FIXTURE_GENERATED_AT,
            ).model_dump(mode="json"),
            "securities.json": build_securities_result(
                session, today=FIXTURE_TODAY, generated_at=FIXTURE_GENERATED_AT
            ).model_dump(mode="json"),
        }

    def partial(session: Session) -> dict[str, Any]:
        _seed(session, holdings_date=_FRESH, lag_one_account=True)
        return {
            "portfolio-snapshot.partial.json": build_portfolio_snapshot(
                session, today=FIXTURE_TODAY, generated_at=FIXTURE_GENERATED_AT
            ).model_dump(mode="json")
        }

    def stale(session: Session) -> dict[str, Any]:
        _seed(session, holdings_date=_ANCIENT)
        return {
            "portfolio-snapshot.stale.json": build_portfolio_snapshot(
                session, today=FIXTURE_TODAY, generated_at=FIXTURE_GENERATED_AT
            ).model_dump(mode="json")
        }

    health = HealthV1(
        status="ok",
        schema_version=V1_SCHEMA_VERSION,
        generated_at=FIXTURE_GENERATED_AT,
        database_ok=True,
        migration_version="0000example",
        providers=[
            ProviderHealthV1(
                name="plaid",
                configured=True,
                items_linked=2,
                items_active=1,
                last_successful_sync_at=_SYNC_AT,
            ),
            ProviderHealthV1(
                name="snaptrade",
                configured=True,
                items_linked=1,
                items_active=1,
                last_successful_sync_at=_SYNC_AT,
            ),
        ],
        active_account_count=3,
        latest_snapshot_date=_FRESH,
        is_stale=False,
        links={
            "accounts": "/api/v1/accounts",
            "portfolio_snapshot": "/api/v1/portfolio-snapshot",
            "positions": "/api/v1/portfolio/positions",
            "positioning": "/api/v1/analytics/positioning",
            "openapi": "/openapi.json",
        },
    )

    payloads: dict[str, dict[str, Any]] = {}
    payloads.update(_with_session(current))
    payloads.update(_with_session(partial))
    payloads.update(_with_session(stale))
    payloads["health.json"] = health.model_dump(mode="json")
    return payloads


def render(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in build_fixture_payloads().items():
        (FIXTURES_DIR / name).write_text(render(payload), encoding="utf-8", newline="\n")
        print(f"Wrote {FIXTURES_DIR / name}")


if __name__ == "__main__":
    main()
