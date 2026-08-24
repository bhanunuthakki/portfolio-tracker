"""Governed policy-write contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from portfolio_tracker.models import PolicyState, PolicyWeight, PolicyWriteReceipt

_WRITE_HEADERS = {"X-Portfolio-Write-Intent": "replace-policy"}


def _request(
    *,
    key: str = "policy-write-0001",
    expected_revision: int = 0,
    ticker: str = " qqq ",
    weight_pct: str = "100.000",
) -> dict[str, object]:
    return {
        "weights": [{"ticker": ticker, "weight_pct": weight_pct, "notes": " growth "}],
        "expected_revision": expected_revision,
        "idempotency_key": key,
        "source": "earnings_summary",
        "as_of": datetime(2026, 8, 23, 12, 0, tzinfo=UTC).isoformat(),
    }


def test_policy_write_requires_local_explicit_intent(client) -> None:
    response = client.put("/api/policy", json=_request())

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "POLICY_WRITE_UNAUTHORIZED"


def test_policy_write_rejects_unconfigured_browser_origin(client) -> None:
    response = client.put(
        "/api/policy",
        json=_request(),
        headers={**_WRITE_HEADERS, "Origin": "https://untrusted.example"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "POLICY_WRITE_UNAUTHORIZED"


def test_policy_write_returns_fresh_normalized_state_and_durable_receipt(client, session) -> None:
    response = client.put("/api/policy", json=_request(), headers=_WRITE_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 1
    assert body["source"] == "earnings_summary"
    assert body["as_of"] == "2026-08-23T12:00:00Z"
    assert body["weights"] == [
        {
            "ticker": "QQQ",
            "weight_pct": "100.00",
            "notes": "growth",
            "updated_at": body["weights"][0]["updated_at"],
        }
    ]
    assert body["recomputation"] == {
        "status": "required",
        "policy_revision": 1,
        "reason": "policy_weights_changed",
    }
    assert body["receipt"]["outcome"] == "applied"

    session.expire_all()
    assert session.scalar(select(PolicyState.revision)) == 1
    assert session.scalar(select(func.count()).select_from(PolicyWriteReceipt)) == 1

    fresh = client.get("/api/policy").json()
    assert fresh["revision"] == 1
    assert fresh["weights"] == body["weights"]
    assert fresh["receipt"] is None


def test_policy_write_replays_same_idempotency_key_without_a_second_write(client, session) -> None:
    first = client.put("/api/policy", json=_request(), headers=_WRITE_HEADERS)
    replay = client.put("/api/policy", json=_request(), headers=_WRITE_HEADERS)

    assert first.status_code == replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    session.expire_all()
    assert session.scalar(select(PolicyState.revision)) == 1
    assert session.scalar(select(func.count()).select_from(PolicyWriteReceipt)) == 1


def test_policy_write_rejects_idempotency_key_reuse_with_other_content(client) -> None:
    assert client.put("/api/policy", json=_request(), headers=_WRITE_HEADERS).status_code == 200

    response = client.put(
        "/api/policy",
        json=_request(ticker="SPY"),
        headers=_WRITE_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "POLICY_IDEMPOTENCY_CONFLICT"


def test_policy_write_rejects_stale_expected_revision(client) -> None:
    assert client.put("/api/policy", json=_request(), headers=_WRITE_HEADERS).status_code == 200

    response = client.put(
        "/api/policy",
        json=_request(key="policy-write-0002", expected_revision=0),
        headers=_WRITE_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "POLICY_REVISION_CONFLICT",
        "expected_revision": 0,
        "current_revision": 1,
    }


def test_policy_write_reports_semantic_validation_distinctly(client) -> None:
    request = _request()
    request["weights"] = [
        {"ticker": "QQQ", "weight_pct": "50"},
        {"ticker": " qqq ", "weight_pct": "50"},
    ]

    response = client.put("/api/policy", json=request, headers=_WRITE_HEADERS)

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "POLICY_VALIDATION_FAILED",
        "reason": "duplicate_ticker",
    }


@pytest.mark.parametrize(
    ("weights", "reason"),
    [
        ([], "policy_must_not_be_empty"),
        (
            [{"ticker": "QQQ", "weight_pct": "99.98"}],
            "policy_total_must_equal_100_pct",
        ),
        (
            [
                {"ticker": "QQQ", "weight_pct": "100"},
                {"ticker": "SPY", "weight_pct": "0.02"},
            ],
            "policy_total_must_equal_100_pct",
        ),
    ],
)
def test_policy_write_rejects_empty_or_unbalanced_normalized_total(client, weights, reason) -> None:
    request = _request()
    request["weights"] = weights

    response = client.put("/api/policy", json=request, headers=_WRITE_HEADERS)

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "POLICY_VALIDATION_FAILED",
        "reason": reason,
    }


@pytest.mark.parametrize(
    "weights",
    [
        [{"ticker": "QQQ", "weight_pct": "99.99"}],
        [
            {"ticker": "QQQ", "weight_pct": "100"},
            {"ticker": "SPY", "weight_pct": "0.01"},
        ],
    ],
)
def test_policy_write_accepts_documented_one_basis_point_tolerance(client, weights) -> None:
    request = _request()
    request["weights"] = weights

    response = client.put("/api/policy", json=request, headers=_WRITE_HEADERS)

    assert response.status_code == 200
    assert abs(int(Decimal(response.json()["total_pct"]) * 100) - 10000) <= 1


def test_policy_write_rolls_back_when_recomputation_invalidation_fails(
    client, session, monkeypatch
) -> None:
    from portfolio_tracker.services import policy_write

    def _fail(*_args, **_kwargs) -> None:
        raise policy_write.PolicyRecomputationError

    monkeypatch.setattr(policy_write, "_mark_benchmark_recomputation_required", _fail)

    response = client.put("/api/policy", json=_request(), headers=_WRITE_HEADERS)

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "POLICY_RECOMPUTATION_INVALIDATION_FAILED",
        "retryable": True,
    }
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(PolicyWeight)) == 0
    assert session.scalar(select(func.count()).select_from(PolicyWriteReceipt)) == 0
