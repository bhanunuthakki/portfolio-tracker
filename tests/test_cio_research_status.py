"""Tests for the CIO brief's earnings-summary research-status facts wiring.

The brief used to assert "the decision log is empty" / "held without a thesis"
purely from this tracker's own (sparse) tables. `_research_status_for` pulls the
companion earnings-summary platform's per-holding thesis/ledger status so the
brief can be grounded in the real research history; these tests pin the
coverage-marking (absent tickers flagged as a genuine gap) and the prompt-block
rendering (populated vs "platform unavailable").
"""

from __future__ import annotations

import pytest

from portfolio_tracker.services import cio_advisor
from portfolio_tracker.services.earnings_summary import ThesisStatusSummary


def test_research_status_marks_covered_and_uncovered(monkeypatch: pytest.MonkeyPatch) -> None:
    # NU is documented (thesis + ledger); FLKR has no coverage in the platform.
    def fake_status(tickers: list[str]) -> dict[str, ThesisStatusSummary]:
        return {
            "NU": ThesisStatusSummary(
                ticker="NU",
                has_written_thesis=True,
                breach_status="ok",
                ledger_entry_count=17,
                last_ledger_at="2026-06-01T18:18:24",
                open_notes_count=3,
                open_questions_count=1,
            )
        }

    monkeypatch.setattr(cio_advisor.earnings_summary, "thesis_status_by_ticker", fake_status)

    positions: list[dict[str, object]] = [{"ticker": "NU"}, {"ticker": "FLKR"}]
    out = cio_advisor._research_status_for(positions)
    by_ticker = {r["ticker"]: r for r in out}

    assert by_ticker["NU"]["in_research_platform"] is True
    assert by_ticker["NU"]["has_written_thesis"] is True
    assert by_ticker["NU"]["ledger_entries"] == 17
    assert by_ticker["NU"]["last_decision_at"] == "2026-06-01T18:18:24"

    # Absent from the platform → explicit no-coverage row (the real gap signal).
    assert by_ticker["FLKR"]["in_research_platform"] is False
    assert by_ticker["FLKR"]["has_written_thesis"] is False
    assert by_ticker["FLKR"]["ledger_entries"] == 0


def test_research_status_empty_when_platform_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def empty_status(tickers: list[str]) -> dict[str, ThesisStatusSummary]:
        return {}

    monkeypatch.setattr(cio_advisor.earnings_summary, "thesis_status_by_ticker", empty_status)
    positions: list[dict[str, object]] = [{"ticker": "NU"}]
    assert cio_advisor._research_status_for(positions) == []


def test_research_block_renders_data_and_absence() -> None:
    populated = cio_advisor._research_status_block(
        [{"ticker": "NU", "in_research_platform": True, "has_written_thesis": True}]
    )
    assert "Earnings-summary research status" in populated
    assert '"NU"' in populated

    absent = cio_advisor._research_status_block([])
    assert "not reachable" in absent
    # Must instruct the model NOT to infer "no thesis" from an unavailable platform.
    assert "Do NOT" in absent or "do NOT" in absent
