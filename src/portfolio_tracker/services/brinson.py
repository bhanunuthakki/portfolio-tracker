"""Brinson-Fachler sector attribution vs the S&P 500 (SPY).

Decomposes the portfolio's active return — how it did relative to the index —
into three sector-level effects:

  * **Allocation**  did over/under-weighting a sector (vs the index) help, given
                    how that sector did vs the index overall?
                    allocation = Σ_s (w_p,s − w_b,s)(r_b,s − r_b)
  * **Selection**   within a sector, did the specific names beat that sector's
                    benchmark?   selection = Σ_s w_b,s (r_p,s − r_b,s)
  * **Interaction** the cross term (over-weighting a sector where you also
                    picked well):  interaction = Σ_s (w_p,s − w_b,s)(r_p,s − r_b,s)

The three sum to the active return r_p − r_b when both weight vectors sum to 1
and every sector has both returns (Brinson-Fachler identity).

Inputs, and the approximations each carries (also surfaced in the response
`notes`):

  * **Portfolio sector weights / returns** — aggregated from
    `services.position_alpha` per-name rows by GICS sector. Weights are
    beginning-of-window (each name's `value_at_start`); a sector's return is
    its summed P&L ÷ summed beginning value. Only names that map to one of the
    11 GICS sectors are included — cash, crypto, broad-index ETFs, funds, and
    unclassified names are excluded (reported as the uncovered sleeve).
  * **Benchmark sector returns** — price returns for the 11 SPDR Select Sector
    ETFs (XLK/XLF/…/XLC) over the window.
  * **Benchmark sector weights** — there is no live S&P 500 sector-weight feed,
    so a documented STATIC, approximate weight map is used (renormalized to sum
    to 1). Only the relative sizes matter for the attribution.
  * **Benchmark price return r_b** — reconstructed as Σ_s w_b,s·r_b,s (the model
    benchmark implied by the static weights × sector-ETF returns), so the
    decomposition identity holds. The ACTUAL SPY price return is reported
    separately (`spy_actual_return_pct`) and differs because the sector weights
    are approximate.

The math is split from the DB layer: :func:`brinson_fachler` is pure (unit
tested on hand-built sectors); :func:`compute_brinson` is the DB-facing
assembler.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_tracker.models import Security, SecurityClassification
from portfolio_tracker.services.position_alpha import (
    _benchmark_closes_with_lookback,  # pyright: ignore[reportPrivateUsage]
    _last_known_price,  # pyright: ignore[reportPrivateUsage]
    compute_position_alpha,
)


@dataclass(frozen=True)
class _SectorDef:
    """A GICS sector, its SPDR ETF proxy, and an approximate static S&P 500 weight."""

    gics: str
    etf: str
    spy_weight: Decimal  # fraction; renormalized before use


# The 11 GICS sectors with their SPDR Select Sector ETF and an APPROXIMATE,
# static S&P 500 weight (no live sector-weight feed). The weights are a rough
# early-2026 snapshot and are renormalized to sum to 1 before use, so only their
# relative sizes matter — the absolute figures are illustrative, not live.
_SECTORS: tuple[_SectorDef, ...] = (
    _SectorDef("Information Technology", "XLK", Decimal("0.31")),
    _SectorDef("Financials", "XLF", Decimal("0.13")),
    _SectorDef("Health Care", "XLV", Decimal("0.11")),
    _SectorDef("Consumer Discretionary", "XLY", Decimal("0.10")),
    _SectorDef("Communication Services", "XLC", Decimal("0.09")),
    _SectorDef("Industrials", "XLI", Decimal("0.08")),
    _SectorDef("Consumer Staples", "XLP", Decimal("0.06")),
    _SectorDef("Energy", "XLE", Decimal("0.035")),
    _SectorDef("Utilities", "XLU", Decimal("0.025")),
    _SectorDef("Real Estate", "XLRE", Decimal("0.022")),
    _SectorDef("Materials", "XLB", Decimal("0.022")),
)

_SPY_WEIGHTS_SOURCE = (
    "Static approximate S&P 500 GICS sector weights (illustrative ~early-2026 "
    "snapshot, no live feed), renormalized to sum to 100%. Only relative sizes "
    "matter for the attribution."
)

# Map a stored sector string (yfinance/Yahoo taxonomy, or a manual GICS edit) to
# a canonical GICS sector. yfinance uses its own labels (e.g. "Technology",
# "Financial Services", "Consumer Cyclical") which differ from GICS names;
# `classify_securities` stores them verbatim, so both spellings are aliased.
_SECTOR_ALIASES: dict[str, str] = {
    "technology": "Information Technology",
    "information technology": "Information Technology",
    "financial services": "Financials",
    "financial": "Financials",
    "financials": "Financials",
    "healthcare": "Health Care",
    "health care": "Health Care",
    "energy": "Energy",
    "consumer cyclical": "Consumer Discretionary",
    "consumer discretionary": "Consumer Discretionary",
    "consumer defensive": "Consumer Staples",
    "consumer staples": "Consumer Staples",
    "industrials": "Industrials",
    "basic materials": "Materials",
    "materials": "Materials",
    "utilities": "Utilities",
    "real estate": "Real Estate",
    "communication services": "Communication Services",
    "communications": "Communication Services",
}


def canonical_sector(sector: str | None) -> str | None:
    """Normalize a stored sector string to a canonical GICS sector.

    Returns None for anything that isn't one of the 11 GICS sectors — e.g.
    `ETF/Fund`, `Crypto`, `Unclassified`, or a missing classification — so the
    caller excludes those from the equity attribution sleeve.
    """
    if not sector:
        return None
    return _SECTOR_ALIASES.get(sector.strip().lower())


def benchmark_sector_weights() -> dict[str, Decimal]:
    """Canonical GICS sector → benchmark weight fraction, renormalized to sum 1.

    The tiny Decimal-division residual is folded into the largest sector so the
    weights sum to EXACTLY 1, which keeps the Brinson identity exact downstream.
    """
    total = sum((s.spy_weight for s in _SECTORS), Decimal(0))
    weights = {s.gics: s.spy_weight / total for s in _SECTORS}
    largest = max(weights, key=lambda k: weights[k])
    weights[largest] += Decimal(1) - sum(weights.values(), Decimal(0))
    return weights


# ---- pure Brinson-Fachler math -------------------------------------------


@dataclass(frozen=True)
class SectorInput:
    """One sector's weights and returns (fractions). `r_p` / `r_b` are None when
    that side has no usable return for the sector (no held names with a
    beginning value, or no benchmark-ETF data)."""

    sector: str
    etf: str | None
    w_p: Decimal
    w_b: Decimal
    r_p: Decimal | None
    r_b: Decimal | None


@dataclass(frozen=True)
class SectorAttribution:
    sector: str
    etf: str | None
    w_p: Decimal
    w_b: Decimal
    r_p: Decimal | None
    r_b: Decimal | None
    allocation: Decimal | None
    selection: Decimal | None
    interaction: Decimal | None
    total: Decimal | None


@dataclass(frozen=True)
class BrinsonAttribution:
    benchmark_return: Decimal | None  # r_b = Σ w_b,s·r_b,s (reconstructed)
    portfolio_return: Decimal | None  # r_p = Σ w_p,s·r_p,s
    allocation: Decimal | None
    selection: Decimal | None
    interaction: Decimal | None
    total_active: Decimal | None
    sectors: list[SectorAttribution]


def brinson_fachler(sectors: list[SectorInput]) -> BrinsonAttribution:
    """Pure Brinson-Fachler decomposition over a list of sector inputs.

    `r_b` (benchmark total) is reconstructed as Σ w_b,s·r_b,s across sectors with
    benchmark-ETF data, so allocation/selection/interaction sum to r_p − r_b when
    every sector is fully specified and both weight vectors sum to 1.

    A sector contributes:
      * allocation only when its benchmark return (and the reconstructed r_b)
        is available;
      * selection + interaction only when BOTH its portfolio and benchmark
        returns are available (no selection decision is attributed to a sector
        the portfolio doesn't hold).
    Each total effect is None when none of its components are computable.
    """
    rb_sectors = [s for s in sectors if s.r_b is not None]
    r_b_total = sum((s.w_b * _d(s.r_b) for s in rb_sectors), Decimal(0)) if rb_sectors else None
    rp_sectors = [s for s in sectors if s.r_p is not None]
    r_p_total = sum((s.w_p * _d(s.r_p) for s in rp_sectors), Decimal(0)) if rp_sectors else None

    rows: list[SectorAttribution] = []
    alloc_sum = Decimal(0)
    sel_sum = Decimal(0)
    inter_sum = Decimal(0)
    any_alloc = any_sel = any_inter = False

    for s in sectors:
        allocation: Decimal | None = None
        selection: Decimal | None = None
        interaction: Decimal | None = None
        if s.r_b is not None and r_b_total is not None:
            allocation = (s.w_p - s.w_b) * (s.r_b - r_b_total)
            alloc_sum += allocation
            any_alloc = True
        if s.r_p is not None and s.r_b is not None:
            selection = s.w_b * (s.r_p - s.r_b)
            interaction = (s.w_p - s.w_b) * (s.r_p - s.r_b)
            sel_sum += selection
            inter_sum += interaction
            any_sel = True
            any_inter = True
        parts = [x for x in (allocation, selection, interaction) if x is not None]
        total = sum(parts, Decimal(0)) if parts else None
        rows.append(
            SectorAttribution(
                sector=s.sector,
                etf=s.etf,
                w_p=s.w_p,
                w_b=s.w_b,
                r_p=s.r_p,
                r_b=s.r_b,
                allocation=allocation,
                selection=selection,
                interaction=interaction,
                total=total,
            )
        )

    total_parts: list[Decimal] = []
    if any_alloc:
        total_parts.append(alloc_sum)
    if any_sel:
        total_parts.append(sel_sum)
    if any_inter:
        total_parts.append(inter_sum)
    total_active = sum(total_parts, Decimal(0)) if total_parts else None

    return BrinsonAttribution(
        benchmark_return=r_b_total,
        portfolio_return=r_p_total,
        allocation=alloc_sum if any_alloc else None,
        selection=sel_sum if any_sel else None,
        interaction=inter_sum if any_inter else None,
        total_active=total_active,
        sectors=rows,
    )


def _d(x: Decimal | None) -> Decimal:
    """Narrow a known-present Optional[Decimal] to Decimal (callers guard None)."""
    return x if x is not None else Decimal(0)


# ---- response models ------------------------------------------------------


class BrinsonSectorRow(BaseModel):
    sector: str
    etf: str | None
    portfolio_weight_pct: Decimal
    benchmark_weight_pct: Decimal
    portfolio_return_pct: Decimal | None
    benchmark_return_pct: Decimal | None
    allocation_effect_pct: Decimal | None
    selection_effect_pct: Decimal | None
    interaction_effect_pct: Decimal | None
    total_effect_pct: Decimal | None


class BrinsonResult(BaseModel):
    calculation_status: Literal["available", "unavailable"]
    calculation_reason_codes: list[str]
    start_date: date
    end_date: date
    benchmark: str
    # Model benchmark return = Σ w_b,s·r_b,s (reconstructed from static weights ×
    # sector-ETF returns); the decomposition sums to portfolio − this.
    benchmark_return_pct: Decimal | None
    # Actual SPY total return over the window, for contrast with the above.
    spy_actual_return_pct: Decimal | None
    portfolio_return_pct: Decimal | None  # over the classified equity sleeve
    allocation_effect_pct: Decimal | None
    selection_effect_pct: Decimal | None
    interaction_effect_pct: Decimal | None
    total_active_return_pct: Decimal | None
    sectors: list[BrinsonSectorRow]
    # How much of the active sleeve (by beginning value) maps to a GICS sector.
    classified_value: Decimal
    excluded_value: Decimal
    classified_weight_pct: Decimal | None
    benchmark_weights_source: str
    notes: list[str]


_PCT_QUANT = Decimal("0.0001")


def _pct(x: Decimal | None) -> Decimal | None:
    return (x * Decimal(100)).quantize(_PCT_QUANT) if x is not None else None


def _window_return(closes: dict[date, Decimal], start_date: date, end_date: date) -> Decimal | None:
    """Total return of a benchmark series over the window (forward-filled ends)."""
    p_start = _last_known_price(closes, start_date)
    p_end = _last_known_price(closes, end_date)
    if p_start is None or p_end is None or p_start <= 0:
        return None
    return (p_end - p_start) / p_start


def _sector_by_ticker(session: Session) -> dict[str, str]:
    """TICKER (upper) → canonical GICS sector, for names that map to one."""
    rows = session.execute(
        select(Security.ticker, SecurityClassification.sector).join(
            SecurityClassification,
            SecurityClassification.security_id == Security.security_id,
        )
    ).all()
    out: dict[str, str] = {}
    for ticker, sector in rows:
        if ticker is None:
            continue
        gics = canonical_sector(sector)
        if gics is not None:
            out.setdefault(ticker.upper(), gics)
    return out


def compute_brinson(session: Session, start_date: date, end_date: date) -> BrinsonResult:
    """Assemble the Brinson-Fachler attribution vs SPY for [start_date, end_date]."""
    pa = compute_position_alpha(session, start_date, end_date, exclude_broad_index=False)
    sector_by_ticker = _sector_by_ticker(session)

    vstart_by_sector: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    pnl_by_sector: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    classified_value = Decimal(0)
    excluded_value = Decimal(0)
    for row in pa.rows:
        gics = sector_by_ticker.get(row.ticker)
        if gics is None:
            excluded_value += row.value_at_start
            continue
        vstart_by_sector[gics] += row.value_at_start
        classified_value += row.value_at_start
        if row.actual_pl is not None:
            pnl_by_sector[gics] += row.actual_pl

    classified_weight_pct = (
        (classified_value / (classified_value + excluded_value) * Decimal(100)).quantize(_PCT_QUANT)
        if (classified_value + excluded_value) > 0
        else None
    )
    if pa.calculation_status == "unavailable":
        unavailable_notes = [
            "Brinson attribution is unavailable because the underlying "
            "invested-position price/trade calculation failed closed. Use "
            "whole-account Modified Dietz performance as the fallback."
        ]
        if classified_value + excluded_value == 0:
            unavailable_notes.append(
                "No classifiable equity positions have beginning value in this window."
            )
        return BrinsonResult(
            calculation_status="unavailable",
            calculation_reason_codes=pa.calculation_reason_codes,
            start_date=start_date,
            end_date=end_date,
            benchmark="SPY",
            benchmark_return_pct=None,
            spy_actual_return_pct=None,
            portfolio_return_pct=None,
            allocation_effect_pct=None,
            selection_effect_pct=None,
            interaction_effect_pct=None,
            total_active_return_pct=None,
            sectors=[],
            classified_value=classified_value.quantize(Decimal("0.01")),
            excluded_value=excluded_value.quantize(Decimal("0.01")),
            classified_weight_pct=classified_weight_pct,
            benchmark_weights_source=_SPY_WEIGHTS_SOURCE,
            notes=unavailable_notes,
        )

    bench_weights = benchmark_sector_weights()
    spy_closes = _benchmark_closes_with_lookback(
        session, "SPY", start_date, end_date, total_return=False
    )
    spy_actual = _window_return(spy_closes, start_date, end_date)

    covered = classified_value > 0
    inputs: list[SectorInput] = []
    sectors_missing_etf: list[str] = []
    for s in _SECTORS:
        w_b = bench_weights[s.gics]
        v_start_s = vstart_by_sector.get(s.gics, Decimal(0))
        w_p = (v_start_s / classified_value) if covered else Decimal(0)
        r_p = (pnl_by_sector[s.gics] / v_start_s) if v_start_s > 0 else None
        etf_closes = _benchmark_closes_with_lookback(
            session, s.etf, start_date, end_date, total_return=False
        )
        r_b = _window_return(etf_closes, start_date, end_date)
        if r_b is None:
            sectors_missing_etf.append(s.etf)
        inputs.append(SectorInput(sector=s.gics, etf=s.etf, w_p=w_p, w_b=w_b, r_p=r_p, r_b=r_b))

    attribution = brinson_fachler(inputs)

    sector_rows = [
        BrinsonSectorRow(
            sector=a.sector,
            etf=a.etf,
            portfolio_weight_pct=(a.w_p * Decimal(100)).quantize(_PCT_QUANT),
            benchmark_weight_pct=(a.w_b * Decimal(100)).quantize(_PCT_QUANT),
            portfolio_return_pct=_pct(a.r_p),
            benchmark_return_pct=_pct(a.r_b),
            allocation_effect_pct=_pct(a.allocation),
            selection_effect_pct=_pct(a.selection),
            interaction_effect_pct=_pct(a.interaction),
            total_effect_pct=_pct(a.total),
        )
        for a in attribution.sectors
    ]
    sector_rows.sort(key=lambda r: (-r.benchmark_weight_pct, r.sector))

    notes = _brinson_notes(covered, excluded_value, sectors_missing_etf, attribution)

    return BrinsonResult(
        calculation_status="available",
        calculation_reason_codes=[],
        start_date=start_date,
        end_date=end_date,
        benchmark="SPY",
        benchmark_return_pct=_pct(attribution.benchmark_return),
        spy_actual_return_pct=_pct(spy_actual),
        portfolio_return_pct=_pct(attribution.portfolio_return),
        allocation_effect_pct=_pct(attribution.allocation),
        selection_effect_pct=_pct(attribution.selection),
        interaction_effect_pct=_pct(attribution.interaction),
        total_active_return_pct=_pct(attribution.total_active),
        sectors=sector_rows,
        classified_value=classified_value.quantize(Decimal("0.01")),
        excluded_value=excluded_value.quantize(Decimal("0.01")),
        classified_weight_pct=classified_weight_pct,
        benchmark_weights_source=_SPY_WEIGHTS_SOURCE,
        notes=notes,
    )


def _brinson_notes(
    covered: bool,
    excluded_value: Decimal,
    sectors_missing_etf: list[str],
    attribution: BrinsonAttribution,
) -> list[str]:
    notes = [
        "Brinson-Fachler attribution vs the S&P 500 (SPY). Allocation = "
        "Σ(w_p−w_b)(r_b,s−r_b); selection = Σ w_b(r_p,s−r_b,s); interaction = "
        "Σ(w_p−w_b)(r_p,s−r_b,s). The three sum to portfolio − benchmark return.",
        "Benchmark price return is reconstructed as Σ(benchmark sector weight × "
        "sector-ETF return) so the decomposition is exact; it differs from the "
        "actual SPY price return (spy_actual_return_pct) because the sector "
        "weights are a static approximation.",
        _SPY_WEIGHTS_SOURCE,
        "Portfolio sector weights are beginning-of-window (each name's "
        "value_at_start); a sector's return is its summed P&L ÷ summed beginning "
        "value. Names opened mid-window contribute P&L but no beginning weight, "
        "slightly biasing that sector's return.",
        "Benchmark sector returns use raw closes for the 11 SPDR Select Sector "
        "ETFs as GICS sector proxies, matching the portfolio price/trade basis.",
    ]
    if not covered:
        notes.append(
            "No classifiable equity positions at the window start — the portfolio "
            "side is empty (only cash/crypto/funds/unclassified, or all positions "
            "opened mid-window). Effects requiring portfolio returns are null."
        )
    if excluded_value > 0:
        notes.append(
            f"${excluded_value.quantize(Decimal('1'))} of beginning value is excluded "
            f"from the attribution (cash, crypto, broad-index ETFs, funds, and "
            f"unclassified names have no single GICS sector)."
        )
    if sectors_missing_etf:
        notes.append(
            f"No benchmark-ETF price data over the window for: "
            f"{', '.join(sorted(sectors_missing_etf))} — those sectors are dropped "
            f"from the benchmark reconstruction and their effects are null. Run "
            f"`python -m portfolio_tracker.jobs.benchmarks --start <earliest>`."
        )
    if attribution.total_active is None:
        notes.append(
            "Attribution could not be computed — missing both portfolio and "
            "benchmark sector returns for the window."
        )
    return notes
