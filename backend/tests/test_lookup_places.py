"""Find a number: a place typed into the search box must be the place answered.

"Los angeles population" once returned the U.S. total. Every Census call is
faked here; nothing reaches an agency or a model.
"""
from unittest.mock import AsyncMock

import pytest

import config
import evidence.cache as cache_mod
from claims.parser import parse_claim
from evidence.datasets import census_profile as cp
from evidence.sources import SourceUnavailable
from services import lookup as lookup_mod
from services.lookup import PLACE_UNKNOWN, SOURCE_DOWN, _place_in_query, lookup

VAR = "DP05_0001E"
CATALOG = {VAR: {"label": "Estimate!!SEX AND AGE!!Total population", "table": "DP05"}}
PLACES = [["Los Angeles city, California", 3_820_000, "0644000"],
          ["Salem city, Oregon", 177_000, "4164900"],
          ["Salem city, Massachusetts", 44_000, "2559105"]]


@pytest.fixture(autouse=True)
def census(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "CENSUS_API_KEY", "k")
    monkeypatch.setattr(cp, "CENSUS_API_KEY", "k")

    async def catalog(year):
        return CATALOG

    async def places(year, level, survey="acs1", state=""):
        return PLACES if (level, survey) == ("place", "acs1") else []
    monkeypatch.setattr(cp, "catalog", catalog)
    monkeypatch.setattr(cp, "_places", places)

    async def national(_parsed):
        raise AssertionError("a place query must never fetch the national series")
    monkeypatch.setattr(lookup_mod, "fetch_structured", national)


def census_answer(name, fips):
    return AsyncMock(return_value=[
        ["NAME", VAR, cp._moe_var(VAR), "state", "place"],
        [name, "3820000", "-555555555", fips[:2], fips[2:]],
    ])


@pytest.mark.parametrize("query,name,level", [
    ("Los angeles population", "Los Angeles", "unknown"),
    ("population of los angeles", "Los Angeles", "unknown"),
    ("what's the population of seattle", "Seattle", "unknown"),
    ("salem oregon population", "Salem, Oregon", "place"),
    ("Austin Texas population", "Austin, Texas", "place"),
    ("unemployment rate austin tx", "Austin, Texas", "place"),
    ("median income Cook County", "Cook County", "county"),
    ("texas population", "Texas", "state"),
    ("new york population", "New York", "state"),
    ("washington state population", "Washington", "state"),
    ("US population", "United States", "nation"),
])
def test_the_place_is_found_wherever_it_sits(query, name, level):
    p = parse_claim(query)
    g = _place_in_query(query, p, p.metric)
    assert (g.name, g.level) == (name, level)


@pytest.mark.parametrize("query", ["population", "population 2023", "unemployment rate"])
def test_no_place_means_national(query):
    p = parse_claim(query)
    assert _place_in_query(query, p, p.metric) is None


@pytest.mark.asyncio
class TestLookup:
    async def test_lowercase_city_first_gets_the_city(self, monkeypatch):
        get = census_answer("Los Angeles city, California", "0644000")
        monkeypatch.setattr(cp, "_get_json", get)
        r = await lookup("Los angeles population")
        assert r.ok and r.model_calls == 0
        assert r.evidence["geography"] == "Los Angeles city, California"
        assert r.evidence["geography_level"] == "place"
        assert "Los Angeles" in r.citations["attribution"]
        params = get.call_args.args[1]
        assert params["for"] == "place:44000" and params["in"] == "state:06"

    async def test_city_then_state_is_that_states_city(self, monkeypatch):
        get = census_answer("Salem city, Oregon", "4164900")
        monkeypatch.setattr(cp, "_get_json", get)
        r = await lookup("salem oregon population")
        assert r.ok and r.evidence["geography"] == "Salem city, Oregon"

    async def test_unknown_place_is_said_never_swapped_for_the_nation(self):
        r = await lookup("population of Zzyzxville")
        assert not r.ok and r.reason == PLACE_UNKNOWN
        assert "Zzyzxville" in r.headline

    async def test_census_outage_is_an_outage_not_a_missing_place(self, monkeypatch):
        async def down(*a, **k):
            raise SourceUnavailable("Census", "timed out")
        monkeypatch.setattr(cp, "_places", down)
        r = await lookup("chicago population")
        assert not r.ok and r.reason == SOURCE_DOWN and r.degraded
