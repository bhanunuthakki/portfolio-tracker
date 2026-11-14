"""The ad-hoc view trace checker passes a grounded page and fails each kind of drift."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from portfolio_tracker.agent.trace_check import check_page, displayed_numbers, load_body, main

FIXTURES = Path(__file__).resolve().parents[1] / "docs" / "api" / "fixtures" / "v1"
PERFORMANCE: dict[str, Any] = json.loads((FIXTURES / "performance.json").read_text())
POINT = "/series/points/-1"


def performance_page(body: dict[str, Any], *, extra: str = "") -> str:
    """A small performance report rendered only from the response, as WP5 requires."""
    meta, series = body["meta"], body["series"]
    last = series["points"][-1]
    warnings = "".join(f"<li>{w['code']}: {w['message']}</li>" for w in meta["warnings"])
    if series["calculation_status"] == "unavailable":
        result = "Unavailable: " + ", ".join(series["calculation_reason_codes"])
    else:
        result = (
            f"<td>Portfolio return <span data-source='{POINT}/portfolio_return_pct'>"
            f"{float(last['portfolio_return_pct']):.2f}%</span></td>"
            f"<td>SPY <span data-source='{POINT}/spy_return_pct'>"
            f"{float(last['spy_return_pct']):.2f}%</span></td>"
            f"<td>Ending value <span data-source='{POINT}/portfolio_value'>"
            f"${float(last['portfolio_value']):,.2f}</span></td>"
            f"<td>Net external flows ${float(series['net_external_cashflow_in']):,.0f}</td>"
        )
    chart = json.dumps([[p["date"], p["portfolio_return_pct"]] for p in series["points"][-3:]])
    return f"""<!doctype html><html><head><style>.a{{width:12px}}</style>
<script>const width = 640;</script></head><body>
<h1>Performance <span data-trace-ignore>Top 10</span></h1>
<p>{series["start_date"]} to {series["end_date"]} · as of {meta["as_of"]} ·
providers {", ".join(meta["source_providers"])} ·
{len(meta["account_coverage"]["included_account_ids"])} accounts</p>
<table><tr>{result}</tr></table><ul>{warnings}</ul>
<script type="application/json">{chart}</script>{extra}</body></html>"""


def test_grounded_performance_page_passes() -> None:
    report = check_page(performance_page(PERFORMANCE), [PERFORMANCE])

    assert report.ok, report.render()
    assert report.numbers_checked >= 8
    assert report.strict_traces == 3
    assert report.ignored_numbers == ["10"]


def test_fabricated_or_derived_number_fails() -> None:
    derived = float(PERFORMANCE["series"]["points"][-1]["portfolio_return_pct"]) - float(
        PERFORMANCE["series"]["points"][-1]["spy_return_pct"]
    )
    cited = f"<p>Alpha <b data-source='{POINT}/portfolio_return_pct'>{derived:.2f} pp</b></p>"

    report = check_page(performance_page(PERFORMANCE, extra=cited), [PERFORMANCE])

    assert not report.ok
    assert report.untraced_numbers == [f"{derived:.2f} pp (not {POINT}/portfolio_return_pct)"]
    missing = check_page(
        performance_page(PERFORMANCE, extra="<b data-source='/series/alpha'>1.0</b>"), [PERFORMANCE]
    )
    assert missing.untraced_numbers == ["1.0 (data-source /series/alpha not found)"]


def test_loose_trace_rejects_a_precise_fabrication() -> None:
    report = check_page(performance_page(PERFORMANCE, extra="<p>9.9701 pp</p>"), [PERFORMANCE])

    assert report.untraced_numbers == ["9.9701 pp"]


def test_flipped_sign_and_unknown_date_fail() -> None:
    page = performance_page(PERFORMANCE, extra="<p>Flow -$750 on 2031-01-01</p>")

    report = check_page(page, [PERFORMANCE])

    assert report.untraced_numbers == ["-$750"]
    assert report.untraced_dates == ["2031-01-01"]


def test_missing_as_of_and_warning_fail() -> None:
    body = copy.deepcopy(PERFORMANCE)
    body["meta"]["warnings"] = [{"code": "STALE_HOLDINGS", "message": "old", "scope": "portfolio"}]
    body["meta"]["as_of"] = "2026-06-30"
    page = performance_page(PERFORMANCE)

    report = check_page(page, [body])

    assert "2026-06-30 (meta.as_of)" in report.missing_context
    assert "STALE_HOLDINGS (meta.warnings)" in report.missing_context


def test_unavailable_result_must_show_reason_codes() -> None:
    body = copy.deepcopy(PERFORMANCE)
    body["series"]["calculation_status"] = "unavailable"
    body["series"]["calculation_reason_codes"] = ["ending_boundary_incomplete"]

    shown = check_page(performance_page(body), [body])
    hidden = check_page(performance_page(body).replace("ending_boundary_incomplete", "n/a"), [body])

    assert shown.ok, shown.render()
    assert (
        "ending_boundary_incomplete (unavailable calculation_reason_codes)"
        in hidden.missing_context
    )


def test_display_number_parsing() -> None:
    parsed = [
        (n.value, n.places, n.unit) for n in displayed_numbers("($1,234.5) −2% 3.4M 12bps WP1")
    ]

    assert [(str(v), p, u) for v, p, u in parsed] == [
        ("-1234.5", 1, None),
        ("-2", 0, "%"),
        ("3.4", 1, "M"),
        ("12", 0, "bps"),
    ]


def test_cli_reads_wrapped_responses(tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_text(performance_page(PERFORMANCE), encoding="utf-8")
    saved = tmp_path / "performance.json"
    saved.write_text(json.dumps({"request": "/api/v1/analytics/performance", "body": PERFORMANCE}))

    assert load_body(saved) == PERFORMANCE
    assert main([str(page), str(saved)]) == 0
    named = performance_page(PERFORMANCE).replace(f"'{POINT}", f"'performance:{POINT}")
    page.write_text(named, encoding="utf-8")
    assert main([str(page), str(saved), str(FIXTURES / "risk.json")]) == 0
    page.write_text(performance_page(PERFORMANCE, extra="<b>99.99%</b>"), encoding="utf-8")
    assert main([str(page), str(saved)]) == 1
