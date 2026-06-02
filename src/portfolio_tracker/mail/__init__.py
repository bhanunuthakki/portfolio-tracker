"""Gmail send over OAuth — used by the scheduled monthly-brief email job.

Adapted from the sibling `date-suggester` project's google_io/auth modules,
trimmed to Gmail-send only (no Calendar/Drive/Photos scopes). The OAuth client
secret (`credentials.json`) and the cached token (`token.json`) live at the
repo root and are gitignored; the one-time consent flow mints the token:

    python -m portfolio_tracker.jobs.email_brief --authorize

Imports of `googleapiclient` / `google_auth_oauthlib` are heavy and only
needed when actually sending, so callers should import this package lazily
(the email job does) to keep the pure logic importable without the Google
dependencies installed.
"""

from __future__ import annotations

from portfolio_tracker.mail.auth import GmailNotConfiguredError
from portfolio_tracker.mail.client import GmailClient

__all__ = ["GmailClient", "GmailNotConfiguredError"]
