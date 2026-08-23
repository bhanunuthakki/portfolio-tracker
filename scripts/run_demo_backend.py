"""Launch Portfolio Tracker API in Demo Mode with 100% synthetic fixtures.

Seeds an isolated SQLite database (`demo_portfolio.db`) with synthetic
accounts, holdings, and transactions so you can record screencasts with zero
risk of leaking personal balances, real accounts, or holdings.

Usage:
    python scripts/run_demo_backend.py
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from portfolio_tracker.api.fixtures_v1 import FIXTURE_TODAY, _seed  # pyright: ignore[reportPrivateUsage]
from portfolio_tracker.models import Base

DEMO_DB_PATH = Path(__file__).resolve().parents[1] / "demo_portfolio.db"
DEMO_DB_URL = f"sqlite:///{DEMO_DB_PATH}"


def init_demo_db() -> None:
    """Create and seed the demo database."""
    if DEMO_DB_PATH.exists():
        DEMO_DB_PATH.unlink()

    engine = create_engine(DEMO_DB_URL, future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed(session, holdings_date=FIXTURE_TODAY, lag_one_account=False)

    print(f"✅ Demo database seeded with synthetic data at: {DEMO_DB_PATH}")


def main() -> None:
    init_demo_db()
    os.environ["DATABASE_URL"] = DEMO_DB_URL
    os.environ["PLAID_CLIENT_ID"] = "dummy-plaid-client-id"
    os.environ["PLAID_SECRET"] = "dummy-plaid-secret"
    os.environ["PLAID_ENV"] = "sandbox"
    os.environ["FERNET_KEY"] = "8_sL1vXm2Q0K5n6P7R8T9U0V1W2X3Y4Z5A6B7C8D9E0="

    print("\n🚀 Starting Portfolio Tracker API in DEMO MODE on http://localhost:8000")
    print("Zero real accounts or balances are loaded. Safe for recording.")
    uvicorn.run("portfolio_tracker.api.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
