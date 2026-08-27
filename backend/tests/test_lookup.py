"""Tests for the retrieval path: /v2/lookup and the citation renderer.

The rule these lock down is the one the citation exists to serve: a figure the
agency did not publish, standing in for one it did, must say so. Dropping the
basis is the December-print-as-annual-average bug, reintroduced at the point
where it does the most damage — inside someone else's published work.
"""
from datetime import date

import pytest

from evidence.citation import agency_name, agency_short, render
from evidence.datapoint import (
    ANNUAL, MONTHLY, POINT_IN_TIME, Datapoint, Geography, Period,
)
from evidence.sources import SourceUnavailable
from evidence.units import PERCENT, USD, Quantity
from services import lookup as lookup_mod
from services.lookup import (
    AMBIGUOUS, METRIC_NOT_WIRED, METRIC_UNKNOWN, PERIOD_UNAVAILABLE,
    SOURCE_DOWN, GEOGRAPHY_UNSUPPORTED, _query_stem, lookup,
)

ACCESSED = date(2026, 8, 27)


def _unemployment(basis="annual average", value=3.6, year=2023):
    return Datapoint(
        quantity=Quantity(magnitude=value, family=PERCENT),
        metric="unemployment_rate", label="Unemployment rate",
        observed=Period(year=year, kind=ANNUAL, basis=basis),
        geography=Geography.national(), source="BLS",
        source_url="https://data.bls.gov/timeseries/LNS14000000",
        series_id="LNS14000000",
    )


class TestCitationCarriesTheBasis:
    def test_short_basis_stays_inline(self):
        c = render(_unemployment("annual average"), accessed=ACCESSED)
        assert c["inline"] == "3.6% (BLS, 2023 annual average)"
        assert c["caveat"] == ""

    def test_long_basis_moves_to_a_caveat_and_is_never_dropped(self):
        basis = "computed as the mean of 12 monthly observations, to 1 dp"
        c = render(_unemployment(basis), accessed=ACCESSED)
        # Too long to sit inside "3.6% (…)" — but it must survive somewhere.
        assert c["inline"] == "3.6% (BLS, 2023)"
        assert c["caveat"] == basis
        assert basis in c["note"]

    def test_note_carries_agency_series_period_and_access_date(self):
        c = render(_unemployment(), accessed=ACCESSED)
        for fragment in ("U.S. Bureau of Labor Statistics", "LNS14000000",
                         "United States", "2023", "3.6%", "accessed 27 August 2026"):
            assert fragment in c["note"], fragment

    def test_html_form_is_escaped_and_linked(self):
        c = render(_unemployment(), accessed=ACCESSED)
        assert c["html"].startswith("3.6% (<a href=")
        assert "data.bls.gov" in c["html"]

    def test_nested_parentheses_never_appear_inline(self):
        """'3.7% (BLS, 2019 (annual average))' is unreadable in prose."""
        c = render(_unemployment("annual average"), accessed=ACCESSED)
        assert "((" not in c["inline"] and "))" not in c["inline"]

    def test_national_geography_is_omitted_inline_but_kept_in_the_note(self):
        c = render(_unemployment(), accessed=ACCESSED)
        assert "United States" not in c["inline"]
        assert "United States" in c["note"]

    def test_state_geography_is_kept_inline(self):
        dp = Datapoint(
            quantity=Quantity(magnitude=73035, family=USD),
            metric="median_household_income", label="Median household income",
            observed=Period(year=2022, kind=ANNUAL, basis="ACS 1-year"),
            geography=Geography(name="Texas", level="state", fips="48"),
            source="CENSUS", source_url="https://api.census.gov/data/2022/acs/acs1",
        )
        assert "Texas" in render(dp, accessed=ACCESSED)["inline"]

    def test_point_in_time_reads_as_of(self):
        dp = Datapoint(
            quantity=Quantity(magnitude=34.0e12, family=USD),
            metric="national_debt", label="Total public debt outstanding",
            observed=Period(year=2023, kind=POINT_IN_TIME, record_date="2023-12-29"),
            geography=Geography.national(), source="TREASURY",
            source_url="https://fiscaldata.treasury.gov/",
        )
        assert "as of 2023-12-29" in render(dp, accessed=ACCESSED)["inline"]

    def test_agency_names(self):
        assert agency_name("BLS") == "U.S. Bureau of Labor Statistics"
        assert agency_short("TREASURY") == "Treasury"
        assert agency_short("BLS") == "BLS"


class TestQueryStem:
    @pytest.mark.parametrize("query,stem", [
        ("debt 2023", "debt"), ("jobs 2023", "jobs"), ("cpi 2022", "cpi"),
        ("the unemployment rate in 2023", "unemployment"), ("wages", "wages"),
    ])
    def test_period_and_filler_words_are_stripped(self, query, stem):
        assert _query_stem(query) == stem


@pytest.mark.asyncio
class TestLookupDeclines:
    """Every decline names a different fix. 'No data' covering an outage is the
    failure this codebase already paid for once."""

    async def test_unrecognised_metric_suggests_what_is_available(self):
        r = await lookup("how many cats live in ohio")
        assert not r.ok and r.reason == METRIC_UNKNOWN
        assert r.suggestions, "a decline with no suggestion is a dead end"

    async def test_unwired_source_says_so_rather_than_no_data(self):
        r = await lookup("gdp 2023")
        assert not r.ok and r.reason == METRIC_NOT_WIRED
        assert "BEA" in r.headline

    async def test_geography_below_series_level(self):
        r = await lookup("unemployment in Ohio 2023")
        assert not r.ok and r.reason == GEOGRAPHY_UNSUPPORTED

    async def test_future_year(self):
        r = await lookup("unemployment 2031")
        assert not r.ok and r.reason == PERIOD_UNAVAILABLE

    async def test_ambiguous_query_asks_instead_of_guessing(self):
        r = await lookup("cpi 2022")
        assert not r.ok and r.reason == AMBIGUOUS
        assert {s["metric"] for s in r.suggestions} == {"inflation_rate", "cpi_index"}

    async def test_outage_is_not_reported_as_missing_data(self, monkeypatch):
        async def boom(_parsed):
            raise SourceUnavailable("BLS", "daily threshold reached")
        monkeypatch.setattr(lookup_mod, "fetch_structured", boom)
        r = await lookup("unemployment 2023")
        assert not r.ok
        assert r.reason == SOURCE_DOWN and r.degraded is True
        assert r.reason != PERIOD_UNAVAILABLE
        assert "not a statement about the data" in r.headline

    async def test_empty_result_is_period_unavailable_not_an_outage(self, monkeypatch):
        async def none(_parsed):
            return []
        monkeypatch.setattr(lookup_mod, "fetch_structured", none)
        r = await lookup("unemployment 2023")
        assert not r.ok and r.reason == PERIOD_UNAVAILABLE and r.degraded is False


@pytest.mark.asyncio
class TestLookupSucceeds:
    async def test_returns_every_citation_form(self, monkeypatch):
        async def one(_parsed):
            return [_unemployment()]
        monkeypatch.setattr(lookup_mod, "fetch_structured", one)
        r = await lookup("unemployment 2023")
        assert r.ok and r.value == "3.6%"
        for form in ("value", "inline", "markdown", "html", "note"):
            assert r.citations[form]
        assert r.evidence["series_id"] == "LNS14000000"
        assert r.model_calls == 0, "a lookup must never cost a model call"

    async def test_query_alias_resolves(self, monkeypatch):
        captured = {}

        async def cap(parsed):
            captured["metric"] = parsed.metric.metric
            return [_unemployment()]
        monkeypatch.setattr(lookup_mod, "fetch_structured", cap)
        await lookup("jobs 2023")
        assert captured["metric"] == "nonfarm_employment"


class TestLookupEndpoint:
    """The wiring, not the logic — that a real request reaches the service and
    the response shape the extension depends on survives serialisation."""

    def test_short_query_is_accepted(self, test_client, monkeypatch):
        async def one(_parsed):
            return [_unemployment()]
        monkeypatch.setattr(lookup_mod, "fetch_structured", one)

        response = test_client.post("/v2/lookup", json={"query": "unemployment 2023"})
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["value"] == "3.6%"
        assert body["citations"]["note"]
        assert body["model_calls"] == 0

    def test_one_character_query_is_rejected(self, test_client):
        assert test_client.post("/v2/lookup", json={"query": "u"}).status_code == 422

    def test_miss_returns_suggestions_not_an_error(self, test_client):
        response = test_client.post("/v2/lookup", json={"query": "how many cats in ohio"})
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["suggestions"]
