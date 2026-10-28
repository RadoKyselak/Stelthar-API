"""The `display` block every client renders, the web page, and coverage.

The extension panel, its popup and the web page all draw `display`, so its
wording and its safety rules (only http(s) links, nothing executable) are
tested here once rather than in three front ends.
"""
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

import config
import evidence.cache as cache_mod
from evidence.datapoint import ANNUAL, Datapoint, Geography, Period
from evidence.grounded import Citation, GroundedAnswer
from evidence.sources import bls as bls_source
from evidence.units import PERCENT, Quantity, humanize_claim, parse_quantity
from services.presentation import build_display
from services.verify_v2 import to_response_dict, verify

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEXAS = "LAUST480000000000003"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path / "cache")


def fake_bls(table):
    async def _fetch_raw(series_id, start, end):
        rows = []
        for y in range(end, start - 1, -1):
            for m, v in sorted(enumerate(table.get(series_id, {}).get(y) or [], start=1), reverse=True):
                rows.append({"year": str(y), "period": f"M{m:02d}", "periodName": "", "value": str(v)})
        return rows
    return _fetch_raw


async def structured(claim, table):
    with patch.object(bls_source, "_fetch_raw", fake_bls(table)):
        return await verify(claim, allow_grounding=False)


@pytest.mark.asyncio
class TestStructuredDisplay:
    async def test_near_miss(self):
        v = await structured("Unemployment in Texas was 4.1% in 2023", {TEXAS: {2023: [4.0] * 12}})
        d = to_response_dict(v)["display"]
        assert d["verdict"] == {"key": "mostly-accurate", "label": "Mostly accurate",
                                "meaning": "Close to the official figure, but not exact."}
        [c] = d["comparisons"]
        assert c["claim"] == "4.1%" and c["official"].startswith("4")
        assert c["note"] == "Off by 0.1 percentage point." and "Texas" in c["context"]
        [s] = d["sources"]
        assert s["agency"] == "U.S. Bureau of Labor Statistics"
        assert s["dataset"] == "Local Area Unemployment Statistics"
        assert s["identifier"] == f"series {TEXAS}" and s["url"].startswith("https://data.bls.gov/")
        assert d["method"]["kind"] == "official_data" and "No AI was used" in d["method"]["text"]
        assert d["confidence"]["tier"] in ("High", "Medium") and d["confidence"]["text"]
        assert TEXAS in d["share_text"] and "Mostly accurate" in d["share_text"]
        cite = d["citation"]
        for part in ("U.S. Bureau of Labor Statistics", "\"Unemployment rate,\"",
                     "Local Area Unemployment Statistics", f"series {TEXAS}", "Texas", "4.0%.",
                     f"https://data.bls.gov/timeseries/{TEXAS}", "(accessed "):
            assert part in cite, part
        assert d["status"] == "ok" and d["reason"] is None

    async def test_two_point_has_one_comparison_per_year(self):
        v = await structured("The unemployment rate fell from 8.1% in 2020 to 3.6% in 2023",
                             {"LNU04000000": {2020: [8.1] * 12, 2023: [3.6] * 12}})
        d = build_display(v)
        assert [c["label"] for c in d["comparisons"]] == ["2020", "2023"]
        assert all(c["note"] == "Exact match." for c in d["comparisons"])

    async def test_threshold_claim(self):
        v = await structured("Unemployment in Texas was over 5% in 2023", {TEXAS: {2023: [4.0] * 12}})
        [c] = build_display(v)["comparisons"]
        assert c["claim"] == "more than 5%"
        assert c["note"] == "4.0% is not more than 5%."

    async def test_no_verdict_says_what_to_try(self):
        v = await verify("Unemployment in Chicago was 5% in 2023", allow_grounding=False)
        d = build_display(v)
        assert d["verdict"]["key"] == "no-verdict"
        assert d["reason"]["next"] and d["confidence"]["tier"] is None
        assert "wired-up" not in d["summary"]

    async def test_outage_is_not_a_verdict(self):
        from evidence.sources import SourceUnavailable
        boom = AsyncMock(side_effect=SourceUnavailable("BLS", "timeout"))
        with patch.object(bls_source, "_fetch_raw", boom):
            v = await verify("Unemployment was 3.6% in 2023", allow_grounding=False)
        d = build_display(v)
        assert d["status"] == "unavailable" and d["verdict"]["label"] == "Couldn't check"


@pytest.mark.asyncio
class TestWebSearchDisplay:
    async def test_sources_and_link_safety(self):
        g = GroundedAnswer(
            ok=True, verdict="SUPPORTED", official_value="7.3%", period="2023", source_org="INSEE",
            explanation="INSEE reports 7.3%.", model_calls=1, model="claude-haiku-5-5",
            citations=[
                Citation(title="INSEE", uri="https://www.insee.fr/en/x", domain="www.insee.fr", official=True),
                Citation(title="Blog", uri="https://example.com/a", domain="example.com"),
                Citation(title="Bad", uri="javascript:alert(1)", domain="evil"),
            ],
        )
        with patch("services.verify_v2.ai_search.search", AsyncMock(return_value=g)):
            v = await verify("The unemployment rate in France was 7.3% in 2023")
        d = build_display(v)
        assert [s["domain"] for s in d["sources"]] == ["insee.fr", "example.com"]
        assert all(s["url"].startswith("https://") for s in d["sources"])
        assert d["method"]["kind"] == "web_search" and "(1 official)" in d["method"]["text"]
        assert d["reported_value"] == "7.3%" and d["reported_context"] == "2023 \u00b7 INSEE"
        assert d["summary"] == "INSEE reports 7.3%."


class TestFormatting:
    @pytest.mark.parametrize("text,shown", [
        ("$7 trillion", "$7 trillion"), ("$1.8 trillion", "$1.8 trillion"), ("4.0%", "4.0%"),
        ("9%", "9%"), ("$4.82", "$4.82"), ("17 cents", "$0.17"), ("$73,000", "$73,000"),
    ])
    def test_claims_keep_their_written_precision(self, text, shown):
        q = parse_quantity(text)
        assert humanize_claim(q.magnitude, q.family, q.precision) == shown

    def test_rates_keep_the_agency_precision(self):
        dp = Datapoint(quantity=Quantity(4.0, PERCENT), metric="unemployment_rate", label="x",
                       observed=Period(2023, ANNUAL), geography=Geography.national(),
                       source="BLS", source_url="https://x", raw_value="4.0")
        assert dp.display_value() == "4.0%"
        assert dp.to_dict()["period_label"] == "2023"

    def test_treasury_debt_links_to_a_readable_page(self):
        from services.presentation import _data_source
        s = _data_source({"source": "TREASURY", "series_id": "debt_to_penny", "label": "Debt",
                          "source_url": "https://api.fiscaldata.treasury.gov/services/api/x",
                          "period_kind": "point_in_time", "as_of": "2026-10-27",
                          "period_basis": "daily figure, as of 2026-10-27"})
        assert s["url"].startswith("https://fiscaldata.treasury.gov/datasets/debt-to-the-penny")
        assert s["period"] == "October 27, 2026" and s["basis"] == "daily figure"
        assert s["dataset"] == "Debt to the Penny" and s["identifier"] == ""


class TestWebPage:
    def test_browser_gets_the_page_with_a_strict_policy(self, test_client):
        r = test_client.get("/", headers={"accept": "text/html"})
        assert r.status_code == 200 and "Project Mirador" in r.text
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp

    def test_api_clients_still_get_json(self, test_client):
        assert test_client.get("/").json()["status"] == "ok"

    @pytest.mark.parametrize("name", ["app.js", "result-view.js", "site.css", "logo.png", "favicon.png"])
    def test_static_files(self, test_client, name):
        r = test_client.get(f"/static/{name}")
        assert r.status_code == 200 and r.headers["x-content-type-options"] == "nosniff"

    @pytest.mark.parametrize("name", ["index.html", "..%2Fmain.py", "nope.js"])
    def test_nothing_else_is_served(self, test_client, name):
        assert test_client.get(f"/static/{name}").status_code == 404

    def test_extension_and_site_render_with_the_same_code(self):
        a = (ROOT / "extension" / "result-view.js").read_bytes()
        b = (ROOT / "backend" / "static" / "result-view.js").read_bytes()
        assert a == b, "copy extension/result-view.js to backend/static/ after editing it"

    def test_renderer_never_injects_html(self):
        import re
        src = (ROOT / "extension" / "result-view.js").read_text()
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", src, flags=re.S)  # ignore comments
        assert not re.search(r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write", code)

    def test_coverage(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "BEA_API_KEY", "k")
        monkeypatch.setattr(config, "CENSUS_API_KEY", "k")
        r = test_client.get("/v2/coverage").json()
        sources = [a["source"] for a in r["agencies"]]
        assert sources == ["BLS", "BEA", "CENSUS", "TREASURY"]
        bls = r["agencies"][0]["metrics"]
        unemp = next(m for m in bls if m["metric"] == "unemployment_rate")
        assert "Black" in unemp["groups"] and unemp["places"] == "U.S. and every state"
        assert r["metric_count"] == sum(len(a["metrics"]) for a in r["agencies"])


class TestCitations:
    def _dp(self, **kw):
        from evidence.datapoint import MONTHLY, POINT_IN_TIME, FISCAL_YEAR
        base = dict(quantity=Quantity(9.1, PERCENT), metric="inflation_rate", label="CPI-U inflation rate",
                    observed=Period(2022, MONTHLY, month=6,
                                    basis="12-month change from June 2021, not seasonally adjusted"),
                    geography=Geography.national(), source="BLS",
                    source_url="https://data.bls.gov/timeseries/CUUR0000SA0", series_id="CUUR0000SA0",
                    raw_value="9.1")
        base.update(kw)
        return Datapoint(**base)

    def test_inflation_cites_the_published_figure_without_codes(self):
        from datetime import date
        from evidence.citation import render
        c = render(self._dp(), accessed=date(2026, 10, 28))
        assert c["note"] == ('U.S. Bureau of Labor Statistics, "CPI-U inflation rate," Consumer Price '
                             "Index, series CUUR0000SA0, United States, June 2022 (12-month change from "
                             "June 2021, not seasonally adjusted): 9.1%. "
                             "https://data.bls.gov/timeseries/CUUR0000SA0 (accessed October 28, 2026).")
        assert c["inline"] == "9.1% (BLS, June 2022)"
        assert "M06" not in c["note"]

    def test_treasury_and_census_identifiers(self):
        from evidence.citation import identifier, dataset_name
        assert identifier("TREASURY", "MTS Table 1") == "Table 1"
        assert dataset_name("TREASURY", "MTS Table 1") == "Monthly Treasury Statement"
        assert identifier("TREASURY", "debt_to_penny") == ""
        assert identifier("CENSUS", "S1701_C03_001E") == "table S1701"

    def test_basis_cleanup(self):
        from evidence.citation import clean_basis
        assert clean_basis("BLS published annual average") == "annual average"
        assert clean_basis("January monthly observation") == ""
        assert clean_basis("latest monthly observation") == "latest month"
        assert clean_basis("daily figure, as of 2026-10-27") == "daily figure"


class TestAuditFixes:
    """Findings from a line-by-line audit against agency publications, 2026-10-28."""

    def test_annual_figures_come_from_the_series_bls_publishes_averages_for(self):
        from evidence.sources.bls import annual_series_for
        assert annual_series_for("LNS14000000") == "LNU04000000"
        assert annual_series_for("LNS11300000") == "LNU01300000"   # participation
        assert annual_series_for("CES0000000001") == "CEU0000000001"  # payrolls
        assert annual_series_for("CES0500000003") == "CEU0500000003"  # hourly earnings
        assert annual_series_for("LAUST480000000000003") == "LAUST480000000000003"

    def test_bls_links_open_on_the_cited_figure(self):
        from evidence.sources.bls import series_permalink
        assert series_permalink("LNU04000000", annual=True, year=2023).endswith(
            "LNU04000000?annualAveragesRequested=true")
        assert "output_view=pct_12mths" in series_permalink("CUUR0000SA0", pct12=True, year=2022)
        assert "from_year=2008&to_year=2009" in series_permalink("LNU04000000", annual=True, year=2009)

    @pytest.mark.parametrize("raw,family,shown", [
        ("4.0", PERCENT, "4.0%"), ("4.823", "usd", "$4.823"), ("304.702", "index", "304.702"),
        ("0.168", "usd", "$0.168"),
    ])
    def test_figures_keep_the_published_digits(self, raw, family, shown):
        dp = Datapoint(quantity=Quantity(float(raw), family), metric="m", label="x",
                       observed=Period(2023, ANNUAL), geography=Geography.national(),
                       source="BLS", source_url="https://x", raw_value=raw)
        assert dp.display_value() == shown

    def test_identifiers_name_the_table_and_statement(self):
        from evidence.citation import identifier
        assert identifier("BEA", "A191RC", table="T10105") == "NIPA Table 1.1.5, series A191RC"
        assert identifier("TREASURY", "MTS Table 1", as_of="2024-09-30") == \
            "September 2024 statement, Table 1"

    def test_bea_links_open_the_table(self):
        from evidence.sources.bea import table_permalink
        assert "nipa_table_list=5" in table_permalink("T10105")
        assert "nipa_table_list=1" in table_permalink("T10101")
        assert "nipa_table_list=58" in table_permalink("T20100")
