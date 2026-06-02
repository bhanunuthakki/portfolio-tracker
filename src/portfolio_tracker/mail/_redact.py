"""Redact credentials before logging.

`HttpError` and most connection errors embed the full URL — including the
query string — in their stringified message. Any code path that does
``raise RuntimeError(f"... {exc}")`` will therefore surface API keys. Route
exception strings through ``redact()`` before logging or re-raising.

Mirrors date-suggester/src/log_redact.py and earnings-summary/src/log_redact.py.
"""

from __future__ import annotations

import re

_CRED_RE = re.compile(
    r"(?P<param>apikey|api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)=(?P<val>[^&\s]+)",
    re.IGNORECASE,
)


def redact(text: object) -> str:
    """Mask credential-bearing query params in ``text`` (coerced via str())."""
    return _CRED_RE.sub(lambda m: f"{m.group('param')}=***", str(text))
