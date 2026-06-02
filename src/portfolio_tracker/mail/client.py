"""Thin Gmail send wrapper.

One ``GmailClient`` per process run; it loads OAuth credentials (refreshing or
running consent as needed) and holds a Gmail Discovery service handle.
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from typing import Any, cast

from googleapiclient.discovery import build  # pyright: ignore[reportUnknownVariableType]
from googleapiclient.errors import HttpError  # pyright: ignore[reportUnknownVariableType]

from portfolio_tracker.mail._redact import redact
from portfolio_tracker.mail.auth import load_credentials


class GmailClient:
    """Send mail as the authenticated user via the Gmail API."""

    def __init__(self) -> None:
        creds = load_credentials()
        # cache_discovery=False silences the noisy "file_cache is only
        # supported with oauth2client<4.0.0" warning on every run.
        _build = cast(Any, build)
        self._gmail: Any = _build("gmail", "v1", credentials=creds, cache_discovery=False)

    def send_html(self, to: str, subject: str, html_body: str) -> str:
        """Send an HTML email and return the Gmail message id."""
        msg = MIMEText(html_body, _subtype="html", _charset="utf-8")
        msg["To"] = to
        msg["From"] = "me"  # Gmail accepts "me" as the authenticated-user alias.
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        try:
            sent = self._gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
        except HttpError as e:
            raise RuntimeError(f"Gmail send failed: {redact(e)}") from None
        return cast(str, sent["id"])
