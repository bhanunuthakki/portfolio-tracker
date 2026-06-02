"""Enrich securities with sector + region for the positioning view.

Pulls `sector` + `country` from yfinance `.info` for individual equities and
caches them in `security_classifications` as `source='auto'`. Funds/ETFs are
stamped `ETF/Fund` (no single GICS sector) and crypto `Crypto` without a
fetch. Cash equivalents and derivatives are skipped — they don't carry a
meaningful sector/region.

The job NEVER overwrites a `source='manual'` row, so a user correction
(e.g. tagging VXUS as International) survives every refresh.

Run manually (re-fetches all auto rows, picking up sector reclassifications):
    python -m portfolio_tracker.jobs.classify_securities

`daily_refresh` calls it with `only_missing=True` so the daily path only hits
yfinance for securities it has never classified — a no-op on a steady book.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import typer
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.db import SessionLocal
from portfolio_tracker.models import Security, SecurityClassification, TickerOverride
from portfolio_tracker.services.positioning import (
    AssetType,
    classify_asset_type,
    country_to_region,
)

# Type of the injectable fetcher — keeps `classify_securities` testable offline.
FetchInfo = Callable[[str], Mapping[str, object]]


def _fetch_info(ticker: str) -> Mapping[str, object]:
    """yfinance `.info` for a ticker, or an empty mapping on any failure.

    yfinance raises a grab-bag of exceptions (network, JSON, delisting); a
    missing classification is non-fatal, so we swallow and report `no_data`.
    """
    try:
        info = yf.Ticker(ticker).info
    except Exception:
        return {}
    return info if isinstance(info, dict) else {}


def _classification_for(
    asset_type: AssetType, info: Mapping[str, object]
) -> tuple[str | None, str | None]:
    """Map (asset type, yfinance info) to a (sector, region) pair.

    Funds and crypto are bucketed without trusting yfinance (a US-domiciled
    international fund reports country 'United States'). Stocks take their
    yfinance sector + country-derived region. Everything else gets nothing.
    """
    if asset_type in (AssetType.ETF, AssetType.MUTUAL_FUND):
        return ("ETF/Fund", None)
    if asset_type is AssetType.CRYPTO:
        return ("Crypto", None)
    if asset_type is AssetType.STOCK:
        raw_sector = info.get("sector")
        sector = raw_sector.strip() if isinstance(raw_sector, str) and raw_sector.strip() else None
        raw_country = info.get("country")
        country = raw_country if isinstance(raw_country, str) else None
        return (sector, country_to_region(country))
    return (None, None)


def classify_securities(
    session: Session, fetch_info: FetchInfo = _fetch_info, only_missing: bool = False
) -> dict[str, int]:
    """Classify every security in the DB. Mutates the session; caller commits.

    `only_missing=True` skips securities that already have any row (the daily
    path); the default re-fetches `auto` rows so a manual run picks up sector
    reclassifications. Returns per-outcome counts.
    """
    counts = {"classified": 0, "skipped_manual": 0, "no_data": 0}
    existing = {
        c.security_id: c for c in session.execute(select(SecurityClassification)).scalars().all()
    }
    ticker_overrides = {
        ov.security_id: ov.ticker for ov in session.execute(select(TickerOverride)).scalars().all()
    }
    for security in session.execute(select(Security)).scalars().all():
        row = existing.get(security.security_id)
        if row is not None:
            if row.source == "manual":
                counts["skipped_manual"] += 1
                continue
            if only_missing:
                continue

        asset_type = classify_asset_type(security.type, security.is_cash_equivalent)
        if asset_type in (AssetType.CASH, AssetType.OTHER):
            continue

        info: Mapping[str, object] = {}
        if asset_type is AssetType.STOCK:
            ticker = ticker_overrides.get(security.security_id) or security.ticker
            if ticker is None:
                counts["no_data"] += 1
                continue
            info = fetch_info(ticker)

        sector, region = _classification_for(asset_type, info)
        if sector is None and region in (None, "Unknown"):
            # Nothing worth persisting — region falls back to "Unknown" on read.
            counts["no_data"] += 1
            continue

        if row is None:
            session.add(
                SecurityClassification(
                    security_id=security.security_id,
                    sector=sector,
                    region=region,
                    source="auto",
                )
            )
        else:
            row.sector = sector
            row.region = region
            row.source = "auto"
        counts["classified"] += 1
    return counts


def run(only_missing: bool = False) -> dict[str, int]:
    """Run the enrichment against the configured DB and commit."""
    with SessionLocal() as session:
        counts = classify_securities(session, only_missing=only_missing)
        session.commit()
    return counts


def main(
    only_missing: bool = typer.Option(
        False, help="Only classify securities with no existing row (skip refetch)."
    ),
) -> None:
    counts = run(only_missing=only_missing)
    print(
        f"classify_securities complete: classified={counts['classified']} "
        f"skipped_manual={counts['skipped_manual']} no_data={counts['no_data']}"
    )


if __name__ == "__main__":
    typer.run(main)
