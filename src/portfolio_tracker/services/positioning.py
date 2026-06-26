"""Portfolio positioning — the cuts behind the Holdings positioning section.

Answers "how is the book actually positioned?" along five axes:

  * **Asset type**   stock / ETF / mutual fund / crypto / cash / other,
                     from the broker `type` code + the cash-equivalent flag.
  * **Sector**       GICS-style sector, with funds bucketed as `ETF/Fund`
                     (they have no single sector) — from `security_classifications`.
  * **Region**       US / International / Unknown, from the classified country.
  * **Account type** taxable vs retirement / tax-advantaged, from the account
                     subtype (with a name fallback for feeds that omit it).
  * **Concentration** top-N weight, position count, HHI + effective holdings.

Plus a per-ticker **correlation / beta** table vs SPY, QQQ, and the policy mix,
reusing the OLS routine from `services.beta`.

This module is split deliberately: the pure helpers below (classification,
concentration, correlation) carry the testable logic and know nothing about
the database; `compute_positioning` is the thin DB-facing assembler.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import (
    Account,
    HoldingSnapshot,
    PolicyWeight,
    Price,
    Security,
    SecurityClassification,
)
from portfolio_tracker.schemas import (
    ConcentrationOut,
    PositionCorrelationRow,
    PositioningBucketOut,
    PositioningOut,
)
from portfolio_tracker.services.active_items import active_account_ids
from portfolio_tracker.services.beta import (
    _benchmark_daily_returns,  # pyright: ignore[reportPrivateUsage]
    _benchmark_daily_returns_for,  # pyright: ignore[reportPrivateUsage]
    _ols,  # pyright: ignore[reportPrivateUsage]
)


class AssetType(StrEnum):
    """Coarse instrument class. Values double as display labels."""

    STOCK = "Stock"
    ETF = "ETF"
    MUTUAL_FUND = "Mutual fund"
    CRYPTO = "Crypto"
    CASH = "Cash"
    OTHER = "Other"


class TaxTreatment(StrEnum):
    """Tax location of an account. Values double as display labels.

    `TAX_ADVANTAGED` deliberately covers retirement *and* adjacent
    tax-sheltered accounts (HSA, 529) — the meaningful positioning split is
    "taxable vs sheltered", and a lone HSA slice adds noise without insight.
    """

    TAXABLE = "Taxable"
    TAX_ADVANTAGED = "Retirement / tax-advantaged"
    UNKNOWN = "Unknown"


# Broker `type` codes → asset class. Plaid emits words (`equity`, `etf`,
# `cash`, `cryptocurrency`); SnapTrade emits two-letter codes (`cs`=common
# stock, `et`=ETF, `ad`=ADR, `oef`=open-end fund). Unknown codes fall to
# OTHER — a legitimate bucket, not a swallowed error.
_ASSET_TYPE_BY_CODE: dict[str, AssetType] = {
    "cs": AssetType.STOCK,
    "stock": AssetType.STOCK,
    "equity": AssetType.STOCK,
    "common stock": AssetType.STOCK,
    "preferred stock": AssetType.STOCK,
    "ps": AssetType.STOCK,
    "ad": AssetType.STOCK,  # ADR — an equity; its foreign domicile shows in the region cut
    "adr": AssetType.STOCK,
    "et": AssetType.ETF,
    "etf": AssetType.ETF,
    "etn": AssetType.ETF,
    "oef": AssetType.MUTUAL_FUND,
    "cef": AssetType.MUTUAL_FUND,
    "mutual fund": AssetType.MUTUAL_FUND,
    "mutualfund": AssetType.MUTUAL_FUND,
    "mf": AssetType.MUTUAL_FUND,
    "fund": AssetType.MUTUAL_FUND,
    "cryptocurrency": AssetType.CRYPTO,
    "crypto": AssetType.CRYPTO,
    "cash": AssetType.CASH,
}


def classify_asset_type(security_type: str | None, is_cash_equivalent: bool) -> AssetType:
    """Map a broker `type` code (+ cash-equivalent flag) to an `AssetType`.

    The cash-equivalent flag wins: money-market funds (FDRXX, SPAXX) arrive
    typed `oef` but are economically cash.
    """
    if is_cash_equivalent:
        return AssetType.CASH
    if security_type is None:
        return AssetType.OTHER
    return _ASSET_TYPE_BY_CODE.get(security_type.strip().lower(), AssetType.OTHER)


# Structured account-subtype tokens (Plaid / SnapTrade `subtype`).
_RETIREMENT_SUBTYPES: frozenset[str] = frozenset(
    {
        "ira",
        "roth_ira",
        "roth ira",
        "traditional_ira",
        "traditional ira",
        "roth",
        "roth_401k",
        "roth 401k",
        "401k",
        "401(k)",
        "401a",
        "403b",
        "403(b)",
        "457b",
        "457",
        "457(b)",
        "sep_ira",
        "sep ira",
        "sep",
        "simple_ira",
        "simple ira",
        "simple",
        "rollover",
        "rollover ira",
        "pension",
        "keogh",
        "tsp",
        "sarsep",
        "hsa",
        "529",
        "coverdell",
        "esa",
    }
)
_TAXABLE_SUBTYPES: frozenset[str] = frozenset(
    {"individual", "brokerage", "taxable", "joint", "cash management", "custodial", "ugma", "utma"}
)
# Name fallback — used ONLY when the structured subtype/type doesn't resolve.
# Some feeds (Fidelity via SnapTrade) omit subtype entirely, leaving the
# account name as the sole signal. This mirrors the sanctioned name-based
# fallback in `services/performance.py:_classify_by_name`. Matched as whole
# words (so "ira" doesn't fire inside "Miraflores"), plus a couple of compound
# phrases checked as substrings.
_RETIREMENT_NAME_WORDS: frozenset[str] = frozenset(
    {
        "roth",
        "ira",
        "sep",
        "simple",
        "pension",
        "rollover",
        "hsa",
        "retirement",
        "401k",
        "403b",
        "457b",
        "401",
        "403",
        "457",
        "keogh",
        "tsp",
    }
)
_RETIREMENT_NAME_PHRASES: tuple[str, ...] = ("health savings", "brokeragelink")
_TAXABLE_NAME_WORDS: frozenset[str] = frozenset({"individual", "taxable", "joint"})


def classify_tax_treatment(
    subtype: str | None, account_type: str | None, name: str | None
) -> TaxTreatment:
    """Classify an account's tax location.

    Order: structured subtype → account type (`brokerage` ⇒ taxable) → an
    explicit name-token fallback → `UNKNOWN`. Unknown is the honest default;
    nothing is force-bucketed on a guess.
    """
    if subtype is not None:
        s = subtype.strip().lower()
        if s in _RETIREMENT_SUBTYPES:
            return TaxTreatment.TAX_ADVANTAGED
        if s in _TAXABLE_SUBTYPES:
            return TaxTreatment.TAXABLE
    if account_type is not None and account_type.strip().lower() == "brokerage":
        return TaxTreatment.TAXABLE
    if name:
        lowered = name.lower()
        words = set(re.findall(r"[a-z0-9]+", lowered))
        if words & _RETIREMENT_NAME_WORDS or any(p in lowered for p in _RETIREMENT_NAME_PHRASES):
            return TaxTreatment.TAX_ADVANTAGED
        if words & _TAXABLE_NAME_WORDS:
            return TaxTreatment.TAXABLE
    return TaxTreatment.UNKNOWN


_US_COUNTRY_NAMES: frozenset[str] = frozenset(
    {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}
)


def country_to_region(country: str | None) -> str:
    """Collapse a country name to the coarse region cut (`US` / `International`).

    Returns `Unknown` for a missing country so the position still lands in a
    bucket rather than being dropped.
    """
    if not country or not country.strip():
        return "Unknown"
    return "US" if country.strip().lower() in _US_COUNTRY_NAMES else "International"


@dataclass(frozen=True)
class Concentration:
    """Concentration summary for a set of position market values."""

    num_positions: int
    top1_weight_pct: Decimal | None
    top5_weight_pct: Decimal | None
    top10_weight_pct: Decimal | None
    # Herfindahl-Hirschman Index on the 0-10000 scale (sum of squared percent
    # weights): 10000 = one position, falling toward 0 as holdings spread out.
    hhi: float | None
    # Effective number of holdings = 1 / sum(weight_fraction^2). The intuitive
    # read of HHI: "this book behaves like ~N equally-weighted positions."
    effective_holdings: float | None


def concentration_metrics(values: list[Decimal]) -> Concentration:
    """Top-N weights, HHI, and effective holdings from raw position values.

    Non-positive values are ignored. An empty / all-zero input yields a
    zero-count summary with `None` metrics (no division by zero).
    """
    positives = sorted((v for v in values if v > 0), reverse=True)
    total = sum(positives, Decimal(0))
    if not positives or total <= 0:
        return Concentration(
            num_positions=len(positives),
            top1_weight_pct=None,
            top5_weight_pct=None,
            top10_weight_pct=None,
            hhi=None,
            effective_holdings=None,
        )

    def top_n_pct(n: int) -> Decimal:
        return sum(positives[:n], Decimal(0)) / total * Decimal(100)

    fractions = [v / total for v in positives]
    sum_sq = sum((f * f for f in fractions), Decimal(0))
    return Concentration(
        num_positions=len(positives),
        top1_weight_pct=top_n_pct(1),
        top5_weight_pct=top_n_pct(5),
        top10_weight_pct=top_n_pct(10),
        hhi=float(sum_sq) * 10000.0,
        effective_holdings=(1.0 / float(sum_sq)) if sum_sq > 0 else None,
    )


def correlation_beta(
    security_returns: dict[date, Decimal],
    benchmark_returns: dict[date, Decimal],
) -> tuple[float | None, float | None, int]:
    """(correlation, beta, sample_size) of a security's daily returns vs a benchmark.

    Pairs on common dates only. We apply NO move bound here — these come
    straight from the `prices` table, so a 30%+ earnings move is real signal.
    (The portfolio-level beta service keeps real moves too, excluding only
    >50% single-day swings as suspected price-data errors.) Returns
    `(None, None, n)` when there are fewer than two paired observations or the
    benchmark has no variance.
    """
    common = sorted(set(security_returns) & set(benchmark_returns))
    if len(common) < 2:
        return (None, None, len(common))
    p = [float(security_returns[d]) for d in common]
    m = [float(benchmark_returns[d]) for d in common]
    beta, _alpha, _r_squared, correlation = _ols(p, m)
    return (correlation, beta, len(common))


# ---- DB-facing assembly --------------------------------------------------


def _sector_bucket(asset_type: AssetType, sector: str | None) -> str:
    """Which sector slice a position falls in.

    Cash and crypto are their own slices; funds with no assigned sector fall
    to `ETF/Fund`; a stock with no classification is `Unclassified`. Every
    position thus lands somewhere and the slices sum to the whole book.
    """
    if asset_type is AssetType.CASH:
        return "Cash"
    if asset_type is AssetType.CRYPTO:
        return "Crypto"
    if sector:
        return sector
    if asset_type in (AssetType.ETF, AssetType.MUTUAL_FUND):
        return "ETF/Fund"
    return "Unclassified"


def _region_bucket(asset_type: AssetType, region: str | None) -> str:
    if asset_type is AssetType.CASH:
        return "Cash"
    return region or "Unknown"


def _aggregate_buckets(
    labeled_values: list[tuple[str, Decimal]], total: Decimal
) -> list[PositioningBucketOut]:
    """Sum (label, value) pairs into weighted buckets, largest slice first."""
    acc: dict[str, tuple[Decimal, int]] = {}
    for label, value in labeled_values:
        prior_value, prior_count = acc.get(label, (Decimal(0), 0))
        acc[label] = (prior_value + value, prior_count + 1)
    buckets = [
        PositioningBucketOut(
            label=label,
            value=value,
            weight_pct=(value / total * Decimal(100)) if total > 0 else Decimal(0),
            count=count,
        )
        for label, (value, count) in acc.items()
    ]
    buckets.sort(key=lambda b: (-b.value, b.label))
    return buckets


def _concentration_out(c: Concentration) -> ConcentrationOut:
    return ConcentrationOut(
        num_positions=c.num_positions,
        top1_weight_pct=c.top1_weight_pct,
        top5_weight_pct=c.top5_weight_pct,
        top10_weight_pct=c.top10_weight_pct,
        hhi=c.hhi,
        effective_holdings=c.effective_holdings,
    )


def _latest_active_holdings(
    session: Session,
) -> tuple[list[tuple[HoldingSnapshot, Account, Security]], date | None]:
    """Latest-snapshot holdings for active accounts (mirrors the holdings route)."""
    accts = active_account_ids(session)
    if not accts:
        return [], None
    latest = session.execute(
        select(HoldingSnapshot.snapshot_date)
        .where(HoldingSnapshot.account_id.in_(accts))
        .order_by(HoldingSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return [], None
    rows = session.execute(
        select(HoldingSnapshot, Account, Security)
        .join(Account, Account.account_id == HoldingSnapshot.account_id)
        .join(Security, Security.security_id == HoldingSnapshot.security_id)
        .where(HoldingSnapshot.snapshot_date == latest)
        .where(HoldingSnapshot.account_id.in_(accts))
    ).all()
    return [(h, a, s) for h, a, s in rows], latest


def _build_correlations(
    session: Session,
    value_by_security: dict[int, Decimal],
    security_by_id: dict[int, Security],
    total: Decimal,
    start_date: date,
    end_date: date,
    has_policy: bool,
) -> tuple[list[PositionCorrelationRow], float | None, int]:
    """Per-(non-cash)-security correlation + beta to SPY/QQQ/policy.

    Returns the rows, the book value-weighted average SPY correlation across
    the names that have one, and the count of positions with no usable price
    history.
    """
    spy_returns = _benchmark_daily_returns_for(session, "SPY", start_date, end_date)
    qqq_returns = _benchmark_daily_returns_for(session, "QQQ", start_date, end_date)
    policy_returns = (
        _benchmark_daily_returns_for(session, "POLICY", start_date, end_date) if has_policy else {}
    )

    security_ids = list(value_by_security.keys())
    closes_by_security: dict[int, dict[date, Decimal]] = defaultdict(dict)
    if security_ids:
        price_rows = (
            session.execute(
                select(Price)
                .where(Price.security_id.in_(security_ids))
                .where(Price.date >= start_date)
                .where(Price.date <= end_date)
            )
            .scalars()
            .all()
        )
        for p in price_rows:
            closes_by_security[p.security_id][p.date] = p.close

    rows: list[PositionCorrelationRow] = []
    no_price_count = 0
    weighted_corr_sum = 0.0
    included_weight = 0.0
    for security_id, value in value_by_security.items():
        security = security_by_id[security_id]
        asset_type = classify_asset_type(security.type, security.is_cash_equivalent)
        if asset_type is AssetType.CASH:
            continue
        security_returns = _benchmark_daily_returns(closes_by_security.get(security_id, {}))
        corr_spy, beta_spy, sample = correlation_beta(security_returns, spy_returns)
        corr_qqq, beta_qqq, _ = correlation_beta(security_returns, qqq_returns)
        corr_policy, beta_policy, _ = (
            correlation_beta(security_returns, policy_returns) if has_policy else (None, None, 0)
        )
        if sample == 0:
            no_price_count += 1
        weight = (value / total * Decimal(100)) if total > 0 else Decimal(0)
        if corr_spy is not None and total > 0:
            weight_fraction = float(value / total)
            weighted_corr_sum += weight_fraction * corr_spy
            included_weight += weight_fraction
        rows.append(
            PositionCorrelationRow(
                security_id=security_id,
                ticker=security.ticker,
                name=security.name,
                value=value,
                weight_pct=weight,
                sample_size=sample,
                correlation_spy=corr_spy,
                beta_spy=beta_spy,
                correlation_qqq=corr_qqq,
                beta_qqq=beta_qqq,
                correlation_policy=corr_policy,
                beta_policy=beta_policy,
            )
        )
    rows.sort(key=lambda r: (-r.value, r.ticker or ""))
    weighted_avg = (weighted_corr_sum / included_weight) if included_weight > 0 else None
    return rows, weighted_avg, no_price_count


def _positioning_notes(
    unknown_accounts: set[str], no_price_count: int, has_policy: bool
) -> list[str]:
    notes = [
        "Funds/ETFs are shown as their own 'ETF/Fund' sector bucket (no single "
        "GICS sector); cash is excluded from the correlation table.",
    ]
    if unknown_accounts:
        notes.append(
            f"{len(unknown_accounts)} account(s) couldn't be classified by tax "
            f"treatment: {', '.join(sorted(unknown_accounts))}."
        )
    if no_price_count:
        notes.append(
            f"{no_price_count} position(s) lack enough price history in the window "
            f"for a correlation estimate."
        )
    if not has_policy:
        notes.append("No policy weights set — the policy correlation column is empty.")
    return notes


def compute_positioning(session: Session, start_date: date, end_date: date) -> PositioningOut:
    """Assemble every positioning cut from the latest active-account snapshot.

    Breakdowns are value-weighted over current market value; the correlation
    window is [start_date, end_date]. Returns an empty-but-valid payload when
    there are no active holdings.
    """
    rows, snapshot_date = _latest_active_holdings(session)
    has_policy = session.execute(select(PolicyWeight.ticker).limit(1)).first() is not None
    if not rows or snapshot_date is None:
        return PositioningOut(
            snapshot_date=end_date,
            start_date=start_date,
            end_date=end_date,
            total_value=Decimal(0),
            by_asset_type=[],
            by_sector=[],
            by_region=[],
            by_account_type=[],
            concentration=_concentration_out(concentration_metrics([])),
            correlations=[],
            weighted_avg_correlation_spy=None,
            has_policy=has_policy,
            notes=["No active holdings to position yet."],
        )

    classifications = {
        c.security_id: c for c in session.execute(select(SecurityClassification)).scalars().all()
    }
    total = sum((h.institution_value or Decimal(0) for h, _, _ in rows), Decimal(0))

    asset_items: list[tuple[str, Decimal]] = []
    sector_items: list[tuple[str, Decimal]] = []
    region_items: list[tuple[str, Decimal]] = []
    account_items: list[tuple[str, Decimal]] = []
    value_by_security: dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    security_by_id: dict[int, Security] = {}
    unknown_accounts: set[str] = set()

    for holding, account, security in rows:
        value = holding.institution_value or Decimal(0)
        asset_type = classify_asset_type(security.type, security.is_cash_equivalent)
        classification = classifications.get(security.security_id)
        sector = classification.sector if classification else None
        region = classification.region if classification else None
        treatment = classify_tax_treatment(account.subtype, account.type, account.name)

        asset_items.append((asset_type.value, value))
        sector_items.append((_sector_bucket(asset_type, sector), value))
        region_items.append((_region_bucket(asset_type, region), value))
        account_items.append((treatment.value, value))
        if treatment is TaxTreatment.UNKNOWN:
            unknown_accounts.add(account.name)
        value_by_security[security.security_id] += value
        security_by_id[security.security_id] = security

    correlations, weighted_avg_corr_spy, no_price_count = _build_correlations(
        session, dict(value_by_security), security_by_id, total, start_date, end_date, has_policy
    )

    return PositioningOut(
        snapshot_date=snapshot_date,
        start_date=start_date,
        end_date=end_date,
        total_value=total,
        by_asset_type=_aggregate_buckets(asset_items, total),
        by_sector=_aggregate_buckets(sector_items, total),
        by_region=_aggregate_buckets(region_items, total),
        by_account_type=_aggregate_buckets(account_items, total),
        concentration=_concentration_out(concentration_metrics(list(value_by_security.values()))),
        correlations=correlations,
        weighted_avg_correlation_spy=weighted_avg_corr_spy,
        has_policy=has_policy,
        notes=_positioning_notes(unknown_accounts, no_price_count, has_policy),
    )
