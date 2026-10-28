"""New exact-figure coverage: BLS subgroups and states, consumer prices,
federal budget totals, quarterly GDP, two-point claims, and the rule that an
AI verdict without a source is never shown.

Fixture provenance:
  * treasury_mts_table1_2024-09-30.json is a live capture of MTS Table 1
    (record_date 2024-09-30), taken 2026-10-28.
  * BLS rows below use values read from the live API on 2026-10-28 where
    the test depends on a real figure (e.g. eggs $2.507 in Dec 2023).
  * BEA quarterly rows follow the documented NIPA layout (TimePeriod
    "2026Q2"); BEA needs a key, so they were not captured live.
"""
import json
import pathlib
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest

import config
import evidence.cache as cache_mod
from claims.parser import parse_claim
from claims.verdict import CONTRADICTED, NO_VERDICT, SUPPORTED
from evidence.sources import bea as bea_source
from evidence.sources import bls as bls_source
from evidence.units import humanize, parse_quantity
from services.verify_v2 import verify

pytestmark = pytest.mark.asyncio
FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "BEA_API_KEY", "k")
    monkeypatch.setattr(bea_source, "BEA_API_KEY", "k")


def months(year, values):
    """BLS rows, newest first, as the API returns them."""
    return [{"year": str(year), "period": f"M{m:02d}", "periodName": "", "value": str(v)}
            for m, v in sorted(enumerate(values, start=1), reverse=True)]


def fake_bls(table):
    """table: {series_id: {year: [12 monthly values]}}"""
    calls = []

    async def _fetch_raw(series_id, start, end):
        calls.append(series_id)
        rows = []
        for y in range(end, start - 1, -1):
            vals = table.get(series_id, {}).get(y)
            if vals:
                rows.extend(months(y, vals))
        return rows
    return _fetch_raw, calls


# --------------------------------------------------------------- BLS subgroups

class TestUnemploymentBySubgroupAndState:
    @pytest.mark.parametrize("claim,series,verdict", [
        ("Black unemployment was 5.5% in 2023", "LNS14000006", SUPPORTED),
        ("The unemployment rate for Black workers was 5.5% in 2023", "LNS14000006", SUPPORTED),
        ("The unemployment rate for Black Americans was 9% in 2023", "LNS14000006", CONTRADICTED),
        ("Hispanic unemployment was 4.6% in 2023", "LNS14000009", SUPPORTED),
        ("The unemployment rate for women was 3.4% in 2023", "LNS14000002", SUPPORTED),
        ("Teen unemployment was 11.2% in 2023", "LNS14000012", SUPPORTED),
        ("Unemployment among people aged 16-24 was 8% in 2023", "LNS14024887", SUPPORTED),
        ("Veteran unemployment was 3% in 2023", "LNU04049526", SUPPORTED),
    ])
    async def test_subgroup_series(self, claim, series, verdict):
        table = {
            "LNS14000006": {2023: [5.4, 5.7, 5.0, 4.7, 5.6, 6.0, 5.8, 5.3, 5.7, 5.7, 5.8, 5.2]},
            "LNS14000009": {2023: [4.9, 5.0, 4.6, 4.4, 4.0, 4.3, 4.4, 4.9, 4.6, 4.8, 4.7, 5.0]},
            "LNS14000002": {2023: [3.4, 3.5, 3.4, 3.2, 3.5, 3.5, 3.4, 3.6, 3.5, 3.6, 3.4, 3.4]},
            "LNS14000012": {2023: [10.9, 10.7, 9.8, 9.2, 11.3, 11.0, 11.2, 13.1, 12.6, 11.8, 11.4, 11.8]},
            "LNS14024887": {2023: [7.8, 7.5, 7.5, 6.8, 7.8, 7.5, 7.7, 8.5, 8.2, 8.2, 8.0, 8.0]},
            "LNU04049526": {2023: [3.4, 3.1, 2.9, 2.1, 2.3, 2.9, 2.8, 2.9, 2.8, 2.8, 2.8, 3.0]},
        }
        fetch, calls = fake_bls(table)
        with patch.object(bls_source, "_fetch_raw", fetch):
            r = await verify(claim, allow_grounding=False)
        assert r.verdict == verdict, r.headline
        assert calls == [series]
        assert r.tier == "structured" and r.model_calls == 0

    async def test_state_unemployment_uses_laus_and_names_the_state(self):
        fetch, calls = fake_bls({"LAUST480000000000003": {2023: [4.3, 4.2, 4.0, 3.7, 4.0, 4.3, 4.3, 4.2, 4.0, 3.9, 3.9, 3.7]}})
        with patch.object(bls_source, "_fetch_raw", fetch):
            r = await verify("Unemployment in Texas was 4.0% in 2023", allow_grounding=False)
        assert r.verdict == SUPPORTED, r.headline
        assert calls == ["LAUST480000000000003"]
        assert r.evidence[0]["geography"] == "Texas"

    async def test_two_subgroups_still_decline(self):
        p = parse_claim("The unemployment rate for Black women was 5% in 2023")
        assert p.structured_blocker

    async def test_subgroup_at_state_level_declines(self):
        r = await verify("Black unemployment in Texas was 6% in 2023", allow_grounding=False)
        assert r.verdict == NO_VERDICT and r.reason == "no_official_series_at_that_geography"


# --------------------------------------------------------------- consumer prices

class TestConsumerPrices:
    EGGS_2023 = [4.823, 4.211, 3.446, 3.270, 2.666, 2.219, 2.094, 2.043, 2.065, 2.072, 2.138, 2.507]
    GAS_2023 = [3.392, 3.444, 3.448, 3.660, 3.636, 3.656, 3.681, 3.966, 3.939, 3.680, 3.500, 3.289]

    async def test_annual_average_price(self):
        fetch, _ = fake_bls({"APU000074714": {2023: self.GAS_2023}})
        with patch.object(bls_source, "_fetch_raw", fetch):
            r = await verify("Gas averaged $3.60 a gallon in 2023", allow_grounding=False)
        assert r.verdict == SUPPORTED, r.headline
        assert r.evidence[0]["value_display"].startswith("$3.")

    async def test_monthly_price(self):
        fetch, _ = fake_bls({"APU0000708111": {2023: self.EGGS_2023}})
        with patch.object(bls_source, "_fetch_raw", fetch):
            ok = await verify("A dozen eggs cost $2.51 in December 2023", allow_grounding=False)
            bad = await verify("A dozen eggs cost $6 in December 2023", allow_grounding=False)
        assert ok.verdict == SUPPORTED, ok.headline
        assert bad.verdict == CONTRADICTED

    async def test_present_tense_uses_latest_month(self, monkeypatch):
        from datetime import datetime, timezone
        y = datetime.now(timezone.utc).year
        fetch, _ = fake_bls({"APU000074714": {y - 1: self.GAS_2023, y: [3.1, 3.2, 3.3]}})
        with patch.object(bls_source, "_fetch_raw", fetch):
            r = await verify("Gas is over $10 a gallon", allow_grounding=False)
        assert r.verdict == CONTRADICTED
        assert r.evidence[0]["period_kind"] == "monthly"
        assert r.evidence[0]["value"] == 3.3

    async def test_loose_price_quote_is_not_wrong_by_two_cents(self):
        fetch, _ = fake_bls({"APU000074714": {2023: self.GAS_2023}})
        with patch.object(bls_source, "_fetch_raw", fetch):
            r = await verify("Gas was $3.55 a gallon in 2023", allow_grounding=False)
        assert r.verdict == SUPPORTED, r.headline  # actual mean ≈ $3.61, within 2%

    def test_cents_and_display(self):
        assert parse_quantity("17 cents per kWh").magnitude == pytest.approx(0.17)
        assert humanize(0.169, "usd") == "$0.169"
        assert humanize(3.523, "usd") == "$3.52"
        assert humanize(77719, "usd") == "$77,719"


# --------------------------------------------------------------- federal budget

class TestFederalBudget:
    @pytest.fixture
    def mts(self, monkeypatch):
        payload = json.loads((FIXTURES / "treasury_mts_table1_2024-09-30.json").read_text())
        from evidence.sources import treasury

        async def cached(ns, key, ttl, loader):
            return {"payload": payload, "url": "https://example"} if key == "mts1:2024" else None
        monkeypatch.setattr(treasury, "cached", cached)

    @pytest.mark.parametrize("claim,verdict,value", [
        ("The federal deficit was $1.8 trillion in 2024", SUPPORTED, 1832815989074.04),
        ("The budget deficit was $1 trillion in 2024", CONTRADICTED, 1832815989074.04),
        ("The deficit was more than $1.5 trillion in FY2024", SUPPORTED, 1832815989074.04),
        ("Federal revenue was $4.9 trillion in 2024", SUPPORTED, 4918735644738.25),
        ("Federal spending was $6.75 trillion in 2024", SUPPORTED, 6751551633812.29),
        ("The government spent over $7 trillion in 2024", CONTRADICTED, 6751551633812.29),
    ])
    async def test_fiscal_year_totals(self, mts, claim, verdict, value):
        r = await verify(claim, allow_grounding=False)
        assert r.verdict == verdict, r.headline
        assert r.evidence[0]["value"] == pytest.approx(value)
        assert r.evidence[0]["period"].startswith("FY2024")
        assert any("fiscal year" in c for c in r.caveats)

    def test_trade_deficit_is_not_the_budget_deficit(self):
        assert parse_claim("The trade deficit was $800 billion in 2024").metric.metric == "trade_deficit"

    def test_mts_picks_each_year_by_its_parent_row(self):
        """The Sept 2024 report carries FY2023's final total before FY2024's;
        each must be read from its own "FY <year>" block, not by position."""
        from evidence.sources.treasury import _mts_row_to_datapoint
        rows = json.loads((FIXTURES / "treasury_mts_table1_2024-09-30.json").read_text())["data"]
        assert _mts_row_to_datapoint(rows, 2024, "deficit").quantity.magnitude == pytest.approx(1832815989074.04)
        assert _mts_row_to_datapoint(rows, 2023, "deficit").quantity.magnitude == pytest.approx(1695239736018.67)
        assert _mts_row_to_datapoint(rows, 2022, "deficit") is None


# --------------------------------------------------------------- quarterly GDP

class TestQuarterlyGDP:
    @pytest.fixture
    def bea_q(self, monkeypatch):
        rows = [
            {"TableName": "T10101", "SeriesCode": "A191RL", "LineNumber": "1",
             "LineDescription": "Gross domestic product", "TimePeriod": f"{y}Q{q}",
             "UNIT_MULT": "0", "DataValue": v}
            for (y, q, v) in [(2023, 3, "4.9"), (2023, 4, "3.4"), (2026, 1, "1.1"), (2026, 2, "2.2")]
        ]

        async def fetch_table(table, years, frequency="A"):
            assert frequency == "Q"
            return {"rows": [r for r in rows if int(r["TimePeriod"][:4]) in years], "url": "x"}
        monkeypatch.setattr(bea_source, "_fetch_table", fetch_table)

    @pytest.mark.parametrize("claim,verdict", [
        ("Real GDP increased at an annual rate of 2.2% in the second quarter of 2026", SUPPORTED),
        ("GDP grew 4.9% in Q3 2023", SUPPORTED),
        ("GDP grew at a 4.9% annual rate in 2023Q3", SUPPORTED),
        ("The economy grew 6% in the fourth quarter of 2023", CONTRADICTED),
    ])
    async def test_quarter(self, bea_q, claim, verdict):
        r = await verify(claim, allow_grounding=False)
        assert r.verdict == verdict, r.headline
        assert r.evidence[0]["period_kind"] == "quarterly"
        assert any("revises" in c for c in r.caveats)

    def test_q2_is_not_read_as_the_value_2(self):
        p = parse_claim("GDP grew 4.9% in Q2 2024")
        assert p.claimed_value.magnitude == 4.9 and not p.is_trend_claim and p.quarter == 2


# --------------------------------------------------------------- two-point claims

class TestTwoPointClaims:
    TABLE = {"LNS14000000": {
        2020: [3.5, 3.5, 4.4, 14.8, 13.2, 11.0, 10.2, 8.4, 7.8, 6.8, 6.7, 6.7],
        2023: [3.4, 3.6, 3.5, 3.4, 3.6, 3.6, 3.5, 3.7, 3.8, 3.9, 3.7, 3.7],
    }}

    @pytest.mark.parametrize("claim,verdict", [
        ("The unemployment rate fell from 8.1% in 2020 to 3.6% in 2023", SUPPORTED),
        ("Unemployment was 3.6% in 2023, down from 8.1% in 2020", SUPPORTED),
        ("The unemployment rate fell from 10% in 2020 to 3.6% in 2023", CONTRADICTED),
    ])
    async def test_each_point_checked(self, claim, verdict):
        fetch, calls = fake_bls(self.TABLE)
        with patch.object(bls_source, "_fetch_raw", fetch):
            r = await verify(claim, allow_grounding=False)
        assert r.verdict == verdict, r.headline
        assert len(r.evidence) == 2 and "2020:" in r.headline and "2023:" in r.headline


# --------------------------------------------------------------- unsourced AI

class TestNoUnsourcedVerdicts:
    async def test_ai_verdict_without_sources_is_not_shown(self):
        from evidence.grounded import GroundedAnswer
        g = GroundedAnswer(ok=True, verdict="CONTRADICTED", explanation="Gas is not $10.",
                           citations=[], model="claude-haiku-5-5")
        with patch("services.verify_v2.ai_search.search", AsyncMock(return_value=g)):
            r = await verify("Bananas cost $10 a pound in 1990")
        assert r.verdict == NO_VERDICT and r.reason == "no_source_found"


class TestReviewOfThisChange:
    def test_whole_percent_is_precise_to_one_point(self):
        """'10%' must not agree with 8.1% (it did: trailing-zero rule)."""
        assert parse_quantity("10%").tolerance() <= 0.5
        assert parse_quantity("$73,000").tolerance() >= 500  # dollars keep the rule

    async def test_gross_price_miss_is_contradicted_despite_alternate_measures(self):
        fetch, _ = fake_bls({"APU000074714": {2023: TestConsumerPrices.GAS_2023}})
        with patch.object(bls_source, "_fetch_raw", fetch):
            r = await verify("Gas was $10 a gallon in 2023", allow_grounding=False)
        assert r.verdict == CONTRADICTED
        assert "Census" not in r.headline


@pytest.mark.parametrize("claim", [
    "The poverty rate was 11.1% in 2023",          # official CPS rate
    "Median household income was $80,610 in 2023",  # CPS headline figure
])
async def test_other_official_census_figures_are_not_called_wrong(claim, monkeypatch):
    from evidence.datapoint import ANNUAL, Datapoint, Geography, Period
    from evidence.units import PERCENT, USD, Quantity
    from services import verify_v2 as v2
    monkeypatch.setattr(config, "CENSUS_API_KEY", "k")
    acs = {"poverty_rate": (12.5, PERCENT), "median_household_income": (77719, USD)}

    async def fake_census(spec, year, geo):
        val, fam = acs[spec.metric]
        return [Datapoint(quantity=Quantity(val, fam), metric=spec.metric, label=spec.label,
                          observed=Period(2023, ANNUAL), geography=Geography.national(),
                          source="CENSUS", source_url="x")]
    monkeypatch.setattr(v2.census_source, "fetch", fake_census)
    r = await verify(claim, allow_grounding=False)
    assert r.verdict == NO_VERDICT and r.reason == "agency_publishes_more_than_one_official_figure"
