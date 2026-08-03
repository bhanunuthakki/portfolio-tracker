"""Guard: neither aggregator SDK may load at app import.

The two vendor SDKs were the most expensive imports in the tree and both were
paid on every uvicorn boot, delaying the port bind long enough that a
dev-server or preview launcher probing :8000 gave up before the app ever bound.

  * `snaptrade_client` — ~5s warm, ~30s on a cold/Drive-synced cache. An
    OPTIONAL integration (`config.py`: with SNAPTRADE_CLIENT_ID /
    SNAPTRADE_CONSUMER_KEY unset the routes 503 cleanly and the rest of the app
    keeps working with Plaid only), so the eager cost was pure waste.
  * `plaid` — ~2.9s, most of it hundreds of generated `plaid.model.*` modules.
    Required at runtime, but not at import: only the five SDK-calling functions
    in `plaid_client` need it.

Both now import on first use. These tests pin both halves of the deal: the SDKs
stay out of the boot import graph, and the surfaces still behave — real clients
when credentials are configured, a clean 503 for SnapTrade when they aren't.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import portfolio_tracker

_SRC = str(Path(portfolio_tracker.__file__).resolve().parents[1])


# Mirrors the earnings-summary guard
# (`python -c "import llm.cli, sys; assert 'google.generativeai' not in sys.modules"`).
# `sdk` must be absent from the boot graph; `wrapper` is our thin module around
# it, which must still import. The check runs in a clean interpreter because
# this suite imports both wrappers directly, which would otherwise make the
# in-process assertion depend on test ordering.
def _guard(sdk: str, wrapper: str) -> str:
    return (
        "import sys;"
        "from portfolio_tracker.api.main import app;"
        f"assert {sdk!r} not in sys.modules, "
        f"'{sdk} was imported eagerly at app import';"
        f"assert {wrapper!r} in sys.modules"
    )


def _run_guard(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": _SRC},
    )


def test_app_import_does_not_load_snaptrade_sdk() -> None:
    """Importing the FastAPI app must leave the vendor SDK unimported.

    Note the two module names differ by package: `snaptrade_client` is the
    vendor SDK (the expensive one), `portfolio_tracker.snaptrade_client` is our
    thin wrapper, which must still import — the routes and this suite depend on
    its Pydantic models.
    """
    result = _run_guard(_guard("snaptrade_client", "portfolio_tracker.snaptrade_client"))
    assert result.returncode == 0, result.stderr


def test_app_import_does_not_load_plaid_sdk() -> None:
    """Same deal for `plaid-python`: ~2.9s of generated `plaid.model.*` modules
    that only the five SDK-calling functions in `plaid_client` need.

    Plaid is a REQUIRED integration, unlike SnapTrade — but "required at
    runtime" is not "required at import". A request that never calls Plaid
    should never pay for the SDK.
    """
    result = _run_guard(_guard("plaid", "portfolio_tracker.plaid_client"))
    assert result.returncode == 0, result.stderr


def _stub_settings(client_id: str | None, consumer_key: str | None) -> object:
    return SimpleNamespace(snaptrade_client_id=client_id, snaptrade_consumer_key=consumer_key)


@pytest.fixture
def uncached_client(monkeypatch: pytest.MonkeyPatch):
    """Clear the module-level client cache so each test builds its own."""
    from portfolio_tracker import snaptrade_client

    monkeypatch.setattr(snaptrade_client, "_client", None)
    return snaptrade_client


def test_get_client_builds_a_real_sdk_client_when_configured(uncached_client, monkeypatch) -> None:
    """The deferred import still resolves — credentials configured → real client."""
    monkeypatch.setattr(
        uncached_client, "get_settings", lambda: _stub_settings("test-id", "test-key")
    )

    client = uncached_client.get_client()

    from snaptrade_client import SnapTrade

    assert isinstance(client, SnapTrade)
    # Cached: a second call must not re-import or rebuild.
    assert uncached_client.get_client() is client


def test_is_configured_reflects_credentials(uncached_client, monkeypatch) -> None:
    monkeypatch.setattr(uncached_client, "get_settings", lambda: _stub_settings(None, None))
    assert uncached_client.is_configured() is False

    monkeypatch.setattr(uncached_client, "get_settings", lambda: _stub_settings("id", "key"))
    assert uncached_client.is_configured() is True


def test_get_client_raises_not_configured_without_credentials(uncached_client, monkeypatch) -> None:
    """Missing credentials must fail as SnapTradeNotConfiguredError — the error
    the routes translate into a 503 — and must do so *before* paying for the
    SDK import."""
    monkeypatch.setattr(uncached_client, "get_settings", lambda: _stub_settings(None, None))

    with pytest.raises(uncached_client.SnapTradeNotConfiguredError):
        uncached_client.get_client()


def test_connection_portal_url_503s_when_unconfigured(client, monkeypatch) -> None:
    """End-to-end: the route contract in config.py holds — unset credentials
    give a clean 503, not a 500."""
    from portfolio_tracker import snaptrade_client

    monkeypatch.setattr(snaptrade_client, "_client", None)
    monkeypatch.setattr(snaptrade_client, "get_settings", lambda: _stub_settings(None, None))

    response = client.post("/api/snaptrade/connection-portal-url?profile=primary")

    assert response.status_code == 503
    assert "SNAPTRADE_CLIENT_ID" in response.json()["detail"]


def test_status_endpoint_reports_unconfigured(client, monkeypatch) -> None:
    from portfolio_tracker import snaptrade_client

    monkeypatch.setattr(snaptrade_client, "get_settings", lambda: _stub_settings(None, None))

    response = client.get("/api/snaptrade/status")

    assert response.status_code == 200
    assert response.json() == {"configured": False}


def test_plaid_client_still_builds_from_the_deferred_import(monkeypatch) -> None:
    """The Plaid deferred imports still resolve — building a client is local
    (no network), so this exercises the real SDK path end to end.

    conftest injects throwaway Plaid credentials, so `get_settings()` is
    satisfied without touching a real account.
    """
    from portfolio_tracker import plaid_client

    monkeypatch.setattr(plaid_client, "_client", None)

    built = plaid_client._build_client()

    from plaid.api import plaid_api

    assert isinstance(built, plaid_api.PlaidApi)
    # And the cache still short-circuits.
    monkeypatch.setattr(plaid_client, "_client", built)
    assert plaid_client.get_client() is built
