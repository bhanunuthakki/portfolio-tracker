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

from cryptography.fernet import Fernet

os.environ.setdefault("PLAID_CLIENT_ID", "test-client-id")
os.environ.setdefault("PLAID_SECRET", "test-secret")
os.environ.setdefault("PLAID_ENV", "sandbox")
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
