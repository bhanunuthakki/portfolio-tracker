"""Unit tests for the pure Brinson-Fachler math + sector mapping (no DB)."""

from __future__ import annotations

from decimal import Decimal

from portfolio_tracker.services.brinson import (
    SectorInput,
    benchmark_sector_weights,
    brinson_fachler,
    canonical_sector,
)


def test_canonical_sector_aliases():
    # yfinance labels normalize to canonical GICS sectors.
    assert canonical_sector("Technology") == "Information Technology"
    assert canonical_sector("Financial Services") == "Financials"
    assert canonical_sector("Consumer Cyclical") == "Consumer Discretionary"
    assert canonical_sector("Consumer Defensive") == "Consumer Staples"
    assert canonical_sector("Basic Materials") == "Materials"
    assert canonical_sector("healthcare") == "Health Care"  # case-insensitive
    # Non-GICS buckets and unknowns map to None (excluded from the sleeve).
    assert canonical_sector("ETF/Fund") is None
    assert canonical_sector("Crypto") is None
    assert canonical_sector("Unclassified") is None
    assert canonical_sector(None) is None
    assert canonical_sector("") is None


def test_benchmark_weights_sum_to_one():
    weights = benchmark_sector_weights()
    assert len(weights) == 11
    assert sum(weights.values()) == Decimal(1)


def test_brinson_identity_two_sectors():
    # A fully-specified 2-sector world; both weight vectors sum to 1, so the
    # three effects must sum exactly to r_p − r_b.
    #   A: w_p=.6 w_b=.5 r_p=.10 r_b=.08
    #   B: w_p=.4 w_b=.5 r_p=.05 r_b=.06
    sectors = [
        SectorInput("A", "XLA", Decimal("0.6"), Decimal("0.5"), Decimal("0.10"), Decimal("0.08")),
        SectorInput("B", "XLB", Decimal("0.4"), Decimal("0.5"), Decimal("0.05"), Decimal("0.06")),
    ]
    res = brinson_fachler(sectors)

    assert res.benchmark_return == Decimal("0.07")  # .5*.08 + .5*.06
    assert res.portfolio_return == Decimal("0.08")  # .6*.10 + .4*.05
    assert res.allocation == Decimal("0.002")
    assert res.selection == Decimal("0.005")
    assert res.interaction == Decimal("0.003")
    assert res.total_active == Decimal("0.010")
    # Identity: alloc + sel + inter == r_p − r_b
    assert res.total_active == res.portfolio_return - res.benchmark_return


def test_brinson_per_sector_effects():
    sectors = [
        SectorInput("A", "XLA", Decimal("0.6"), Decimal("0.5"), Decimal("0.10"), Decimal("0.08")),
        SectorInput("B", "XLB", Decimal("0.4"), Decimal("0.5"), Decimal("0.05"), Decimal("0.06")),
    ]
    res = brinson_fachler(sectors)
    a = next(s for s in res.sectors if s.sector == "A")
    # r_b reconstructed = 0.07; allocation_A = (.6−.5)(.08−.07) = .001
    assert a.allocation == Decimal("0.001")
    # selection_A = .5(.10−.08) = .010
    assert a.selection == Decimal("0.010")
    # interaction_A = (.6−.5)(.10−.08) = .002
    assert a.interaction == Decimal("0.002")
    assert a.total == Decimal("0.013")


def test_brinson_missing_benchmark_return_drops_sector():
    # Sector B has no ETF data (r_b=None): its allocation/selection/interaction
    # are all None, and it's dropped from the benchmark reconstruction.
    sectors = [
        SectorInput("A", "XLA", Decimal("0.5"), Decimal("0.5"), Decimal("0.10"), Decimal("0.08")),
        SectorInput("B", "XLB", Decimal("0.5"), Decimal("0.5"), Decimal("0.05"), None),
    ]
    res = brinson_fachler(sectors)
    b = next(s for s in res.sectors if s.sector == "B")
    assert b.allocation is None
    assert b.selection is None
    assert b.interaction is None
    assert b.total is None
    # r_b reconstructed only from A: w_b(A)=.5 * r_b(A)=.08 = .04
    assert res.benchmark_return == Decimal("0.04")


def test_brinson_unheld_sector_has_allocation_only():
    # Sector B has no portfolio return (r_p=None: nothing held there): no
    # selection/interaction attributed, but allocation (underweight) still is.
    sectors = [
        SectorInput("A", "XLA", Decimal("1.0"), Decimal("0.5"), Decimal("0.10"), Decimal("0.08")),
        SectorInput("B", "XLB", Decimal("0.0"), Decimal("0.5"), None, Decimal("0.06")),
    ]
    res = brinson_fachler(sectors)
    b = next(s for s in res.sectors if s.sector == "B")
    assert b.selection is None
    assert b.interaction is None
    assert b.allocation is not None  # (0 − .5)(r_b,B − r_b)
    assert res.portfolio_return == Decimal("0.10")  # only A contributes


def test_brinson_all_missing_is_empty():
    sectors = [
        SectorInput("A", "XLA", Decimal("1.0"), Decimal("1.0"), None, None),
    ]
    res = brinson_fachler(sectors)
    assert res.benchmark_return is None
    assert res.portfolio_return is None
    assert res.total_active is None
    assert res.allocation is None
