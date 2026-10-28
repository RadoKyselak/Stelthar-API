"""Near misses: a claim off by one step in its last written digit is
"Mostly accurate", not "Contradicted".

Found on production 2026-10-28: "Unemployment in Texas was 4.1% in 2023"
(BLS: 4.0%) came back Contradicted with High confidence, the same verdict as
"Unemployment in Ohio was 10%" (BLS: 3.7%).
"""
import json
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

import config
import evidence.cache as cache_mod
from claims.verdict import CONTRADICTED, MOSTLY_ACCURATE, NO_VERDICT, SUPPORTED
from evidence.grounded import Citation, GroundedAnswer, _build_prompt, _parse_fields
from evidence.sources import bea as bea_source
from evidence.sources import bls as bls_source
from services.verify_compat import to_legacy_shape
from services.verify_v2 import verify

pytestmark = pytest.mark.asyncio
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TEXAS = "LAUST480000000000003"
NATION = "LNU04000000"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "BEA_API_KEY", "k")
    monkeypatch.setattr(bea_source, "BEA_API_KEY", "k")


def fake_bls(table):
    async def _fetch_raw(series_id, start, end):
        rows = []
        for y in range(end, start - 1, -1):
            for m, v in sorted(enumerate(table.get(series_id, {}).get(y) or [], start=1), reverse=True):
                rows.append({"year": str(y), "period": f"M{m:02d}", "periodName": "", "value": str(v)})
        return rows
    return _fetch_raw


async def run(claim, table, **kw):
    with patch.object(bls_source, "_fetch_raw", fake_bls(table)):
        return await verify(claim, allow_grounding=False, **kw)


def sourced(verdict):
    return GroundedAnswer(
        ok=True, verdict=verdict, official_value="2.1%", period="Q2 2026",
        source_org="BEA", explanation="x",
        citations=[Citation(title="BEA", uri="https://www.bea.gov/x", domain="www.bea.gov",
                            official=True)],
        model_calls=1, model="claude-haiku-5-5",
    )


class TestStructuredNearMiss:
    TX = {TEXAS: {2023: [4.0] * 12}}
    US = {NATION: {2023: [3.6] * 12}}

    async def test_one_step_off_is_mostly_accurate(self):
        r = await run("Unemployment in Texas was 4.1% in 2023", self.TX)
        assert r.verdict == MOSTLY_ACCURATE, r.headline
        assert "close, but off by 0.1 percentage point." in r.headline
        assert r.tier == "structured" and r.model_calls == 0

    async def test_one_step_under_is_mostly_accurate(self):
        r = await run("Unemployment in Texas was 3.9% in 2023", self.TX)
        assert r.verdict == MOSTLY_ACCURATE, r.headline

    @pytest.mark.parametrize("claim,verdict", [
        ("Unemployment in Texas was 4.0% in 2023", SUPPORTED),
        ("Unemployment in Texas was 4.3% in 2023", CONTRADICTED),     # three steps off
        ("Unemployment in Texas was 10% in 2023", CONTRADICTED),
        # Directional claims are thresholds: "over 4.1%" is false at 4.0%.
        ("Unemployment in Texas was over 4.1% in 2023", CONTRADICTED),
    ])
    async def test_other_gaps_unchanged(self, claim, verdict):
        r = await run(claim, self.TX)
        assert r.verdict == verdict, r.headline

    async def test_coarse_claim_is_not_rescued(self):
        """'3%' against 3.6% is one step in the claim's last digit but 17% off."""
        r = await run("Unemployment was 3% in 2023", self.US)
        assert r.verdict == CONTRADICTED, r.headline
        r = await run("Unemployment was 4% in 2023", self.US)
        assert r.verdict == SUPPORTED, r.headline  # 3.6% rounds to 4%

    async def test_dollar_totals(self, monkeypatch):
        payload = json.loads((FIXTURES / "treasury_mts_table1_2024-09-30.json").read_text())
        from evidence.sources import treasury

        async def cached(ns, key, ttl, loader):
            return {"payload": payload, "url": "https://example"} if key == "mts1:2024" else None
        monkeypatch.setattr(treasury, "cached", cached)
        near = await verify("The federal deficit was $1.9 trillion in 2024", allow_grounding=False)
        far = await verify("The federal deficit was $2.5 trillion in 2024", allow_grounding=False)
        assert near.verdict == MOSTLY_ACCURATE, near.headline   # $1.83T
        assert "off by $67.18 billion." in near.headline
        assert far.verdict == CONTRADICTED

    async def test_two_point_claim(self):
        table = {NATION: {2020: [8.1] * 12, 2023: [3.6] * 12}}
        ok = await run("The unemployment rate fell from 8.1% in 2020 to 3.6% in 2023", table)
        near = await run("The unemployment rate fell from 8.1% in 2020 to 3.7% in 2023", table)
        bad = await run("The unemployment rate fell from 8.1% in 2020 to 5% in 2023", table)
        assert ok.verdict == SUPPORTED
        assert near.verdict == MOSTLY_ACCURATE, near.headline
        assert bad.verdict == CONTRADICTED


@pytest.mark.parametrize("claim,blocked", [
    ("Unemployment was over 4.1% in 2023", False),      # a threshold, not an age
    ("Unemployment was above 3.5 percent in 2023", False),
    ("Unemployment for people over 65 was 3% in 2023", True),
    ("The poverty rate for those under 18 was 15% in 2023", True),
])
def test_decimal_threshold_is_not_an_age_group(claim, blocked):
    from claims.parser import parse_claim
    assert bool(parse_claim(claim).structured_blocker) is blocked


class TestNearMissWithAnotherOfficialMeasure:
    """GDP: a near miss may match an earlier BEA estimate exactly. The search
    may upgrade it to Supported, never downgrade it."""

    @pytest.fixture(autouse=True)
    def bea_q(self, monkeypatch):
        rows = [{"TableName": "T10101", "LineNumber": "1", "LineDescription": "Gross domestic product",
                 "TimePeriod": "2026Q2", "UNIT_MULT": "0", "DataValue": "2.2"}]

        async def fetch_table(table, years, frequency="A"):
            return {"rows": rows, "url": "x"}
        monkeypatch.setattr(bea_source, "_fetch_table", fetch_table)

    CLAIM = "GDP grew at a 2.1% annual rate in Q2 2026"

    async def test_without_search_the_near_miss_stands(self):
        r = await verify(self.CLAIM, allow_grounding=False)
        assert r.verdict == MOSTLY_ACCURATE, r.headline

    async def test_search_can_upgrade_to_supported(self):
        with patch("services.verify_v2.ai_search.search", AsyncMock(return_value=sourced("SUPPORTED"))):
            r = await verify(self.CLAIM)
        assert r.verdict == SUPPORTED and r.tier == "grounded"
        assert r.evidence  # the BEA figure rides along

    @pytest.mark.parametrize("g", [
        sourced("CONTRADICTED"),
        sourced("NO_EVIDENCE"),
        GroundedAnswer(ok=False, error="limit", skipped=True, limited=True, model_calls=0),
        GroundedAnswer(ok=False, error="AI search could not be reached", model_calls=0),
    ])
    async def test_search_cannot_downgrade(self, g):
        with patch("services.verify_v2.ai_search.search", AsyncMock(return_value=g)):
            r = await verify(self.CLAIM)
        assert r.verdict == MOSTLY_ACCURATE, r.headline
        assert r.tier == "structured" and r.model_calls == g.model_calls

    async def test_gross_miss_still_goes_to_search(self):
        r = await verify("GDP grew at a 1.5% annual rate in Q2 2026", allow_grounding=False)
        assert r.verdict == NO_VERDICT and r.reason == "agency_publishes_more_than_one_official_figure"


class TestAITierAndClients:
    def test_prompt_offers_the_verdict(self):
        assert "MOSTLY_ACCURATE" in _build_prompt("<claim>x</claim>")

    @pytest.mark.parametrize("line", ["VERDICT: MOSTLY_ACCURATE", "VERDICT: Mostly accurate",
                                      "VERDICT: **MOSTLY ACCURATE**"])
    def test_parse(self, line):
        assert _parse_fields(f"{line}\nOFFICIAL_VALUE: 7.3%\nEXPLANATION: x")["verdict"] == "MOSTLY_ACCURATE"

    async def test_ai_answer_maps_to_mostly_accurate(self):
        with patch("services.verify_v2.ai_search.search", AsyncMock(return_value=sourced("MOSTLY_ACCURATE"))):
            r = await verify("The unemployment rate in France was 7.4% in 2023")
        assert r.verdict == MOSTLY_ACCURATE and r.tier == "grounded"

    async def test_unsourced_ai_near_miss_is_not_shown(self):
        g = sourced("MOSTLY_ACCURATE")
        g.citations = []
        with patch("services.verify_v2.ai_search.search", AsyncMock(return_value=g)):
            r = await verify("The unemployment rate in France was 7.4% in 2023")
        assert r.verdict == NO_VERDICT and r.reason == "no_source_found"

    async def test_extension_endpoint_shape(self):
        r = await run("Unemployment in Texas was 4.1% in 2023", TestStructuredNearMiss.TX)
        out = to_legacy_shape(r)
        assert out["verdict"] == "Mostly accurate" and out["verdict_detail"] == "Mostly accurate"
        assert out["confidence"] > 0 and not out["degraded"]
