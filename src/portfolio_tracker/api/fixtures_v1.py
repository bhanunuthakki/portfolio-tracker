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
  * ``positions.json``                   — consolidated positions (older positions-v1 shape)
  * ``position-snapshots.json``          — historical observed holdings, bounded window
  * ``data-quality.json``                — enveloped data-quality findings
  * ``performance.json``                 — Modified-Dietz TWR + benchmark counterfactuals
  * ``position-performance.json``        — per-ticker dollar alpha
  * ``risk.json``                        — beta/volatility + drawdown together
  * ``beta.json``                        — the regression half alone
  * ``drawdown.json``                    — the loss-shaped half alone
  * ``exit-quality.json``                — sell-side quality facts

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
    Benchmark,
    HoldingSnapshot,
    InvestmentTransaction,
    Item,
    Price,
    PriceAdjustmentBasis,
    PriceSource,
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
        price: str | None = None,
        fees: str | None = None,
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
                price=Decimal(price) if price is not None else None,
                fees=Decimal(fees) if fees is not None else None,
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
        price="116",
        fees="0",
    )

    # Canonical analytics fixture: a complete, synthetic split-normalized
    # price/trade basis. Position calculations accept only named yfinance
    # split-adjusted closes; legacy/unknown rows intentionally fail closed.
    analytics_start = FIXTURE_TODAY - timedelta(days=365)
    trade_date = holdings_date - timedelta(days=7)

    def position_price(sec: Security, d: date, close: str) -> Price:
        return Price(
            security_id=sec.security_id,
            date=d,
            close=Decimal(close),
            source=PriceSource.YFINANCE.value,
            adjustment_basis=PriceAdjustmentBasis.SPLIT_ADJUSTED.value,
        )

    benchmark_dates = {analytics_start + timedelta(days=offset) for offset in range(0, 366, 7)} | {
        trade_date,
        FIXTURE_TODAY,
    }

    def stepped_benchmark(symbol: str, d: date) -> Benchmark:
        if symbol == "SPY":
            close = "110" if d == FIXTURE_TODAY else "108" if d >= trade_date else "100"
        else:
            close = "112" if d == FIXTURE_TODAY else "110" if d >= trade_date else "100"
        return Benchmark(symbol=symbol, date=d, close=Decimal(close))

    def stepped_position(sec: Security, d: date) -> Price:
        if sec is stock_a:
            close = "120" if d == FIXTURE_TODAY else "100"
        else:
            close = "116" if d >= trade_date else "100"
        return position_price(sec, d, close)

    session.add_all(
        [
            *(stepped_position(stock_a, d) for d in sorted(benchmark_dates)),
            *(stepped_position(stock_b, d) for d in sorted(benchmark_dates)),
            *(stepped_benchmark("SPY", d) for d in sorted(benchmark_dates)),
            *(stepped_benchmark("QQQ", d) for d in sorted(benchmark_dates)),
        ]
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
    from portfolio_tracker.api.routes.portfolio import (
        _consolidate_holdings,  # pyright: ignore[reportPrivateUsage]
        _latest_holding_rows,  # pyright: ignore[reportPrivateUsage]
        _load_cost_basis_overrides,  # pyright: ignore[reportPrivateUsage]
    )
    from portfolio_tracker.api.routes.v1 import DataQualityV1Result, HealthV1, ProviderHealthV1
    from portfolio_tracker.services import data_quality as data_quality_service
    from portfolio_tracker.services.beta import compute_beta
    from portfolio_tracker.services.drawdown import compute_drawdown
    from portfolio_tracker.services.exit_quality import compute_exit_quality
    from portfolio_tracker.services.performance import compute_performance_series
    from portfolio_tracker.services.position_alpha import compute_position_alpha
    from portfolio_tracker.services.positions_v1 import build_positions_result, tax_treatment
    from portfolio_tracker.services.v1_accounts import build_accounts_result
    from portfolio_tracker.services.v1_analytics import (
        BetaV1Result,
        DrawdownV1Result,
        ExitQualityV1Result,
        PerformanceV1Result,
        PositionPerformanceV1Result,
        RiskV1Result,
        exit_quality_meta,
        performance_meta,
        position_performance_meta,
        risk_meta,
    )
    from portfolio_tracker.services.v1_common import V1_SCHEMA_VERSION, build_meta
    from portfolio_tracker.services.v1_history import (
        build_cash_flows_page,
        build_position_snapshots_page,
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

    def detail_and_analytics(session: Session) -> dict[str, Any]:
        """The remaining focused resources + the enveloped analytics endpoints.

        Positions / position-snapshots / data-quality are populated from the
        holdings+transaction seed. The analytics fixtures include synthetic,
        provenance-eligible position prices and price-return benchmarks so the
        canonical position-performance sample exercises the available path;
        focused tests separately pin every unavailable reason.
        """
        _seed(session, holdings_date=_FRESH)
        window_start = FIXTURE_TODAY - timedelta(days=365)

        rows = _latest_holding_rows(session)
        if rows:
            snapshot_date = rows[0][0].snapshot_date
            overrides = _load_cost_basis_overrides(session)
            consolidated = _consolidate_holdings(snapshot_date, rows, overrides)
            account_tax = {
                a.account_id: tax_treatment(a.type, a.subtype, a.name) for _, a, _ in rows
            }
            positions = build_positions_result(snapshot_date, consolidated, account_tax)
        else:
            positions = build_positions_result(None, [], {})

        dq_meta_src = build_accounts_result(
            session, today=FIXTURE_TODAY, generated_at=FIXTURE_GENERATED_AT
        ).meta
        # `today=` pins the staleness threshold and the backfill-anomaly window.
        # Without it the finders read the real clock while everything else here
        # is seeded from fixed dates, so the committed fixture drifted on its
        # own once the calendar crossed a threshold.
        report = data_quality_service.build_report(session, today=FIXTURE_TODAY).model_copy(
            update={"generated_at": FIXTURE_GENERATED_AT}
        )
        data_quality = DataQualityV1Result(
            meta=build_meta(
                as_of=dq_meta_src.as_of,
                source_providers=dq_meta_src.source_providers,
                coverage=dq_meta_src.account_coverage,
                last_successful_sync_at=dq_meta_src.last_successful_sync_at,
                warnings=[],
                links={"health": "/api/v1/health", "accounts": "/api/v1/accounts"},
                methodology="data_quality.findings",
                methodology_version="1",
                today=FIXTURE_TODAY,
                generated_at=FIXTURE_GENERATED_AT,
            ),
            report=report,
        )

        performance = PerformanceV1Result(
            meta=performance_meta(session, today=FIXTURE_TODAY, generated_at=FIXTURE_GENERATED_AT),
            series=compute_performance_series(
                session, window_start, FIXTURE_TODAY, Decimal(0), False
            ),
        )
        position_performance = PositionPerformanceV1Result(
            meta=position_performance_meta(
                session, today=FIXTURE_TODAY, generated_at=FIXTURE_GENERATED_AT
            ),
            result=compute_position_alpha(session, window_start, FIXTURE_TODAY),
        )
        beta_result = compute_beta(
            session, window_start, FIXTURE_TODAY, "SPY", None, False, Decimal(0)
        )
        drawdown_result = compute_drawdown(session, window_start, FIXTURE_TODAY, Decimal(0), False)
        risk = RiskV1Result(
            meta=risk_meta(session, today=FIXTURE_TODAY, generated_at=FIXTURE_GENERATED_AT),
            beta=beta_result,
            drawdown=drawdown_result,
        )
        beta_only = BetaV1Result(
            meta=risk_meta(session, today=FIXTURE_TODAY, generated_at=FIXTURE_GENERATED_AT),
            beta=beta_result,
        )
        drawdown_only = DrawdownV1Result(
            meta=risk_meta(session, today=FIXTURE_TODAY, generated_at=FIXTURE_GENERATED_AT),
            drawdown=drawdown_result,
        )
        exit_quality = ExitQualityV1Result(
            meta=exit_quality_meta(session, today=FIXTURE_TODAY, generated_at=FIXTURE_GENERATED_AT),
            result=compute_exit_quality(session, window_start, FIXTURE_TODAY),
        )

        return {
            "positions.json": positions.model_dump(mode="json"),
            "position-snapshots.json": build_position_snapshots_page(
                session,
                start_date=FIXTURE_TODAY - timedelta(days=90),
                end_date=FIXTURE_TODAY,
                limit=500,
                cursor=None,
                generated_at=FIXTURE_GENERATED_AT,
            ).model_dump(mode="json"),
            "data-quality.json": data_quality.model_dump(mode="json"),
            "performance.json": performance.model_dump(mode="json"),
            "position-performance.json": position_performance.model_dump(mode="json"),
            "risk.json": risk.model_dump(mode="json"),
            "beta.json": beta_only.model_dump(mode="json"),
            "drawdown.json": drawdown_only.model_dump(mode="json"),
            "exit-quality.json": exit_quality.model_dump(mode="json"),
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
    payloads.update(_with_session(detail_and_analytics))
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
