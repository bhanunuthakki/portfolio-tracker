"""Trace checker for ad-hoc views rendered from saved `/api/v1` responses.

A rendered page passes when:

1. Every number it shows equals a number in one of the saved responses, at the
   displayed precision. Allowed display scales: `%`/`pp` (value or value x 100),
   `bp`/`bps` (x 100 or x 10 000), `K`/`M`/`B` suffixes. A count may equal the
   length of a returned list. The sign must match.
2. Every ISO date or timestamp it shows appears in a saved response.
3. For each saved response with a `meta` block: `meta.as_of` (or `NO_DATA` when it
   is null), each source provider, and each warning code appear on the page.
4. For each top-level result with `calculation_status=unavailable`, its reason codes
   appear on the page.

An element may cite the field it shows with `data-source="<name>:<pointer>"`, where
`<name>` is the saved response's file stem (optional with one response) and
`<pointer>` is a JSON pointer such as `/series/points/-1/portfolio_return_pct`
(`-1` is the last item). Numbers inside it must equal that field: a strict trace.
Other numbers only need to equal some returned value: a loose trace, which can match
by chance in a long series. The report counts both.

Numbers inside an element marked `data-trace-ignore` (labels such as "Top 10") are
skipped and listed in the report. Chart data embedded as
`<script type="application/json">` is checked like visible text; other scripts and
styles are not read.

    python -m portfolio_tracker.agent.trace_check page.html saved/*.json

A saved response file holds either the raw response body, or
`{"request": "/api/v1/...", "body": {...}}`.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast

_DATE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
_NUMBER_RE = re.compile(
    r"(?<![\w.])(?P<open>\()?(?P<sign>[-+−])?\s?\$?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<frac>\d+))?"
    r"(?:\s?(?P<unit>%|pp\b|bps\b|bp\b|[KMB]\b|x\b))?(?P<close>\))?"
)
_NUMERIC_STRING_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
_SKIP_TAGS = frozenset({"style", "template", "noscript"})
_TEXT_ATTRS = ("title", "aria-label", "data-value", "alt")
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_SCALES: dict[str | None, tuple[Decimal, ...]] = {
    None: (Decimal(1),),
    "x": (Decimal(1),),
    "%": (Decimal(1), Decimal(100)),
    "pp": (Decimal(1), Decimal(100)),
    "bp": (Decimal(100), Decimal(10000)),
    "bps": (Decimal(100), Decimal(10000)),
    "K": (Decimal("0.001"),),
    "M": (Decimal("0.000001"),),
    "B": (Decimal("0.000000001"),),
}


@dataclass
class PageText:
    checked: list[tuple[str, str | None]] = field(
        default_factory=list[tuple[str, str | None]]
    )  # (text, data-source)
    ignored: list[str] = field(default_factory=list[str])


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = PageText()
        self._stack: list[tuple[str, bool, str, str | None]] = []  # tag, ignored, mode, source

    def _ignored(self) -> bool:
        return bool(self._stack) and self._stack[-1][1]

    def _mode(self) -> str:
        return self._stack[-1][2] if self._stack else "text"

    def _source(self) -> str | None:
        return self._stack[-1][3] if self._stack else None

    def _emit(self, text: str, source: str | None, *, ignored: bool) -> None:
        if ignored:
            self.page.ignored.append(text)
        else:
            self.page.checked.append((text, source))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        ignored = self._ignored() or "data-trace-ignore" in attr
        mode = self._mode()
        source = attr.get("data-source") or self._source()
        if tag in _SKIP_TAGS:
            mode = "skip"
        elif tag == "script":
            mode = "json" if (attr.get("type") or "").lower() == "application/json" else "skip"
        if mode != "skip":
            for key in _TEXT_ATTRS:
                if value := attr.get(key):
                    self._emit(value, source, ignored=ignored)
        if tag not in _VOID_TAGS:
            self._stack.append((tag, ignored, mode, source))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS and self._stack and self._stack[-1][0] == tag:
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        mode = self._mode()
        if mode == "skip" or not data.strip():
            return
        if mode == "json":
            try:
                parsed = json.loads(data)
            except ValueError:
                self.page.checked.append(("<invalid embedded JSON>", None))
                return
            data = " ".join(_json_scalar_text(parsed))
        self._emit(data, self._source(), ignored=self._ignored())


def _json_scalar_text(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for item in cast(dict[str, Any], value).values():
            yield from _json_scalar_text(item)
    elif isinstance(value, list):
        for item in cast(list[Any], value):
            yield from _json_scalar_text(item)
    elif isinstance(value, bool) or value is None:
        return
    else:
        yield str(value)


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for item in cast(dict[str, Any], value).values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in cast(list[Any], value):
            yield from _walk(item)


@dataclass
class Evidence:
    numbers: set[Decimal]
    strings: set[str]
    dates: set[str]


def collect_evidence(bodies: Iterable[Any]) -> Evidence:
    numbers: set[Decimal] = set()
    strings: set[str] = set()
    dates: set[str] = set()
    for body in bodies:
        for node in _walk(body):
            if isinstance(node, bool) or node is None:
                continue
            if isinstance(node, int | float):
                numbers.add(Decimal(str(node)))
            elif isinstance(node, str):
                strings.add(node)
                dates.update(_DATE_RE.findall(node))
                if _NUMERIC_STRING_RE.match(node):
                    numbers.add(Decimal(node))
            elif isinstance(node, list):
                numbers.add(Decimal(len(cast(list[Any], node))))
    return Evidence(numbers=numbers, strings=strings, dates=dates)


@dataclass(frozen=True)
class DisplayedNumber:
    text: str
    value: Decimal
    places: int
    unit: str | None


def displayed_numbers(text: str) -> Iterator[DisplayedNumber]:
    for match in _NUMBER_RE.finditer(text):
        whole = match.group("int").replace(",", "")
        frac = match.group("frac") or ""
        value = Decimal(f"{whole}.{frac}" if frac else whole)
        negative = match.group("sign") in {"-", "−"}
        if match.group("open") and match.group("close"):
            negative = True
        yield DisplayedNumber(
            text=match.group(0).strip(),
            value=-value if negative else value,
            places=len(frac),
            unit=match.group("unit"),
        )


def _traces(number: DisplayedNumber, evidence: set[Decimal]) -> bool:
    quantum = Decimal(1).scaleb(-number.places)
    for scale in _SCALES[number.unit]:
        for candidate in evidence:
            scaled = candidate * scale
            for rounding in (ROUND_HALF_UP, ROUND_HALF_EVEN):
                try:
                    if scaled.quantize(quantum, rounding=rounding) == number.value:
                        return True
                except InvalidOperation:
                    continue
    return False


def resolve_source(source: str, bodies: dict[str, Any]) -> Any:
    """Resolve `name:/json/pointer` (name optional with one body). KeyError if absent."""
    name, _, pointer = source.rpartition(":")
    if not name:
        if len(bodies) != 1:
            raise KeyError(source)
        node = next(iter(bodies.values()))
    else:
        node = bodies[name]
    for token in pointer.split("/")[1:] if pointer else []:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            items = cast(list[Any], node)
            node = items[int(token)]
        elif isinstance(node, dict):
            node = cast(dict[str, Any], node)[token]
        else:
            raise KeyError(source)
    return node


@dataclass
class TraceReport:
    numbers_checked: int = 0
    strict_traces: int = 0
    untraced_numbers: list[str] = field(default_factory=list[str])
    untraced_dates: list[str] = field(default_factory=list[str])
    missing_context: list[str] = field(default_factory=list[str])
    ignored_numbers: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        return not (self.untraced_numbers or self.untraced_dates or self.missing_context)

    def render(self) -> str:
        lines = [
            f"trace check: {'PASS' if self.ok else 'FAIL'}",
            f"numbers checked: {self.numbers_checked} "
            f"({self.strict_traces} strict via data-source, "
            f"{self.numbers_checked - self.strict_traces} loose)",
        ]
        for label, items in (
            ("untraced numbers", self.untraced_numbers),
            ("untraced dates", self.untraced_dates),
            ("missing required context", self.missing_context),
            ("ignored (data-trace-ignore)", self.ignored_numbers),
        ):
            if items:
                lines.append(f"{label}: {len(items)}")
                lines.extend(f"  - {item}" for item in items)
        return "\n".join(lines)


def _required_context(body: Any) -> Iterator[tuple[str, str]]:
    """(required page text, why) pairs for one response body."""
    if not isinstance(body, dict):
        return
    root = cast(dict[str, Any], body)
    meta = root.get("meta")
    if isinstance(meta, dict):
        meta_d = cast(dict[str, Any], meta)
        as_of = meta_d.get("as_of")
        if as_of:
            yield str(as_of), "meta.as_of"
        else:
            yield "NO_DATA", "meta.as_of is null"
        for provider in cast(list[Any], meta_d.get("source_providers") or []):
            yield str(provider), "meta.source_providers"
        for warning in cast(list[Any], meta_d.get("warnings") or []):
            if isinstance(warning, dict) and (code := cast(dict[str, Any], warning).get("code")):
                yield str(code), "meta.warnings"
    results: list[dict[str, Any]] = [root]
    results.extend(
        cast(dict[str, Any], v) for k, v in root.items() if k != "meta" and isinstance(v, dict)
    )
    for result_d in results:
        if result_d.get("calculation_status") != "unavailable":
            continue
        for key, value in result_d.items():
            if key.endswith("reason_codes"):
                for code in cast(list[Any], value or []):
                    yield str(code), f"unavailable {key}"
            elif key.endswith("reason_code") and value:
                yield str(value), f"unavailable {key}"


def check_page(html: str, bodies: dict[str, Any] | list[Any]) -> TraceReport:
    named = bodies if isinstance(bodies, dict) else {str(i): b for i, b in enumerate(bodies)}
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    evidence = collect_evidence(named.values())
    report = TraceReport()
    checked_text = "\n".join(text for text, _source in parser.page.checked)

    for chunk, source in parser.page.checked:
        for date_text in _DATE_RE.findall(chunk):
            if date_text not in evidence.dates and date_text[:10] not in evidence.dates:
                report.untraced_dates.append(date_text)
        numbers = list(displayed_numbers(_DATE_RE.sub(" ", chunk)))
        report.numbers_checked += len(numbers)
        if source is None:
            report.untraced_numbers.extend(
                n.text for n in numbers if not _traces(n, evidence.numbers)
            )
            continue
        try:
            cited = collect_evidence([resolve_source(source, named)]).numbers
        except (KeyError, IndexError, ValueError):
            report.untraced_numbers.extend(
                f"{n.text} (data-source {source} not found)" for n in numbers
            )
            continue
        for number in numbers:
            if _traces(number, cited):
                report.strict_traces += 1
            else:
                report.untraced_numbers.append(f"{number.text} (not {source})")
    for chunk in parser.page.ignored:
        report.ignored_numbers.extend(n.text for n in displayed_numbers(_DATE_RE.sub(" ", chunk)))

    lowered = checked_text.lower()
    for body in named.values():
        for needed, why in _required_context(body):
            if needed.lower() not in lowered:
                report.missing_context.append(f"{needed} ({why})")
    report.missing_context = sorted(set(report.missing_context))
    return report


def load_body(path: Path) -> Any:
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        wrapped = cast(dict[str, Any], data)
        if {"request", "body"} <= set(wrapped):
            return wrapped["body"]
        return wrapped
    return data


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        sys.stderr.write(
            "usage: python -m portfolio_tracker.agent.trace_check PAGE.html RESPONSE.json...\n"
        )
        return 2
    page = Path(args[0]).read_text(encoding="utf-8")
    report = check_page(page, {Path(p).stem: load_body(Path(p)) for p in args[1:]})
    sys.stdout.write(report.render() + "\n")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
