"""Shared response envelope for the `/api/v1` Portfolio Data Service contract.

Every v1 decision-support response carries a :class:`V1Meta` block so a
consumer can always answer: what data was observed, which provider supplied
it, which accounts were covered, when each source last succeeded, whether the
result is stale or partial, and which deterministic methodology produced it
(`docs/design/portfolio_data_service_prd.md` §7.3).

Warning codes are stable machine-readable strings — consumers may branch on
``code``; ``message`` is for humans and may change wording freely.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Final

from pydantic import BaseModel

# Bump per the compatibility policy: patch = additive doc/field clarification,
# minor = additive fields, major = breaking (consumers must fail closed).
V1_SCHEMA_VERSION: Final[str] = "1.2.0"

# Holdings older than this many calendar days are stale. Five days covers a
# long weekend + one missed refresh; anything older means the daily snapshot
# job has not succeeded recently and consumers must see is_stale=true.
STALE_AFTER_DAYS: Final[int] = 5

# Stable warning codes (append-only; never rename a shipped code).
W_NO_DATA: Final[str] = "NO_DATA"
W_STALE_HOLDINGS: Final[str] = "STALE_HOLDINGS"
W_PARTIAL_COVERAGE: Final[str] = "PARTIAL_COVERAGE"
W_UNKNOWN_TAX_TREATMENT: Final[str] = "UNKNOWN_TAX_TREATMENT"
W_NO_CANONICAL_LINK: Final[str] = "NO_CANONICAL_LINK"
W_CALCULATION_UNAVAILABLE: Final[str] = "CALCULATION_UNAVAILABLE"
W_MIGRATION_STATE_UNKNOWN: Final[str] = "MIGRATION_STATE_UNKNOWN"


class V1Warning(BaseModel):
    """A structured, machine-readable warning attached to a v1 response."""

    code: str
    message: str
    # What the warning is about: an account id, a provider name, a field —
    # rendered as a plain string so the shape stays stable.
    scope: str | None = None


class V1AccountCoverage(BaseModel):
    """Which accounts contributed to this response.

    ``lagging_account_ids`` are included accounts whose own latest holdings
    snapshot is older than the response ``as_of`` — the v1 heuristic for a
    one-provider refresh failure (its rows stop advancing while others move).
    """

    included_account_ids: list[int]
    excluded_account_ids: list[int]
    lagging_account_ids: list[int]


class V1Meta(BaseModel):
    """The shared envelope (PRD §7.3). Attached as ``meta`` on every v1
    decision-support response."""

    schema_version: str
    generated_at: datetime
    # The observation date the payload describes (latest holdings snapshot for
    # position-shaped data). None ⇒ no data.
    as_of: date | None
    currency: str
    source_providers: list[str]
    account_coverage: V1AccountCoverage
    last_successful_sync_at: datetime | None
    is_partial: bool
    is_stale: bool
    warnings: list[V1Warning]
    methodology: str | None = None
    methodology_version: str | None = None
    # Stable relative URLs to related v1 resources.
    links: dict[str, str]


def is_stale(as_of: date | None, *, today: date | None = None) -> bool:
    """True when the observation date is older than the staleness budget.

    ``as_of=None`` (no data) is NOT stale — it is absent, and carries a
    ``NO_DATA`` warning instead; the two states must not be conflated.
    """
    if as_of is None:
        return False
    reference = today if today is not None else datetime.now(UTC).date()
    return (reference - as_of) > timedelta(days=STALE_AFTER_DAYS)


def build_meta(
    *,
    as_of: date | None,
    source_providers: list[str],
    coverage: V1AccountCoverage,
    last_successful_sync_at: datetime | None,
    warnings: list[V1Warning],
    links: dict[str, str],
    currency: str = "USD",
    methodology: str | None = None,
    methodology_version: str | None = None,
    today: date | None = None,
    generated_at: datetime | None = None,
) -> V1Meta:
    """Assemble the envelope, deriving ``is_stale`` / ``is_partial`` and
    appending their standard warnings exactly once.

    ``today`` / ``generated_at`` exist for deterministic tests and fixture
    generation; production callers omit them.
    """
    stale = is_stale(as_of, today=today)
    partial = bool(coverage.lagging_account_ids)
    all_warnings = list(warnings)
    codes = {w.code for w in all_warnings}
    if as_of is None and W_NO_DATA not in codes:
        all_warnings.append(V1Warning(code=W_NO_DATA, message="No holdings snapshot exists yet."))
    if stale and W_STALE_HOLDINGS not in codes:
        all_warnings.append(
            V1Warning(
                code=W_STALE_HOLDINGS,
                message=(
                    f"Latest holdings snapshot is {as_of.isoformat() if as_of else '?'} — "
                    f"older than the {STALE_AFTER_DAYS}-day staleness budget."
                ),
            )
        )
    if partial and W_PARTIAL_COVERAGE not in codes:
        lagging = ", ".join(str(a) for a in coverage.lagging_account_ids)
        all_warnings.append(
            V1Warning(
                code=W_PARTIAL_COVERAGE,
                message=(
                    "Some included accounts have no holdings row at as_of — their "
                    "provider refresh is lagging; totals may understate the book."
                ),
                scope=f"accounts:{lagging}",
            )
        )
    return V1Meta(
        schema_version=V1_SCHEMA_VERSION,
        generated_at=generated_at if generated_at is not None else datetime.now(UTC),
        as_of=as_of,
        currency=currency,
        source_providers=source_providers,
        account_coverage=coverage,
        last_successful_sync_at=last_successful_sync_at,
        is_partial=partial,
        is_stale=stale,
        warnings=all_warnings,
        methodology=methodology,
        methodology_version=methodology_version,
        links=links,
    )
