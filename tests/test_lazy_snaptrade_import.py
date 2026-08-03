"""Guard: the optional SnapTrade SDK must not load at app import.

The vendor `snaptrade_client` package is the single most expensive import in
the tree — ~5s warm, ~30s on a cold/Drive-synced cache — and it dominated
FastAPI boot, long enough that a dev-server or preview launcher probing :8000
gave up before uvicorn ever bound the port.

SnapTrade is an OPTIONAL integration (`config.py`: with SNAPTRADE_CLIENT_ID /
SNAPTRADE_CONSUMER_KEY unset the routes 503 cleanly and the rest of the app
keeps working with Plaid only), so paying that cost on every boot is pure
waste. `portfolio_tracker.snaptrade_client` now imports the SDK inside
`_ensure_snaptrade()`, on first SnapTrade use.

These tests pin both halves of the deal: the SDK stays out of the import graph
at boot, and the SnapTrade surface still behaves — a real client when
credentials are configured, a clean 503 when they aren't.
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
# Runs in a clean interpreter because this suite's own modules import
# `portfolio_tracker.snaptrade_client` directly, which would otherwise make the
# in-process assertion depend on test ordering.
_GUARD = (
    "import sys;"
    "from portfolio_tracker.api.main import app;"
    "assert 'snaptrade_client' not in sys.modules, "
    "'SnapTrade SDK was imported eagerly at app import';"
    "assert 'portfolio_tracker.snaptrade_client' in sys.modules"
)


def test_app_import_does_not_load_snaptrade_sdk() -> None:
    """Importing the FastAPI app must leave the vendor SDK unimported.

    Note the two module names differ by package: `snaptrade_client` is the
    vendor SDK (the expensive one), `portfolio_tracker.snaptrade_client` is our
    thin wrapper, which must still import — the routes and this suite depend on
    its Pydantic models.
    """
    env = {**os.environ, "PYTHONPATH": _SRC}
    result = subprocess.run(
        [sys.executable, "-c", _GUARD],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
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
