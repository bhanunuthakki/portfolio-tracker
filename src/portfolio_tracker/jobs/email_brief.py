"""Email the monthly portfolio brief.

Intended to run from Windows Task Scheduler **every Saturday** (see
``scripts/run_email_brief.bat`` / ``scripts/install-email-brief-task.ps1``);
the job self-gates and only sends on the **first Saturday of the month**, so
the schedule is a simple weekly trigger and the "first Saturday" rule lives in
one place (here). Use ``--force`` for a manual off-cadence send.

By default it emails the brief for the **prior** calendar month (the just-
completed month). If no brief exists for that period yet, it generates one
first (an Opus call via the CIO advisor); ``--regenerate`` forces a fresh one.

Examples:
    python -m portfolio_tracker.jobs.email_brief                  # scheduled path
    python -m portfolio_tracker.jobs.email_brief --force          # send now
    python -m portfolio_tracker.jobs.email_brief --period 2026-05 # a specific month
    python -m portfolio_tracker.jobs.email_brief --force --dry-run # render, don't send
    python -m portfolio_tracker.jobs.email_brief --authorize      # one-time OAuth consent

The recipient comes from ``BRIEF_EMAIL_RECIPIENT`` in ``.env`` (PII — kept
out of tracked files); ``--to`` overrides it.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from datetime import date

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.config import get_settings
from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import MonthlyBrief

# A callable that sends one HTML email and returns a message id. Injected in
# tests; the production default constructs a GmailClient lazily so importing
# this module doesn't require the Google API dependencies.
Sender = Callable[[str, str, str], str]


def is_first_saturday(d: date) -> bool:
    """True iff ``d`` is the first Saturday of its month."""
    return d.weekday() == 5 and d.day <= 7


def default_period(today: date) -> str:
    """The prior calendar month as ``YYYY-MM`` (the just-completed month)."""
    year, month = today.year, today.month
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def period_label(period: str) -> str:
    """Render ``2026-05`` as ``May 2026`` for the subject line + banner."""
    year_str, month_str = period.split("-")
    return f"{calendar.month_name[int(month_str)]} {year_str}"


def wrap_brief_for_email(label: str, html: str) -> str:
    """Insert a small provenance banner into the brief's HTML body.

    The brief HTML is a full self-contained document; we splice the banner in
    just after the opening ``<body>`` tag (or prepend it if there's no body).
    Inline-styled so it survives Gmail's CSS stripping.
    """
    banner = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        "font-size:12px;line-height:1.5;color:#6b6b6b;padding:10px 14px;"
        'border-bottom:1px solid #e5e5e5;margin:0 0 16px;">'
        f"Portfolio Tracker · Monthly Brief · {label} · generated automatically. "
        "The interactive version lives in the app.</div>"
    )
    lower = html.lower()
    start = lower.find("<body")
    if start != -1:
        end = lower.find(">", start)
        if end != -1:
            return html[: end + 1] + "\n" + banner + html[end + 1 :]
    return banner + html


def _resolve_brief_html(db: Session, period: str, *, regenerate: bool) -> str:
    """Return the brief HTML for ``period``, generating it if needed.

    Prefers an existing brief row. Generation is imported lazily so this
    module stays importable (and its pure logic testable) without the CIO
    advisor's LLM dependencies.
    """
    if not regenerate:
        existing = db.execute(
            select(MonthlyBrief).where(MonthlyBrief.period_yyyymm == period)
        ).scalar_one_or_none()
        if existing is not None:
            return existing.html

    from portfolio_tracker.services.cio_advisor import generate_brief

    return generate_brief(db, period).html


def _default_sender(to: str, subject: str, html_body: str) -> str:
    from portfolio_tracker.mail import GmailClient

    return GmailClient().send_html(to, subject, html_body)


def run(
    *,
    period: str | None = None,
    force: bool = False,
    regenerate: bool = False,
    recipient: str | None = None,
    dry_run: bool = False,
    today: date | None = None,
    send_fn: Sender | None = None,
) -> str | None:
    """Send the monthly brief email. Returns the message id, or None if skipped.

    Skips silently (returns None) when today isn't the first Saturday and
    ``force`` is False — this is the normal weekly-scheduler no-op.
    """
    today = today or date.today()
    if not force and not is_first_saturday(today):
        print(
            f"Skipping: {today.isoformat()} is not the first Saturday of the month "
            "(use --force to send anyway)."
        )
        return None

    period = period or default_period(today)
    to = recipient or get_settings().brief_email_recipient
    if not to:
        raise RuntimeError(
            "No recipient configured. Set BRIEF_EMAIL_RECIPIENT in .env or pass --to."
        )

    label = period_label(period)
    subject = f"Portfolio Brief — {label}"

    db = SessionLocal()
    try:
        brief_html = _resolve_brief_html(db, period, regenerate=regenerate)
    finally:
        db.close()

    email_html = wrap_brief_for_email(label, brief_html)

    if dry_run:
        print(f"[dry-run] would send '{subject}' to {to} ({len(email_html)} chars HTML). Not sent.")
        return None

    sender = send_fn or _default_sender
    message_id = sender(to, subject, email_html)
    print(f"Sent '{subject}' to {to} (message id {message_id}).")
    return message_id


def main(
    period: str | None = typer.Option(
        None, "--period", help="Month to send as YYYY-MM (default: the prior month)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Send even if today isn't the first Saturday of the month."
    ),
    regenerate: bool = typer.Option(
        False, "--regenerate", help="Regenerate the brief even if one already exists."
    ),
    to: str | None = typer.Option(
        None, "--to", help="Override the recipient (default: BRIEF_EMAIL_RECIPIENT)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Resolve and render the brief but don't send."
    ),
    authorize: bool = typer.Option(
        False, "--authorize", help="Run the one-time Google OAuth consent flow and exit."
    ),
) -> None:
    if authorize:
        from portfolio_tracker.mail.auth import load_credentials

        creds = load_credentials()
        print(f"Authorized. Token saved. Scopes: {creds.scopes}")
        return

    run(period=period, force=force, regenerate=regenerate, recipient=to, dry_run=dry_run)


if __name__ == "__main__":
    typer.run(main)
