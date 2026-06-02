"""One-time OAuth flow + silent refresh for Gmail send.

Run the consent flow once:

    python -m portfolio_tracker.jobs.email_brief --authorize

This opens a browser, authorizes this app against your Google account for the
single ``gmail.send`` scope, and writes ``token.json`` at the repo root.
Subsequent runs load + silently refresh that token.

Prerequisite: ``credentials.json`` (OAuth client secret, type "Desktop app")
at the repo root. You can reuse the same client secret as other Google-API
projects on this machine — the token is minted per-project per-scope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import (  # pyright: ignore[reportUnknownVariableType]
    InstalledAppFlow,
)

from portfolio_tracker.config import get_settings

# Least privilege: sending the monthly brief needs only gmail.send.
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

_CONSENT_BANNER = (
    "\n"
    + "=" * 78
    + "\n"
    "OPEN THIS URL IN YOUR BROWSER TO AUTHORIZE Gmail send:\n"
    "\n"
    "  {url}\n"
    "\n"
    "After consenting, you'll be redirected to a localhost page that says\n"
    "'The authentication flow has completed.' Close that tab and this\n"
    "command will exit automatically.\n"
    + "=" * 78
    + "\n"
)


class GmailNotConfiguredError(RuntimeError):
    """Raised when the OAuth client secret (credentials.json) is missing."""


def _credentials_path() -> Path:
    return get_settings().resolved_google_credentials_path


def _token_path() -> Path:
    return get_settings().resolved_google_token_path


def load_credentials() -> Credentials:
    """Return valid OAuth credentials, running the consent flow if needed.

    Re-runs consent whenever ``SCOPES`` adds a permission the stored token
    wasn't issued with — otherwise ``from_authorized_user_file`` attaches the
    new scope list to creds whose access token doesn't actually carry it,
    yielding a 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT at first API call.
    """
    token_path = _token_path()
    credentials_path = _credentials_path()

    creds: Credentials | None = None
    if token_path.exists():
        granted_scopes: set[str]
        try:
            token_data = cast(dict[str, Any], json.loads(token_path.read_text(encoding="utf-8")))
            granted_scopes = set(cast(list[str], token_data.get("scopes", [])))
        except (OSError, json.JSONDecodeError):
            granted_scopes = set()
        if granted_scopes.issuperset(SCOPES):
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        else:
            missing = set(SCOPES) - granted_scopes
            print(f"Token missing scope(s) {missing} — forcing fresh consent flow.")

    if creds is not None and creds.valid:
        return creds

    if creds is not None and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if not credentials_path.exists():
        raise GmailNotConfiguredError(
            f"Missing OAuth client secret at {credentials_path}. Create an OAuth client "
            "(type 'Desktop app') in the Google Cloud console, download it as "
            "credentials.json, drop it at the repo root, then run:\n"
            "  python -m portfolio_tracker.jobs.email_brief --authorize"
        )

    # open_browser=False: this is often launched from a subprocess/SSH where
    # webbrowser.open() can't reach the user's session. Print the URL instead.
    flow: Any = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    fresh_creds = cast(
        Credentials,
        flow.run_local_server(
            port=0,
            open_browser=False,
            authorization_prompt_message=_CONSENT_BANNER,
        ),
    )
    token_path.write_text(fresh_creds.to_json(), encoding="utf-8")
    return fresh_creds
