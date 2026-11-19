"""Federal contracts and grants from USAspending, as finder tools.

The case that asked for it: "On 31 October ICE also signed a new order with
CSI worth up to $1.28bn ..." was held against the previous year's order. The
new order (70CDCR27FC0000001) has a ceiling of exactly $1,280,000,000.
"""
import asyncio
import json

import pytest

from evidence.datasets import finder, usaspending

AWARD = "CONT_AWD_70CDCR27FC0000001_7012_70CDCR24A00000001_7012"
RECORD = {
    "piid": "70CDCR27FC0000001", "total_obligation": 277971276.0, "base_and_all_options": 1280000000.0,
    "total_outlay": None, "date_signed": "2026-10-31",
    "period_of_performance": {"start_date": "2026-11-01", "end_date": "2027-10-31"},
    "recipient": {"recipient_name": "CSI AVIATION, INC"},
    "awarding_agency": {"toptier_agency": {"name": "Department of Homeland Security"}},
}
FOUND = [{"generated_internal_id": AWARD, "Award ID": "70CDCR27FC0000001", "Recipient Name": "CSI AVIATION, INC",
          "Award Amount": 277971276.0, "Start Date": "2026-11-01", "End Date": "2027-10-31",
          "Awarding Agency": "Department of Homeland Security", "Description": "CHARTERED FLIGHT SERVICES"}]


@pytest.fixture
def api(monkeypatch, tmp_path):
    import evidence.cache as cache_mod
    monkeypatch.setattr(cache_mod, "_DIR", tmp_path, raising=False)
    calls = []

    async def call(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith("search/"):
            return {"results": FOUND}
        if path == f"awards/{AWARD}/":
            return RECORD
        return None

    async def no_cache(ns, key, ttl, load):
        return await load()
    monkeypatch.setattr(usaspending, "_call", call)
    monkeypatch.setattr(usaspending, "cached", no_cache)
    return calls


def test_search_sends_words_separately_and_the_signing_window(api):
    got = asyncio.run(usaspending.search("charter flights", "CSI Aviation", "Department of Homeland Security",
                                         "2026-10-24", "2026-11-07"))
    body = api[0][2]
    assert body["filters"]["keywords"] == ["charter", "flights"]   # one phrase missed "chartered flight"
    assert body["filters"]["recipient_search_text"] == ["CSI Aviation"]
    assert body["filters"]["time_period"][0] == {"start_date": "2026-10-24", "end_date": "2026-11-07",
                                                 "date_type": "date_signed"}
    assert got[0]["award"] == AWARD and got[0]["id"] == "70CDCR27FC0000001"


def test_search_needs_words_or_a_recipient(api):
    assert asyncio.run(usaspending.search("", "", "Department of Defense")) == [] and api == []


@pytest.mark.parametrize("measure,value", [("ceiling", 1.28e9), ("obligated", 277971276.0)])
def test_each_measure_is_its_own_named_figure(api, measure, value):
    dp = asyncio.run(usaspending.fetch(AWARD, measure))
    assert dp.quantity.magnitude == value
    assert dp.series_id == "70CDCR27FC0000001" and dp.source == "USASPENDING"
    assert usaspending.MEASURES[measure][1] in dp.label
    assert "signed 2026-10-31" in dp.observed.describe()
    assert dp.source_url == f"https://www.usaspending.gov/award/{AWARD}"


def test_a_figure_the_award_does_not_publish_is_none(api):
    assert asyncio.run(usaspending.fetch(AWARD, "outlays")) is None
    assert asyncio.run(usaspending.fetch(AWARD, "budget")) is None


def test_the_finder_reads_only_awards_its_search_returned(api):
    s = finder._Session("x")
    refused = asyncio.run(finder._usaspending_value(s, {"award": AWARD, "measure": "ceiling"}))
    assert "error" in refused
    asyncio.run(finder._usaspending_search(s, {"words": "charter flights", "recipient": "CSI Aviation"}))
    shown = asyncio.run(finder._usaspending_value(s, {"award": AWARD, "measure": "ceiling"}))
    assert shown["value"] == "$1.28 billion" and shown["id"] == "D1"


def test_a_cached_plan_replays_without_a_search(api):
    dp = asyncio.run(finder.replay({"tool": "usaspending_value", "input": {"award": AWARD, "measure": "ceiling"}},
                                   "x"))
    assert dp is not None and dp.quantity.magnitude == 1.28e9


def test_the_finder_is_told_how_to_pick_the_award_and_the_measure():
    names = [t["name"] for t in finder._tools()]
    assert "usaspending_search" in names and "usaspending_value" in names
    assert "never last year's" in finder._SYSTEM and '"ceiling" for "worth up to"' in finder._SYSTEM


def test_a_change_to_the_tools_starts_a_fresh_plan_cache(monkeypatch):
    before = finder._rules_version()
    monkeypatch.setattr(finder, "_SYSTEM", finder._SYSTEM + " One more rule.")
    assert finder._rules_version() != before
