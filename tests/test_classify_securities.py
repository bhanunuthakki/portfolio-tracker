"""Security classification enrichment job (jobs/classify_securities.py).

The network fetch is injected so these stay offline. Covers the info→
(sector, region) mapping and the run loop's source/skip semantics.
"""

from __future__ import annotations

from collections.abc import Mapping

from portfolio_tracker.jobs.classify_securities import (
    _classification_for,
    classify_securities,
)
from portfolio_tracker.models import Security, SecurityClassification
from portfolio_tracker.services.positioning import AssetType


class TestClassificationMapping:
    def test_stock_uses_sector_and_country(self):
        assert _classification_for(
            AssetType.STOCK, {"sector": "Technology", "country": "United States"}
        ) == ("Technology", "US")

    def test_adr_is_international(self):
        assert _classification_for(
            AssetType.STOCK, {"sector": "Healthcare", "country": "Denmark"}
        ) == ("Healthcare", "International")

    def test_stock_without_info(self):
        assert _classification_for(AssetType.STOCK, {}) == (None, "Unknown")

    def test_fund_is_bucketed_without_fetch(self):
        assert _classification_for(AssetType.ETF, {}) == ("ETF/Fund", None)
        assert _classification_for(AssetType.MUTUAL_FUND, {}) == ("ETF/Fund", None)

    def test_crypto(self):
        assert _classification_for(AssetType.CRYPTO, {}) == ("Crypto", None)

    def test_other_and_cash_get_nothing(self):
        assert _classification_for(AssetType.OTHER, {}) == (None, None)
        assert _classification_for(AssetType.CASH, {}) == (None, None)


def _fake_fetch(table: Mapping[str, Mapping[str, object]]):
    def fetch(ticker: str) -> Mapping[str, object]:
        return table.get(ticker, {})

    return fetch


class TestClassifySecuritiesRun:
    def _seed(self, session):
        nvda = Security(
            plaid_security_id="s-nvda", ticker="NVDA", type="cs", is_cash_equivalent=False
        )
        voo = Security(plaid_security_id="s-voo", ticker="VOO", type="et", is_cash_equivalent=False)
        nvo = Security(plaid_security_id="s-nvo", ticker="NVO", type="ad", is_cash_equivalent=False)
        sgov = Security(
            plaid_security_id="s-sgov", ticker="SGOV", type="oef", is_cash_equivalent=True
        )
        opt = Security(
            plaid_security_id="s-opt", ticker="OPT", type="derivative", is_cash_equivalent=False
        )
        meli = Security(
            plaid_security_id="s-meli", ticker="MELI", type="cs", is_cash_equivalent=False
        )
        session.add_all([nvda, voo, nvo, sgov, opt, meli])
        session.flush()
        # MELI already manually classified — the job must not touch it.
        session.add(
            SecurityClassification(
                security_id=meli.security_id,
                sector="Consumer Cyclical",
                region="International",
                source="manual",
            )
        )
        session.commit()
        return {s.ticker: s.security_id for s in (nvda, voo, nvo, sgov, opt, meli)}

    def test_classifies_and_respects_manual(self, session):
        ids = self._seed(session)
        fetch = _fake_fetch(
            {
                "NVDA": {"sector": "Technology", "country": "United States"},
                "NVO": {"sector": "Healthcare", "country": "Denmark"},
            }
        )
        counts = classify_securities(session, fetch_info=fetch)
        session.commit()

        rows = {c.security_id: c for c in session.query(SecurityClassification).all()}
        assert rows[ids["NVDA"]].sector == "Technology"
        assert rows[ids["NVDA"]].region == "US"
        assert rows[ids["NVDA"]].source == "auto"
        assert rows[ids["VOO"]].sector == "ETF/Fund"
        assert rows[ids["NVO"]].region == "International"
        # cash + derivatives get no row
        assert ids["SGOV"] not in rows
        assert ids["OPT"] not in rows
        # manual row untouched
        assert rows[ids["MELI"]].sector == "Consumer Cyclical"
        assert rows[ids["MELI"]].source == "manual"
        assert counts["classified"] == 3
        assert counts["skipped_manual"] == 1

    def test_only_missing_skips_existing_auto_rows(self, session):
        ids = self._seed(session)
        session.add(
            SecurityClassification(
                security_id=ids["NVDA"], sector="Stale", region="US", source="auto"
            )
        )
        session.commit()
        fetch = _fake_fetch({"NVDA": {"sector": "Technology", "country": "United States"}})
        classify_securities(session, fetch_info=fetch, only_missing=True)
        session.commit()
        row = session.get(SecurityClassification, ids["NVDA"])
        assert row.sector == "Stale"  # not re-fetched when only_missing=True
