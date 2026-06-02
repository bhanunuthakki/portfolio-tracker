"""Tests for the monthly-brief email job — pure logic + orchestration.

No network, no OAuth, no LLM: the Google send path is injected via ``send_fn``
and brief generation is exercised only through the existing-brief path.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from portfolio_tracker.jobs import email_brief
from portfolio_tracker.models import MonthlyBrief


def _first_saturday(year: int, month: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != 5:
        d += timedelta(days=1)
    return d


# --- pure helpers ------------------------------------------------------------


def test_is_first_saturday_true_only_on_first_saturday() -> None:
    fs = _first_saturday(2026, 6)
    assert email_brief.is_first_saturday(fs) is True
    # The next Saturday is the second Saturday — not the first.
    assert email_brief.is_first_saturday(fs + timedelta(days=7)) is False
    # A non-Saturday in the first week.
    assert email_brief.is_first_saturday(date(2026, 6, 1)) is False


def test_is_first_saturday_across_a_year() -> None:
    for month in range(1, 13):
        fs = _first_saturday(2026, month)
        assert email_brief.is_first_saturday(fs)
        assert fs.day <= 7


def test_default_period_is_prior_month() -> None:
    assert email_brief.default_period(date(2026, 6, 6)) == "2026-05"
    assert email_brief.default_period(date(2026, 3, 31)) == "2026-02"


def test_default_period_january_rolls_to_prior_december() -> None:
    assert email_brief.default_period(date(2026, 1, 3)) == "2025-12"


def test_period_label() -> None:
    assert email_brief.period_label("2026-05") == "May 2026"
    assert email_brief.period_label("2025-12") == "December 2025"


def test_wrap_inserts_banner_after_body_tag() -> None:
    html = "<html><head></head><body class='doc'>CONTENT</body></html>"
    out = email_brief.wrap_brief_for_email("May 2026", html)
    assert "<body class='doc'>" in out
    body_at = out.index("<body class='doc'>")
    banner_at = out.index("Monthly Brief · May 2026")
    content_at = out.index("CONTENT")
    assert body_at < banner_at < content_at


def test_wrap_prepends_banner_when_no_body() -> None:
    out = email_brief.wrap_brief_for_email("May 2026", "<p>fragment</p>")
    assert out.index("Monthly Brief · May 2026") < out.index("<p>fragment</p>")


# --- brief resolution --------------------------------------------------------


def test_resolve_returns_existing_brief_html(session: Session) -> None:
    session.add(MonthlyBrief(period_yyyymm="2026-05", html="<html><body>MAY</body></html>"))
    session.commit()
    html = email_brief._resolve_brief_html(session, "2026-05", regenerate=False)
    assert html == "<html><body>MAY</body></html>"


# --- run() orchestration -----------------------------------------------------


def test_run_skips_when_not_first_saturday(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str]] = []
    second_saturday = _first_saturday(2026, 6) + timedelta(days=7)
    result = email_brief.run(
        today=second_saturday,
        recipient="me@example.com",
        send_fn=lambda to, subj, html: calls.append((to, subj, html)) or "id",
    )
    assert result is None
    assert calls == []


def test_run_sends_prior_month_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        email_brief, "_resolve_brief_html", lambda *a, **k: "<html><body>HELLO</body></html>"
    )

    def fake_send(to: str, subject: str, html: str) -> str:
        calls.append((to, subject, html))
        return "msg-123"

    result = email_brief.run(
        today=_first_saturday(2026, 6),  # first Saturday of June → brief for May
        recipient="me@example.com",
        send_fn=fake_send,
    )
    assert result == "msg-123"
    assert len(calls) == 1
    to, subject, html = calls[0]
    assert to == "me@example.com"
    assert subject == "Portfolio Brief — May 2026"
    assert "Monthly Brief · May 2026" in html
    assert "HELLO" in html


def test_run_dry_run_does_not_send(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        email_brief, "_resolve_brief_html", lambda *a, **k: "<html><body>X</body></html>"
    )
    result = email_brief.run(
        today=_first_saturday(2026, 6),
        recipient="me@example.com",
        dry_run=True,
        send_fn=lambda *a: calls.append(a) or "id",
    )
    assert result is None
    assert calls == []


def test_run_raises_without_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    # Config has no BRIEF_EMAIL_RECIPIENT in tests; passing none must raise
    # before any DB / send work happens.
    with pytest.raises(RuntimeError, match="No recipient configured"):
        email_brief.run(today=_first_saturday(2026, 6), recipient=None, force=True)
