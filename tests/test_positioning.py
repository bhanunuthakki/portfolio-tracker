"""Positioning cuts — pure classification, concentration, and correlation math.

These pin the structural behavior of the breakdown helpers (asset type,
tax treatment, region, concentration, per-ticker correlation/beta). DB-backed
assembly is covered in `test_positioning_service.py`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portfolio_tracker.services.positioning import (
    AssetType,
    TaxTreatment,
    classify_asset_type,
    classify_tax_treatment,
    concentration_metrics,
    correlation_beta,
    country_to_region,
)


class TestAssetType:
    @pytest.mark.parametrize(
        "t", ["cs", "stock", "equity", "Common Stock", "ad", "ADR", "preferred stock"]
    )
    def test_equities_and_adrs_are_stock(self, t):
        # ADRs (NVO, TSM, VALE) are equities for the asset-type cut; their
        # foreign domicile shows up in the region cut, not here.
        assert classify_asset_type(t, False) is AssetType.STOCK

    @pytest.mark.parametrize("t", ["et", "etf", "ETF", "etn"])
    def test_etfs(self, t):
        assert classify_asset_type(t, False) is AssetType.ETF

    def test_oef_fund_is_mutual_fund(self):
        assert classify_asset_type("oef", False) is AssetType.MUTUAL_FUND

    def test_cash_equivalent_wins_over_type(self):
        # FDRXX / SPAXX arrive typed as oef but flagged cash-equivalent.
        assert classify_asset_type("oef", True) is AssetType.CASH

    def test_plaid_cash_type(self):
        assert classify_asset_type("cash", False) is AssetType.CASH

    @pytest.mark.parametrize("t", ["cryptocurrency", "crypto"])
    def test_crypto(self, t):
        assert classify_asset_type(t, False) is AssetType.CRYPTO

    @pytest.mark.parametrize("t", ["derivative", "option", "fixed income", "loan", None, "wat"])
    def test_unknown_and_derivatives_are_other(self, t):
        assert classify_asset_type(t, False) is AssetType.OTHER


class TestTaxTreatment:
    @pytest.mark.parametrize(
        "sub", ["ROTH_IRA", "TRADITIONAL_IRA", "ira", "roth", "401k", "sep_ira"]
    )
    def test_retirement_subtypes(self, sub):
        assert classify_tax_treatment(sub, "investment", "x") is TaxTreatment.TAX_ADVANTAGED

    @pytest.mark.parametrize("sub", ["INDIVIDUAL", "brokerage", "taxable"])
    def test_taxable_subtypes(self, sub):
        assert classify_tax_treatment(sub, "investment", "x") is TaxTreatment.TAXABLE

    def test_brokerage_account_type_when_subtype_missing(self):
        assert (
            classify_tax_treatment(None, "brokerage", "Schwab Historical") is TaxTreatment.TAXABLE
        )

    def test_name_fallback_brokeragelink_roth(self):
        # Fidelity feeds omit subtype; the name is the only signal.
        assert (
            classify_tax_treatment(None, "investment", "BrokerageLink Roth")
            is TaxTreatment.TAX_ADVANTAGED
        )

    def test_name_fallback_brokeragelink(self):
        assert (
            classify_tax_treatment(None, "investment", "BrokerageLink")
            is TaxTreatment.TAX_ADVANTAGED
        )

    def test_name_fallback_hsa(self):
        assert (
            classify_tax_treatment(None, "investment", "Health Savings Account")
            is TaxTreatment.TAX_ADVANTAGED
        )

    def test_name_fallback_401k(self):
        assert (
            classify_tax_treatment(None, "investment", "META PLATFORMS, INC. 401(K) PLAN")
            is TaxTreatment.TAX_ADVANTAGED
        )

    def test_unknown_when_no_signal(self):
        assert classify_tax_treatment(None, "investment", "Mystery Account") is TaxTreatment.UNKNOWN


class TestRegion:
    @pytest.mark.parametrize("c", ["United States", "USA", "us", "United States of America"])
    def test_us(self, c):
        assert country_to_region(c) == "US"

    @pytest.mark.parametrize("c", ["Denmark", "Taiwan", "Brazil", "China"])
    def test_international(self, c):
        assert country_to_region(c) == "International"

    @pytest.mark.parametrize("c", [None, ""])
    def test_unknown(self, c):
        assert country_to_region(c) == "Unknown"


class TestConcentration:
    def test_equal_weights(self):
        c = concentration_metrics([Decimal(25)] * 4)
        assert c.num_positions == 4
        assert round(c.effective_holdings, 4) == 4.0
        assert round(float(c.top1_weight_pct), 2) == 25.0
        assert round(float(c.top5_weight_pct), 2) == 100.0

    def test_single_position_fully_concentrated(self):
        c = concentration_metrics([Decimal(1000)])
        assert c.num_positions == 1
        assert round(c.effective_holdings, 4) == 1.0
        assert round(c.hhi) == 10000
        assert round(float(c.top1_weight_pct), 2) == 100.0

    def test_empty_and_zero_do_not_divide_by_zero(self):
        c = concentration_metrics([Decimal(0), Decimal(0)])
        assert c.num_positions == 0
        assert c.effective_holdings is None
        assert c.top1_weight_pct is None

    def test_top5_caps_at_five_largest(self):
        vals = [Decimal(v) for v in [50, 20, 10, 8, 7, 5]]  # total 100
        c = concentration_metrics(vals)
        assert c.num_positions == 6
        assert round(float(c.top5_weight_pct), 2) == 95.0  # excludes the trailing 5


class TestCorrelationBeta:
    def _returns(self, scale: str) -> dict[date, Decimal]:
        days = [date(2025, 1, d) for d in range(2, 12)]
        return {d: Decimal(scale) * (i + 1) for i, d in enumerate(days)}

    def test_perfectly_correlated(self):
        sec = self._returns("0.01")
        corr, beta, n = correlation_beta(sec, dict(sec))
        assert n == len(sec)
        assert round(corr, 6) == 1.0
        assert round(beta, 6) == 1.0

    def test_beta_two(self):
        bench = self._returns("0.01")
        sec = {d: v * 2 for d, v in bench.items()}
        corr, beta, _n = correlation_beta(sec, bench)
        assert round(corr, 6) == 1.0
        assert round(beta, 6) == 2.0

    def test_no_overlap_returns_none(self):
        corr, beta, n = correlation_beta(
            {date(2025, 1, 2): Decimal("0.01")}, {date(2025, 1, 3): Decimal("0.01")}
        )
        assert (corr, beta, n) == (None, None, 0)
