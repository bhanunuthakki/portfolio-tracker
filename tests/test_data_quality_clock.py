"""`build_report` must be pinnable to a caller-supplied date.

The v1 consumer fixtures are generated from fixed seed data and stamped with a
fixed `generated_at`, but two finders — staleness and backfill-anomaly — used to
read the real clock. That made the committed artifacts a time bomb: nothing in
the repo changed, the calendar crossed the 7-day staleness threshold, and
`data-quality.json` started regenerating with two extra `stale_item` findings,
turning the drift gate red on a clean tree.

These tests pin the fix: `today` controls the finders, and omitting it keeps the
live behavior of reading the real clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from portfolio_tracker.models import Item
from portfolio_tracker.services.data_quality import STALE_ITEM, build_report

_REFRESHED_AT = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _seed_item(session) -> None:
    session.add(
        Item(
            source="plaid",
            plaid_item_id="clock-test-1",
            institution_name="Example Broker A",
            is_data_active=True,
            last_refreshed_at=_REFRESHED_AT,
        )
    )
    session.commit()


def _stale_findings(report) -> list[object]:
    return [f for f in report.findings if f.category == STALE_ITEM]


def test_item_is_not_stale_when_today_is_within_the_threshold(session) -> None:
    _seed_item(session)

    report = build_report(session, today=_REFRESHED_AT.date() + timedelta(days=1))

    assert _stale_findings(report) == []


def test_item_is_stale_when_today_is_past_the_threshold(session) -> None:
    _seed_item(session)

    report = build_report(session, today=_REFRESHED_AT.date() + timedelta(days=30))

    assert len(_stale_findings(report)) == 1


def test_report_is_clock_independent_when_today_is_pinned(session) -> None:
    """The whole point: same `today`, same findings, regardless of wall time.

    This is what the fixture generator relies on — without it the committed
    artifacts drift on their own as the calendar advances.
    """
    _seed_item(session)
    pinned = _REFRESHED_AT.date() + timedelta(days=2)

    first = build_report(session, today=pinned)
    second = build_report(session, today=pinned)

    assert first.findings == second.findings
    assert first.summary_counts == second.summary_counts


def test_omitting_today_falls_back_to_the_real_clock(session) -> None:
    """Live API behavior is unchanged: the seeded item was refreshed well over
    a week before today's real date, so it must still be reported stale."""
    _seed_item(session)
    assert date.today() - _REFRESHED_AT.date() > timedelta(days=7), (
        "test premise: the seed date must be more than a week in the past"
    )

    report = build_report(session)

    assert len(_stale_findings(report)) == 1
