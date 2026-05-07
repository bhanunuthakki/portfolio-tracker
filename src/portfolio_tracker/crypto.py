"""Symmetric encryption for Plaid access tokens at rest.

Plaid access tokens are long-lived bearer credentials. Storing them in plaintext
in SQLite would mean a stolen DB file is full account access. Fernet (AES-128-CBC
+ HMAC-SHA256) is enough for a single-user local app: the threat is "laptop is
stolen and DB file is read directly" rather than a sophisticated adversary with
the FERNET_KEY.

The key lives in `.env` (gitignored). Losing it means re-linking every Item.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from portfolio_tracker.config import get_settings


def _cipher() -> Fernet:
    return Fernet(get_settings().fernet_key.encode("utf-8"))


def encrypt_token(plaintext: str) -> str:
    return _cipher().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
    return _cipher().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
