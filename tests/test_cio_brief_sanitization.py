"""Security tests for monthly-brief HTML sanitization.

The brief is assembled from model-produced HTML and then rendered in an
in-app iframe, opened as a standalone `data:text/html` page, and emailed.
A prompt-injected holding/thesis name could try to smuggle a `<script>`
(or `onerror=`, `<iframe>`, `javascript:` URL) through the model into any
of those sinks. `_parse_brief_sections` is the single chokepoint that
allowlist-sanitizes every section, and `_render_brief_html` HTML-escapes
the request-supplied `period_yyyymm`. These tests pin both behaviors.

The companion earnings-summary passthrough route is also covered: its
served reports are static, so it must attach `Content-Security-Policy:
sandbox` to neutralize any embedded script in the app's origin.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_tracker.services.cio_advisor import (
    FactsSnapshot,
    _parse_brief_sections,
    _render_brief_html,
)


def _snap() -> FactsSnapshot:
    return FactsSnapshot(
        as_of="2026-06-04",
        portfolio_total_value=100_000,
        positions=[],
        recent_decisions=[],
        coaching_findings={},
    )


def test_parse_brief_sections_strips_script() -> None:
    """A <script> in a section is removed — tag AND its payload."""
    raw = json.dumps(
        {
            "executive_summary": (
                "<p>Portfolio is healthy.</p><script>alert('xss');fetch('/api/secrets')</script>"
            )
        }
    )
    sections = _parse_brief_sections(raw)
    body = sections["executive_summary"]
    assert "<script" not in body.lower()
    assert "alert(" not in body  # content removed, not just the tag
    assert "fetch(" not in body
    # The legitimate prose survives untouched.
    assert "<p>Portfolio is healthy.</p>" in body


def test_parse_brief_sections_strips_handlers_iframe_and_js_urls() -> None:
    """Inline event handlers, <iframe>/<img>, and javascript: URLs all go."""
    raw = json.dumps(
        {
            "holdings_review": '<p onclick="steal()">NU</p><img src=x onerror=alert(1)>',
            "market_regime": (
                '<iframe src="javascript:alert(1)"></iframe><a href="javascript:alert(2)">click</a>'
            ),
        }
    )
    sections = _parse_brief_sections(raw)
    holdings = sections["holdings_review"]
    regime = sections["market_regime"]
    assert "onclick" not in holdings
    assert "onerror" not in holdings
    assert "<img" not in holdings
    assert "<iframe" not in regime
    assert "javascript:" not in regime
    # <a> is not allowlisted, so the tag is dropped but its text remains.
    assert "click" in regime


def test_parse_brief_sections_preserves_formatting_and_dollars() -> None:
    """Allowlisted formatting + dollar/percent figures pass through verbatim,
    preserving the no-decimal-dollars convention."""
    raw = json.dumps(
        {
            "holdings_review": (
                "<p>NU at <strong>$89,436</strong> is up 13.5%.</p>"
                "<table><tr><td colspan=2>cell</td></tr></table>"
                "<ul><li>one</li><li>two</li></ul>"
            )
        }
    )
    body = _parse_brief_sections(raw)["holdings_review"]
    assert "<strong>$89,436</strong>" in body
    assert "13.5%" in body
    assert "$89,436.00" not in body  # no decimals introduced
    assert "<li>one</li>" in body
    assert 'colspan="2"' in body  # safe table layout attr retained


def test_render_brief_html_emits_no_script_from_sections() -> None:
    """End-to-end: a malicious section cannot reach the rendered document."""
    sections = _parse_brief_sections(
        json.dumps({"executive_summary": "<p>ok</p><script>steal()</script>"})
    )
    html = _render_brief_html("2026-06", _snap(), sections)
    assert "<script>steal()</script>" not in html
    assert "steal()" not in html


def test_render_brief_html_escapes_period() -> None:
    """`period_yyyymm` comes from a request query param, so it is escaped."""
    html = _render_brief_html("<script>evil()</script>", _snap(), {})
    assert "<script>evil()</script>" not in html
    assert "&lt;script&gt;evil()&lt;/script&gt;" in html


def test_brief_passthrough_sets_sandbox_csp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The earnings-summary passthrough serves same-origin HTML, so it must
    sandbox it via CSP to keep any embedded script out of the app origin."""
    from portfolio_tracker.api.routes import earnings_summary as route

    report = tmp_path / "2026-05-09_report.html"
    report.write_text("<!doctype html><html><body><p>hi</p></body></html>", encoding="utf-8")
    monkeypatch.setattr(route.svc, "latest_brief_path", lambda ticker: report)

    resp = route.brief_passthrough("NU")
    assert resp.headers["content-security-policy"] == "sandbox"
    assert resp.media_type == "text/html"
