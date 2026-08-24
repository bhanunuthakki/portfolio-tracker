"""Pytest setup.

The app's `Settings` are fail-fast: Plaid creds and a Fernet key are required
or import raises. Inject throwaway values before anything imports
`portfolio_tracker.config` so the suite can import the package. None of these
touch a real account — they exist only to satisfy validation. A real `.env`
(if present) is ignored because these are set first and pydantic-settings lets
process env take precedence.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

# These satisfy the fail-fast Settings before any test imports the package.
# Imports above don't touch `portfolio_tracker.config`, so they're safe at top;
# every `portfolio_tracker.*` import is deferred into the fixtures below.
os.environ.setdefault("PLAID_CLIENT_ID", "test-client-id")
os.environ.setdefault("PLAID_SECRET", "test-secret")
os.environ.setdefault("PLAID_ENV", "sandbox")
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture
def engine() -> Generator[Engine, None, None]:
    """A throwaway in-memory DB with the full schema, isolated per test.

    StaticPool keeps the single in-memory connection alive across sessions so
    the tables created here are visible to every session bound to this engine.
    """
    from portfolio_tracker.models import Base

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Generator[Session, None, None]:
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(engine: Engine) -> Generator[object, None, None]:
    """A FastAPI TestClient whose `get_session` is bound to the test engine."""
    from fastapi.testclient import TestClient

    from portfolio_tracker.api.main import app
    from portfolio_tracker.db import get_session

    def _override() -> Generator[Session, None, None]:
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _override
    try:
        # Production is a localhost-only app. Exercise that real peer boundary
        # instead of Starlette's synthetic ``testclient`` hostname.
        with TestClient(app, client=("127.0.0.1", 50000)) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
