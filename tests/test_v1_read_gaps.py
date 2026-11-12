"""v1 successors for the legacy-only after-tax, Brinson, by-account, and policy reads."""

from __future__ import annotations

import pytest


def test_after_tax_v1_envelope_and_methodology(client):
    resp = client.get("/api/v1/analytics/after-tax", params={"st_rate": 0.3, "lt_rate": 0.15})

    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["methodology"] == "after_tax.realized_1099b"
    assert body["meta"]["methodology_version"] == "1"
    legacy = client.get("/api/portfolio/after-tax", params={"st_rate": 0.3, "lt_rate": 0.15})
    assert body["after_tax"] == legacy.json()


def test_attribution_v1_envelope_and_window_validation(client):
    resp = client.get(
        "/api/v1/analytics/attribution",
        params={"start_date": "2026-01-01", "end_date": "2026-06-30"},
    )

    assert resp.status_code == 200
    assert resp.json()["meta"]["methodology"] == "attribution.brinson_fachler_sector"
    bad = client.get(
        "/api/v1/analytics/attribution",
        params={"start_date": "2026-06-30", "end_date": "2026-01-01"},
    )
    assert bad.status_code == 422


def test_benchmark_policy_v1_matches_operator_read(client):
    resp = client.get("/api/v1/settings/benchmark-policy")

    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["methodology"] == "benchmark_policy.weights"
    operator = client.get("/api/policy").json()
    # An empty policy stamps `as_of` at read time; every other field must match.
    assert {k: v for k, v in body["policy"].items() if k != "as_of"} == {
        k: v for k, v in operator.items() if k != "as_of"
    }


@pytest.mark.parametrize(
    ("legacy", "successor"),
    [
        ("/api/portfolio/holdings/by-account", "/api/v1/portfolio/positions"),
        ("/api/portfolio/after-tax", "/api/v1/analytics/after-tax"),
        ("/api/portfolio/brinson", "/api/v1/analytics/attribution"),
    ],
)
def test_legacy_reads_point_to_v1_successor(client, legacy, successor):
    resp = client.get(legacy)

    assert resp.headers.get("Deprecation") == "true"
    assert f"<{successor}>" in resp.headers.get("Link", "")
