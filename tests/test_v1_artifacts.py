"""Drift checks for the checked-in v1 contract artifacts (Phase 0 ruling SC-6).

The OpenAPI document and the sanitized consumer fixtures are part of the
compatibility contract. If either regenerates differently from what is checked
in, the contract changed — fail and make the change visible in review.

To accept an intentional change:

    python -m portfolio_tracker.api.openapi_v1
    python -m portfolio_tracker.api.fixtures_v1
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from portfolio_tracker.api.fixtures_v1 import FIXTURES_DIR, build_fixture_payloads, render
from portfolio_tracker.api.openapi_v1 import ARTIFACT_PATH, render_v1_openapi
from portfolio_tracker.api.routes.v1 import DataQualityV1Result
from portfolio_tracker.services.positions_v1 import PositionsV1Result
from portfolio_tracker.services.v1_accounts import AccountsV1Result
from portfolio_tracker.services.v1_analytics import (
    BetaV1Result,
    DrawdownV1Result,
    ExitQualityV1Result,
    PerformanceV1Result,
    PositionPerformanceV1Result,
    RiskV1Result,
)
from portfolio_tracker.services.v1_history import (
    CashFlowsV1Result,
    PositionSnapshotsV1Result,
    SecuritiesV1Result,
    TransactionsV1Result,
)
from portfolio_tracker.services.v1_snapshot import PortfolioSnapshotV1, PositioningV1Result


def test_openapi_artifact_has_no_drift():
    assert ARTIFACT_PATH.exists(), (
        f"Missing {ARTIFACT_PATH} — run `python -m portfolio_tracker.api.openapi_v1`."
    )
    assert ARTIFACT_PATH.read_text(encoding="utf-8") == render_v1_openapi(), (
        "The generated /api/v1 OpenAPI differs from docs/api/openapi.v1.json. "
        "If the change is intentional, regenerate the artifact and review the "
        "diff against the compatibility policy."
    )


def test_fixtures_have_no_drift():
    payloads = build_fixture_payloads()
    for name, payload in payloads.items():
        path = FIXTURES_DIR / name
        assert path.exists(), (
            f"Missing fixture {path} — run `python -m portfolio_tracker.api.fixtures_v1`."
        )
        assert path.read_text(encoding="utf-8") == render(payload), (
            f"Fixture {name} drifted from the generator. If intentional, regenerate "
            "and review — both consumers pin against these files."
        )


def test_fixtures_parse_into_response_models():
    """The checked-in files themselves must deserialize into the wire models —
    this is exactly what consumer contract tests do."""
    accounts = json.loads((FIXTURES_DIR / "accounts.json").read_text(encoding="utf-8"))
    AccountsV1Result.model_validate(accounts)
    for name in (
        "portfolio-snapshot.json",
        "portfolio-snapshot.partial.json",
        "portfolio-snapshot.stale.json",
    ):
        snap = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
        PortfolioSnapshotV1.model_validate(snap)
    positioning = json.loads((FIXTURES_DIR / "positioning.json").read_text(encoding="utf-8"))
    PositioningV1Result.model_validate(positioning)
    TransactionsV1Result.model_validate(
        json.loads((FIXTURES_DIR / "transactions.json").read_text(encoding="utf-8"))
    )
    CashFlowsV1Result.model_validate(
        json.loads((FIXTURES_DIR / "cash-flows.json").read_text(encoding="utf-8"))
    )
    SecuritiesV1Result.model_validate(
        json.loads((FIXTURES_DIR / "securities.json").read_text(encoding="utf-8"))
    )
    PositionsV1Result.model_validate(
        json.loads((FIXTURES_DIR / "positions.json").read_text(encoding="utf-8"))
    )
    PositionSnapshotsV1Result.model_validate(
        json.loads((FIXTURES_DIR / "position-snapshots.json").read_text(encoding="utf-8"))
    )
    DataQualityV1Result.model_validate(
        json.loads((FIXTURES_DIR / "data-quality.json").read_text(encoding="utf-8"))
    )
    PerformanceV1Result.model_validate(
        json.loads((FIXTURES_DIR / "performance.json").read_text(encoding="utf-8"))
    )
    PositionPerformanceV1Result.model_validate(
        json.loads((FIXTURES_DIR / "position-performance.json").read_text(encoding="utf-8"))
    )
    RiskV1Result.model_validate(
        json.loads((FIXTURES_DIR / "risk.json").read_text(encoding="utf-8"))
    )
    BetaV1Result.model_validate(
        json.loads((FIXTURES_DIR / "beta.json").read_text(encoding="utf-8"))
    )
    DrawdownV1Result.model_validate(
        json.loads((FIXTURES_DIR / "drawdown.json").read_text(encoding="utf-8"))
    )
    ExitQualityV1Result.model_validate(
        json.loads((FIXTURES_DIR / "exit-quality.json").read_text(encoding="utf-8"))
    )


@pytest.mark.parametrize(
    "field",
    [
        "net_external_cashflow_in",
        "backfill_start_unreliable",
        "opening_value_provenance",
        "ending_value_provenance",
        "valuation_account_ids",
        "equation_receipt",
    ],
)
def test_performance_contract_requires_financial_provenance_fields(field: str):
    payload = json.loads((FIXTURES_DIR / "performance.json").read_text(encoding="utf-8"))
    payload["series"].pop(field)

    with pytest.raises(ValidationError):
        PerformanceV1Result.model_validate(payload)


def test_performance_receipt_rejects_a_non_reconciling_decimal_identity():
    payload = json.loads((FIXTURES_DIR / "performance.json").read_text(encoding="utf-8"))
    receipt = payload["series"]["equation_receipt"]
    assert receipt is not None
    receipt["investment_gain"] = str(Decimal(receipt["investment_gain"]) + Decimal("0.01"))

    with pytest.raises(ValidationError, match="whole-portfolio value bridge"):
        PerformanceV1Result.model_validate(payload)


def test_fixture_scenarios_express_their_states():
    payloads = build_fixture_payloads()
    current = PortfolioSnapshotV1.model_validate(payloads["portfolio-snapshot.json"])
    assert current.meta.is_stale is False
    assert current.meta.is_partial is False

    partial = PortfolioSnapshotV1.model_validate(payloads["portfolio-snapshot.partial.json"])
    assert partial.meta.is_partial is True
    assert any(w.code == "PARTIAL_COVERAGE" for w in partial.meta.warnings)

    stale = PortfolioSnapshotV1.model_validate(payloads["portfolio-snapshot.stale.json"])
    assert stale.meta.is_stale is True
    assert any(w.code == "STALE_HOLDINGS" for w in stale.meta.warnings)

    # The five-way treatment appears distinctly (SC-1): roth and hsa are never
    # collapsed, and the excluded account exists with an exclusion reason.
    assert current.by_tax_treatment["roth"] != current.by_tax_treatment["hsa"]
    excluded = [a for a in current.accounts if not a.included_in_totals]
    assert excluded and excluded[0].exclusion_reason == "operator_excluded"


def test_fixtures_contain_no_live_looking_values():
    """Fixtures are synthetic by construction; belt-and-braces check that the
    obvious real-world markers never sneak in."""
    for name in FIXTURES_DIR.glob("*.json"):
        text = name.read_text(encoding="utf-8").lower()
        for marker in ("robinhood", "fidelity", "sofi", "schwab", "vanguard"):
            assert marker not in text, f"{name.name} contains live-looking marker {marker!r}"
