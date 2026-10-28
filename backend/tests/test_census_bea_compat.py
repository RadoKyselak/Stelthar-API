"""Census + BEA adapters, the Gemini model fallback, and the /verify bridge.

Fixture provenance: the Census and BEA payloads below follow each API's
documented response layout (Census: header row + data rows; BEA NIPA GetData:
BEAAPI.Results.Data with LineNumber / TimePeriod / UNIT_MULT / DataValue), with
values taken from the published 2023 ACS 1-year and NIPA tables. They were not
captured live — both APIs now require a key that is not available offline — so
when a key is at hand, re-capture them with tools/preflight.py and replace.
"""
import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import evidence.cache as cache_mod
from claims.metrics import BY_METRIC
from claims.parser import parse_claim
from claims.verdict import CONTRADICTED, NO_VERDICT, SUPPORTED
from evidence.datapoint import Geography
from evidence.sources import SourceUnavailable
from evidence.sources import bea as bea_source
from evidence.sources import census as census_source
from evidence.units import PERCENT, USD
from utils import gemini_models


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Every test starts cold so a cached payload cannot mask a parse bug."""
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path / "cache")
    # config reads keys at import, before conftest sets the env.
    monkeypatch.setattr(bea_source, "BEA_API_KEY", "test_bea_key")
    import config
    monkeypatch.setattr(config, "BEA_API_KEY", "test_bea_key")
    monkeypatch.setattr(config, "CENSUS_API_KEY", "test_census_key")
    gemini_models._reset_for_tests()
    yield
    gemini_models._reset_for_tests()


# ---------------------------------------------------------------- fakes

class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None, text: str = "",
                 headers: Optional[Dict[str, str]] = None,
                 url: str = "https://api.example.gov/x?key=SECRET"):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers = headers or {}
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", self.url)
            raise httpx.HTTPStatusError("err", request=req,
                                        response=httpx.Response(self.status_code, request=req))


def fake_client(get=None, post=None):
    client = MagicMock()
    if get is not None:
        client.get = AsyncMock(side_effect=get)
    if post is not None:
        client.post = AsyncMock(side_effect=post)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client


# ---------------------------------------------------------------- Census

# ACS 2023 1-year. B01003 total population is controlled to the population
# estimates, so its MOE is the -555555555 "controlled" sentinel.
CENSUS_ROWS = {
    ("acs/acs1", "B01003_001E", "us:1"):
        [["NAME", "B01003_001E", "B01003_001M", "us"],
         ["United States", "334914895", "-555555555", "1"]],
    ("acs/acs1", "B01003_001E", "state:48"):
        [["NAME", "B01003_001E", "B01003_001M", "state"],
         ["Texas", "30503301", "-555555555", "48"]],
    ("acs/acs1", "B01003_001E", "state:06"):
        [["NAME", "B01003_001E", "B01003_001M", "state"],
         ["California", "38965193", "-555555555", "06"]],
    ("acs/acs1", "B19013_001E", "us:1"):
        [["NAME", "B19013_001E", "B19013_001M", "us"],
         ["United States", "77719", "140", "1"]],
    ("acs/acs1/subject", "S1701_C03_001E", "state:48"):
        [["NAME", "S1701_C03_001E", "S1701_C03_001M", "state"],
         ["Texas", "13.7", "0.3", "48"]],
}


def census_get(published_years=(2023,), redirect=False):
    async def _get(url, params=None):
        if redirect:
            return FakeResponse(302, headers={"location": "https://api.census.gov/data/missing_key.html"})
        # url: https://api.census.gov/data/{year}/{dataset}
        tail = url.split("/data/", 1)[1]
        year_s, dataset = tail.split("/", 1)
        if int(year_s) not in published_years:
            return FakeResponse(404, text="<html>404</html>")
        var = params["get"].split(",")[1]
        rows = CENSUS_ROWS.get((dataset, var, params["for"]))
        if rows is None:
            return FakeResponse(400, text="error: unknown variable")
        return FakeResponse(200, rows, url=f"{url}?get={params['get']}&key=SECRET")
    return _get


@pytest.mark.asyncio
class TestCensusAdapter:
    async def test_national_population_reads_geography_from_row(self):
        ctx, _ = fake_client(get=census_get())
        with patch("evidence.sources.census.httpx.AsyncClient", return_value=ctx):
            out = await census_source.fetch(BY_METRIC["population"], 2023, None)
        assert len(out) == 1
        dp = out[0]
        assert dp.quantity.magnitude == 334914895
        assert dp.geography.name == "United States" and dp.geography.level == "nation"
        assert dp.observed.year == 2023
        # Controlled estimate: no sampling margin to widen tolerance with.
        assert "margin_of_error" not in dp.extra
        assert dp.source_url.startswith("https://data.census.gov/table/ACSDT1Y2023.B01003")
        assert "SECRET" not in json.dumps(dp.to_dict())

    async def test_state_row_carries_fips_from_payload(self):
        ctx, _ = fake_client(get=census_get())
        tx = Geography(name="Texas", level="state", fips="48")
        with patch("evidence.sources.census.httpx.AsyncClient", return_value=ctx):
            out = await census_source.fetch(BY_METRIC["population"], 2023, tx)
        assert out[0].geography.fips == "48" and out[0].geography.name == "Texas"

    async def test_rates_come_from_subject_table_with_moe(self):
        spec = BY_METRIC["poverty_rate"]
        assert spec.census_dataset == "acs/acs1/subject"
        ctx, _ = fake_client(get=census_get())
        tx = Geography(name="Texas", level="state", fips="48")
        with patch("evidence.sources.census.httpx.AsyncClient", return_value=ctx):
            out = await census_source.fetch(spec, 2023, tx)
        assert out[0].quantity.magnitude == 13.7 and out[0].quantity.family == PERCENT
        assert out[0].extra["margin_of_error"] == 0.3

    async def test_unpublished_year_is_absence_not_outage(self):
        ctx, _ = fake_client(get=census_get(published_years=(2023,)))
        with patch("evidence.sources.census.httpx.AsyncClient", return_value=ctx):
            out = await census_source.fetch(BY_METRIC["population"], 2019, None)
        assert out == []

    async def test_missing_key_redirect_is_an_outage(self):
        ctx, _ = fake_client(get=census_get(redirect=True))
        with patch("evidence.sources.census.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(SourceUnavailable) as e:
                await census_source.fetch(BY_METRIC["population"], 2023, None)
        assert "key" in e.value.reason

    async def test_latest_walks_back_to_newest_published_vintage(self):
        ctx, client = fake_client(get=census_get(published_years=(2023,)))
        with patch("evidence.sources.census.httpx.AsyncClient", return_value=ctx), \
             patch("evidence.sources.census.datetime") as dt:
            dt.now.return_value.year = 2025
            out = await census_source.fetch(BY_METRIC["population"], None, None)
        assert out and out[0].observed.year == 2023


# ---------------------------------------------------------------- BEA

def bea_payload(table: str) -> Dict[str, Any]:
    rows = {
        "T10105": [
            {"TableName": "T10105", "SeriesCode": "A191RC", "LineNumber": "1",
             "LineDescription": "Gross domestic product", "TimePeriod": "2023",
             "METRIC_NAME": "Current Dollars", "CL_UNIT": "Level", "UNIT_MULT": "6",
             "DataValue": "27,720,709", "NoteRef": "T10105"},
            {"TableName": "T10105", "SeriesCode": "A191RC", "LineNumber": "1",
             "LineDescription": "Gross domestic product", "TimePeriod": "2024",
             "METRIC_NAME": "Current Dollars", "CL_UNIT": "Level", "UNIT_MULT": "6",
             "DataValue": "29,184,890", "NoteRef": "T10105"},
            {"TableName": "T10105", "SeriesCode": "DPCERC", "LineNumber": "2",
             "LineDescription": "Personal consumption expenditures", "TimePeriod": "2023",
             "METRIC_NAME": "Current Dollars", "CL_UNIT": "Level", "UNIT_MULT": "6",
             "DataValue": "18,822,768", "NoteRef": "T10105"},
        ],
        "T10101": [
            {"TableName": "T10101", "SeriesCode": "A191RL", "LineNumber": "1",
             "LineDescription": "Gross domestic product", "TimePeriod": "2023",
             "METRIC_NAME": "Fisher Quantity Index", "CL_UNIT": "Percent change, annual rate",
             "UNIT_MULT": "0", "DataValue": "2.9", "NoteRef": "T10101"},
        ],
    }[table]
    return {"BEAAPI": {"Request": {}, "Results": {"Statistic": "NIPA Table", "Data": rows}}}


def bea_get(error: Optional[str] = None):
    async def _get(url, params=None):
        if error:
            return FakeResponse(200, {"BEAAPI": {"Results": {"Error": {
                "APIErrorCode": "3", "APIErrorDescription": error}}}})
        return FakeResponse(200, bea_payload(params["TableName"]),
                            url=f"{url}?UserID=SECRET&TableName={params['TableName']}")
    return _get


@pytest.mark.asyncio
class TestBEAAdapter:
    async def test_gdp_scaled_by_unit_mult_and_period_from_row(self):
        ctx, _ = fake_client(get=bea_get())
        with patch("evidence.sources.bea.httpx.AsyncClient", return_value=ctx):
            out = await bea_source.fetch(BY_METRIC["gdp"], 2023)
        assert len(out) == 1
        assert out[0].quantity.magnitude == pytest.approx(27.720709e12)
        assert out[0].quantity.family == USD
        assert out[0].observed.year == 2023
        assert "SECRET" not in json.dumps(out[0].to_dict()) + out[0].extra["api_url"]

    async def test_latest_picks_newest_year(self):
        ctx, _ = fake_client(get=bea_get())
        with patch("evidence.sources.bea.httpx.AsyncClient", return_value=ctx):
            out = await bea_source.fetch(BY_METRIC["gdp"], None)
        assert out[0].observed.year == 2024

    async def test_growth_rate_is_not_scaled(self):
        ctx, _ = fake_client(get=bea_get())
        with patch("evidence.sources.bea.httpx.AsyncClient", return_value=ctx):
            out = await bea_source.fetch(BY_METRIC["real_gdp_growth"], 2023)
        assert out[0].quantity.magnitude == 2.9 and out[0].quantity.family == PERCENT

    async def test_renumbered_line_fails_closed(self):
        """If line 1 stopped being GDP, refuse rather than mislabel."""
        from dataclasses import replace
        spec = replace(BY_METRIC["gdp"], bea_line="2")
        ctx, _ = fake_client(get=bea_get())
        with patch("evidence.sources.bea.httpx.AsyncClient", return_value=ctx):
            assert await bea_source.fetch(spec, 2023) == []

    async def test_bad_key_is_an_outage(self):
        ctx, _ = fake_client(get=bea_get(error="Invalid API UserId"))
        with patch("evidence.sources.bea.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(SourceUnavailable):
                await bea_source.fetch(BY_METRIC["gdp"], 2023)

    async def test_missing_key_is_an_outage(self):
        with patch("evidence.sources.bea.BEA_API_KEY", None):
            with pytest.raises(SourceUnavailable):
                await bea_source.fetch(BY_METRIC["gdp"], 2023)


# ---------------------------------------------------------------- end to end

@pytest.mark.asyncio
class TestEngineNowAnswersCensusAndBEA:
    """The EVAL-REPORT's Finding 2 claims — every one was 'No verdict'."""

    @pytest.mark.parametrize("claim,verdict", [
        ("U.S. population was about 335 million in 2023", SUPPORTED),
        ("The population of California was more than 38 million in 2023", SUPPORTED),
        ("The population of Texas was under 5 million in 2023", CONTRADICTED),
        ("U.S. GDP was more than $25 trillion in 2023", SUPPORTED),
        ("U.S. GDP grew by more than 50% in 2023", CONTRADICTED),
        ("Real GDP grew 2.9% in 2023", SUPPORTED),
        ("Median household income was $77,700 in 2023", SUPPORTED),
        # 13.5% is outside written precision (0.1) but inside the ±0.3 MOE.
        ("The poverty rate in Texas was 13.5% in 2023", SUPPORTED),
        # Census also publishes the CPS official poverty rate, so a mismatch
        # with ACS alone is not a contradiction.
        ("The poverty rate in Texas was 16% in 2023", NO_VERDICT),
    ])
    async def test_claim(self, claim, verdict):
        from services.verify_v2 import verify
        census, bea = census_get(), bea_get()

        # Both adapters share the one httpx module, so a single fake routes
        # by host.
        async def route(url, params=None):
            return await (bea if "bea.gov" in url else census)(url, params=params)
        ctx, _ = fake_client(get=route)
        with patch("httpx.AsyncClient", return_value=ctx), \
             patch("services.verify_v2.ground_claim", AsyncMock(side_effect=AssertionError("no LLM"))):
            r = await verify(claim, allow_grounding=False)
        assert r.verdict == verdict, r.headline
        assert r.tier == "structured" and r.model_calls == 0

    async def test_city_still_declines_to_state_series(self):
        from services.verify_v2 import verify
        r = await verify("The population of New York City was 8 million in 2023",
                         allow_grounding=False)
        assert r.verdict == NO_VERDICT

    def test_growth_phrasing_routes_to_growth_not_level(self):
        assert parse_claim("GDP grew 3% in 2023").metric.metric == "real_gdp_growth"
        assert parse_claim("GDP was $27 trillion in 2023").metric.metric == "gdp"


# ---------------------------------------------------------------- Gemini

def gemini_ok(text="VERDICT: SUPPORTED\nOFFICIAL_VALUE: 3.6%\nPERIOD: 2023\nSOURCE_ORG: BLS\nEXPLANATION: ok"):
    return {"candidates": [{"content": {"parts": [{"text": text}]},
                            "groundingMetadata": {"groundingChunks": [
                                {"web": {"uri": "https://vertexaisearch.cloud.google.com/x",
                                         "title": "bls.gov"}}],
                                "groundingSupports": [{"segment": {"text": "ok"},
                                                       "groundingChunkIndices": [0]}],
                                "searchEntryPoint": {"renderedContent": "<div>chips</div>"}}}]}


RETIRED = {"error": {"code": 404, "status": "NOT_FOUND", "message":
           "This model models/gemini-2.5-flash is no longer available to new users."}}


@pytest.mark.asyncio
class TestGeminiModelFallback:
    async def test_retired_model_falls_through_and_is_remembered(self):
        calls: List[str] = []

        async def post(url, headers=None, json=None):
            calls.append(url)
            if "gemini-2.5-flash" in url:
                return FakeResponse(404, RETIRED)
            return FakeResponse(200, gemini_ok())

        ctx, client = fake_client(post=post)
        with patch.object(gemini_models, "GEMINI_MODEL", "gemini-2.5-flash"), \
             patch.object(gemini_models, "GEMINI_FALLBACK_MODELS", ["gemini-3.8-flash", "gemini-flash-latest"]):
            async with ctx as c:
                r, model = await gemini_models.generate(c, {"contents": []}, {})
                assert r.status_code == 200 and model == "gemini-3.8-flash"
                # Second call goes straight to the working model.
                calls.clear()
                await gemini_models.generate(c, {"contents": []}, {})
                assert len(calls) == 1 and "gemini-3.8-flash" in calls[0]
            assert gemini_models.active_model() == "gemini-3.8-flash"

    async def test_quota_error_is_not_mistaken_for_retirement(self):
        async def post(url, headers=None, json=None):
            return FakeResponse(429, {"error": {"code": 429, "message": "quota"}})
        ctx, client = fake_client(post=post)
        with patch.object(gemini_models, "GEMINI_MODEL", "gemini-3.8-flash"), \
             patch.object(gemini_models, "GEMINI_FALLBACK_MODELS", ["gemini-3.8-flash", "gemini-flash-latest"]):
            async with ctx as c:
                r, _ = await gemini_models.generate(c, {"contents": []}, {})
            assert r.status_code == 429
            # Each model tried once; neither written off as retired.
            assert client.post.await_count == 2
            assert not gemini_models._dead
            assert gemini_models.candidates()  # still has models to try

    async def test_quota_on_one_model_falls_through_to_the_next(self):
        """Production 2026-10-28: gemini-3.8-flash answered 429 on every call."""
        calls = []

        async def post(url, headers=None, json=None):
            calls.append(url)
            if "gemini-3.8-flash" in url:
                return FakeResponse(429, {"error": {"code": 429, "message": "You exceeded your current quota"}})
            return FakeResponse(200, gemini_ok())
        ctx, _ = fake_client(post=post)
        with patch.object(gemini_models, "GEMINI_MODEL", "gemini-3.8-flash"), \
             patch.object(gemini_models, "GEMINI_FALLBACK_MODELS", ["gemini-3.8-flash", "gemini-flash-latest"]):
            async with ctx as c:
                r, model = await gemini_models.generate(c, {"contents": []}, {})
                assert r.status_code == 200 and model == "gemini-flash-latest"
                # While 3.8 cools down, the next call goes straight to the alias.
                calls.clear()
                await gemini_models.generate(c, {"contents": []}, {})
                assert len(calls) == 1 and "gemini-flash-latest" in calls[0]

    async def test_quota_message_is_plain_language(self):
        from evidence import grounded

        async def post(url, headers=None, json=None):
            return FakeResponse(429, {"error": {"code": 429, "message": "quota"}})
        ctx, _ = fake_client(post=post)
        with patch("evidence.grounded.httpx.AsyncClient", return_value=ctx), \
             patch("evidence.grounded.GEMINI_API_KEY", "k"):
            g = await grounded.ground_claim("x is y")
        assert not g.ok and "quota" in g.error

    def test_gen3_drops_low_temperature_and_sets_low_thinking(self):
        body = {"generationConfig": {"temperature": 0.0}}
        out = gemini_models.tune_body(body, "gemini-3.8-flash")
        assert "temperature" not in out["generationConfig"]
        assert out["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
        assert body == {"generationConfig": {"temperature": 0.0}}, "input must not be mutated"
        assert gemini_models.tune_body(body, "gemini-2.0-flash") is body

    async def test_rejected_thinking_config_is_retried_without_it(self):
        seen = []

        async def post(url, headers=None, json=None):
            seen.append(json)
            if "thinkingConfig" in (json.get("generationConfig") or {}):
                return FakeResponse(400, text='{"error": "Unknown name thinkingConfig"}')
            return FakeResponse(200, gemini_ok())
        ctx, _ = fake_client(post=post)
        async with ctx as c:
            r, _ = await gemini_models.generate(c, {"generationConfig": {}}, {})
        assert r.status_code == 200 and len(seen) == 2

    def test_response_text_joins_parts_and_skips_thoughts(self):
        payload = {"candidates": [{"content": {"parts": [
            {"text": "thinking...", "thought": True}, {"text": "VERDICT: "}, {"text": "SUPPORTED"}]}}]}
        assert gemini_models.response_text(payload) == "VERDICT: SUPPORTED"

    async def test_grounded_search_survives_a_retired_model(self):
        from evidence import grounded

        async def post(url, headers=None, json=None):
            if "gemini-2.5-flash" in url:
                return FakeResponse(404, RETIRED)
            return FakeResponse(200, gemini_ok())
        ctx, _ = fake_client(post=post)
        with patch("evidence.grounded.httpx.AsyncClient", return_value=ctx), \
             patch("evidence.grounded.GEMINI_API_KEY", "k"), \
             patch.object(gemini_models, "GEMINI_MODEL", "gemini-2.5-flash"):
            g = await grounded.ground_claim("The unemployment rate was 3.6% in 2023")
        assert g.ok and g.verdict == "SUPPORTED"
        assert g.search_suggestions_html == "<div>chips</div>"

    async def test_legacy_llm_client_survives_a_retired_model(self):
        import services.llm as llm

        async def post(url, headers=None, json=None):
            if "gemini-2.5-flash" in url:
                return FakeResponse(404, RETIRED)
            return FakeResponse(200, gemini_ok('{"ok": true}'))
        ctx, _ = fake_client(post=post)
        with patch("services.llm.httpx.AsyncClient", return_value=ctx), \
             patch("services.llm.GEMINI_API_KEY", "k"), \
             patch.object(gemini_models, "GEMINI_MODEL", "gemini-2.5-flash"):
            out = await llm.call_gemini("x")
        assert out["text"] == '{"ok": true}'


# ---------------------------------------------------------------- /verify bridge

class TestVerifyBridge:
    def _structured(self):
        from services.verify_v2 import VerificationV2
        return VerificationV2(
            claim="The unemployment rate in 2023 was 3.6%", verdict=SUPPORTED,
            confidence="High", headline="Claim says 3.6%. BLS reports 3.6% for 2023.",
            caveats=["Annual average computed from 12 months."],
            evidence=[{"label": "Unemployment rate", "value": 3.6, "value_display": "3.6%",
                       "unit": "percent", "period": "2023", "geography": "United States",
                       "source": "BLS", "source_url": "https://data.bls.gov/timeseries/LNS14000000",
                       "series_id": "LNS14000000"}],
            tier="structured", parsed={"metric": "unemployment_rate"},
        )

    def test_legacy_fields_the_old_popup_reads(self):
        from services.verify_compat import to_legacy_shape
        out = to_legacy_shape(self._structured())
        assert out["verdict"] == "Supported" and out["verdict_detail"] == "Supported"
        assert out["confidence"] == 0.9 and out["confidence_tier"] == "High"
        assert "BLS reports 3.6%" in out["summary"] and "Note:" in out["summary"]
        assert out["evidence_links"][0]["source_url"].startswith("https://data.bls.gov")
        assert out["sources"][0]["data_value"] == 3.6
        assert out["degraded"] is False and out["engine"] == "v2"
        # v2 detail rides along.
        assert out["tier"] == "structured" and out["model_calls"] == 0

    def test_no_verdict_maps_to_inconclusive_with_zero_confidence(self):
        from services.verify_compat import to_legacy_shape
        v = self._structured()
        v.verdict, v.reason, v.evidence = NO_VERDICT, "claim_is_not_empirically_testable", []
        out = to_legacy_shape(v)
        assert out["verdict"] == "Inconclusive" and out["verdict_detail"] == NO_VERDICT
        assert out["confidence"] == 0.0 and out["reason"] == "claim_is_not_empirically_testable"

    def test_grounding_outage_is_flagged_degraded(self):
        from services.verify_compat import to_legacy_shape
        from services.verify_v2 import VerificationV2
        v = VerificationV2(claim="x", verdict=NO_VERDICT, confidence="Low",
                           headline="Could not retrieve evidence for this claim (HTTP 503).",
                           reason="no_official_series_for_this_claim", tier="none", model_calls=1)
        assert to_legacy_shape(v)["degraded"] is True

    def test_verify_endpoint_uses_v2_engine(self, test_client):
        with patch("services.verify_compat.verify_v2", AsyncMock(return_value=self._structured())):
            r = test_client.post("/verify", json={"claim": "The unemployment rate in 2023 was 3.6%"})
        assert r.status_code == 200
        body = r.json()
        assert body["verdict"] == "Supported" and body["engine"] == "v2"

    def test_health_reports_model_and_sources(self, test_client):
        body = test_client.get("/health").json()
        assert body["llm_model"] and "gemini-2.5-flash" not in body["llm_model_candidates"]
        assert set(body["sources_configured"]) >= {"gemini", "bls", "bea", "census"}


# ---------------------------------------------------------------- review regressions
#
# Each case below was a confident wrong verdict found by the pre-PR review:
# the claim asserts something about a DIFFERENT number than the series holds.
# They must never be settled by the structured tier.

from claims.geography import resolve_geography
from claims.verdict import ALTERNATE_MEASURE


class TestGeographyReviewFixes:
    @pytest.mark.parametrize("text", [
        "Washington, D.C.'s poverty rate was 15% in 2023",
        "median household income in Washington DC was $101,000",
        "Median income in Washington D.C. was $101,000",
    ])
    def test_dc_is_not_washington_state(self, text):
        g = resolve_geography(text)
        assert g.fips == "11", g

    def test_washington_state_still_resolves(self):
        assert resolve_geography("Washington's population was 7.8 million").fips == "53"

    @pytest.mark.parametrize("text", [
        "The population of Paris is 2 million",
        "Chicago's population is 2.7 million in 2023",
        "The world population is 8 billion",
        "World GDP is $105 trillion",
        "London's GDP is $800 billion",
        "The EU's GDP is $18 trillion",
        "Global GDP grew 3% in 2023",
    ])
    def test_non_us_scopes_decline(self, text):
        g = resolve_geography(text)
        assert g is not None and not g.is_known, (text, g)

    @pytest.mark.parametrize("text", [
        "The unemployment rate was 3.6% in 2023, according to the Bureau of Labor Statistics",
        "America's GDP was $27 trillion in 2023",
        "The population of the United States was 335 million",
        "The Fed's preferred inflation measure rose 3%",
        "Texas's population was 30 million in 2023",
    ])
    def test_us_and_agency_mentions_are_not_foreign(self, text):
        g = resolve_geography(text)
        assert g is None or g.is_known, (text, g)


class TestQualifierBlockers:
    @pytest.mark.parametrize("claim", [
        "The Black population of the United States was 47 million in 2023",
        "In 2023 the median household income for Black households was $56,490",
        "The child poverty rate in 2023 was 13.7%",
        "The poverty rate for children under 18 was 13.7% in 2023",
        "Nominal GDP grew 6.6% in 2023",
        "Real GDP was $22.7 trillion in 2023",
        "GDP per capita was $82,000 in 2023",
        "Real median household income was $80,610 in 2023",
        "The unemployment rate for Black women was 5% in 2023",
        "Nominal GDP grew 6% in Q3 2023",
        "Personal income grew 5% in Q2 2024",
    ])
    def test_blocked_from_structured_tier(self, claim):
        from services.verify_v2 import _structured_is_applicable
        p = parse_claim(claim)
        assert p.metric is not None
        assert p.structured_blocker, claim
        assert _structured_is_applicable(p) is not None

    @pytest.mark.parametrize("claim", [
        "The population of Texas was under 5 million in 2023",
        "U.S. GDP was more than $25 trillion in 2023",
        "Real GDP grew 2.9% in 2023",
        "The national debt is over $33 trillion",
        "The unemployment rate for people 16 and older was 3.6% in 2023",
        "35% of adults 25 and older had a bachelor's degree or higher in 2023",
        "The White House said unemployment was 3.6% in 2023",
    ])
    def test_plain_totals_are_not_blocked(self, claim):
        assert parse_claim(claim).structured_blocker == "", claim

    @pytest.mark.parametrize("claim", [
        "GDP shrank 2.2% in 2020", "The economy contracted by 2.5% in 2022",
    ])
    def test_contraction_is_never_settled_unsigned(self, claim):
        """'shrank 2.2%' asserts -2.2%; the value parser reads it unsigned."""
        p = parse_claim(claim)
        assert p.metric is None or p.metric.metric != "real_gdp_growth"


@pytest.mark.asyncio
class TestReviewFixesEndToEnd:
    async def test_pre_2015_uninsured_and_education_route_away(self):
        from services.verify_v2 import _structured_is_applicable
        for claim in ("The uninsured rate in 2014 was 11.7%",
                      "In 2013, 30% of adults had a bachelor's degree or higher"):
            assert _structured_is_applicable(parse_claim(claim)) is not None, claim

    async def test_alternate_measure_softens_contradiction(self):
        from services.verify_v2 import verify
        ctx, _ = fake_client(get=census_get())
        with patch("httpx.AsyncClient", return_value=ctx):
            # $80,610 is the CPS figure Census headlines; ACS says $77,719.
            r = await verify("Median household income was $80,610 in 2023", allow_grounding=False)
        assert r.verdict == NO_VERDICT and r.reason == ALTERNATE_MEASURE
        assert r.evidence and r.evidence[0]["value"] == 77719

    async def test_missing_census_key_routes_to_grounding_not_outage(self, monkeypatch):
        import config
        from services.verify_v2 import verify
        monkeypatch.setattr(config, "CENSUS_API_KEY", None)
        r = await verify("U.S. population was about 335 million in 2023", allow_grounding=False)
        assert r.reason != "source_unavailable"
        assert r.tier == "none"

    async def test_grounding_exception_is_degraded_not_500(self, test_client):
        with patch("services.verify_v2.ai_search.search", AsyncMock(side_effect=ValueError("bad json"))):
            r = test_client.post("/verify", json={"claim": "Bananas cost $2 a pound in 2023"})
        assert r.status_code == 200
        body = r.json()
        assert body["degraded"] is True and body["degraded_reason"] == "llm_unavailable"

    async def test_non_json_grounding_body(self):
        from evidence import grounded

        async def post(url, headers=None, json=None):
            return FakeResponse(200, None, text="<html>proxy error</html>")
        ctx, _ = fake_client(post=post)
        with patch("evidence.grounded.httpx.AsyncClient", return_value=ctx), \
             patch("evidence.grounded.GEMINI_API_KEY", "k"):
            g = await grounded.ground_claim("x is y")
        assert not g.ok and "JSON" in g.error


@pytest.mark.parametrize("claim,value", [
    ("35% of adults 25 and older had a bachelor's degree or higher in 2023", 35.0),
    ("The unemployment rate for people 16 years and over was 3.6% in 2023", 3.6),
])
def test_universe_age_bound_is_not_a_second_value(claim, value):
    """Found live on the preview: '25 and older' made this a 'trend claim'."""
    p = parse_claim(claim)
    assert not p.is_trend_claim, p.all_values
    assert p.claimed_value.magnitude == value
