"""Plaid access tokens are encrypted at rest with Fernet. These guard the
round-trip and that a stolen-but-corrupt ciphertext can't be decrypted."""

from __future__ import annotations

import pytest
from cryptography.fernet import InvalidToken

from portfolio_tracker.crypto import decrypt_token, encrypt_token


def test_round_trip():
    token = "access-production-0a1b2c3d-dead-beef"
    ciphertext = encrypt_token(token)
    assert decrypt_token(ciphertext) == token


def test_ciphertext_hides_plaintext():
    secret = "access-production-super-secret"
    ciphertext = encrypt_token(secret)
    assert secret not in ciphertext
    assert ciphertext != secret


def test_encryption_is_nondeterministic():
    # Fernet embeds a random IV + timestamp, so the same plaintext encrypts to
    # different ciphertexts — both must still decrypt back to the original.
    a = encrypt_token("same-token")
    b = encrypt_token("same-token")
    assert a != b
    assert decrypt_token(a) == decrypt_token(b) == "same-token"


def test_decrypt_garbage_raises():
    with pytest.raises(InvalidToken):
        decrypt_token("not-a-valid-fernet-token")
