"""The Energy Information Administration's series (evidence/datasets/eia.py).

EconWebArena asks for California gasoline by the week, the WTI and Henry Hub
spot prices by the day, and U.S. generation by fuel for a year. None was
reachable before. The network is replaced throughout.
"""
import pytest

from evidence.datasets import Candidate, eia

CATALOG = [
    {"id": "PET.EMM_EPMR_PTE_SCA_DPG", "title": "California Regular All Formulations Retail Gasoline Prices (Dollars per Gallon)",
     "frequency": "weekly, monthly, annual", "route": "petroleum/pri/gnd"},
    {"id": "PET.EMM_EPMR_PTE_NUS_DPG", "title": "U.S. Regular All Formulations Retail Gasoline Prices (Dollars per Gallon)",
     "frequency": "weekly, monthly, annual", "route": "petroleum/pri/gnd"},
    {"id": "PET.EMM_EPMR_PTE_R5XCA_DPG",
     "title": "West Coast (PADD 5) Except California Regular All Formulations Retail Gasoline Prices (Dollars per Gallon)",
     "frequency": "weekly, monthly, annual", "route": "petroleum/pri/gnd"},
    {"id": "PET.RWTC", "title": "Cushing, OK WTI Spot Price FOB (Dollars per Barrel)",
     "frequency": "daily, weekly, monthly, annual", "route": "petroleum/pri/spt"},
    {"id": "ELEC.GEN.COW-US-99", "title": "Net electricity generation, all coal products, U.S. Total, all sectors (thousand megawatthours)",
     "frequency": "monthly, annual", "route": eia._GENERATION},
    {"id": "ELEC.GEN.RC-PAC-99", "title": "Net electricity generation, refined coal, Pacific, all sectors (thousand megawatthours)",
     "frequency": "monthly, annual", "route": eia._GENERATION},
    {"id": "ELEC.GEN.WND-TX-99", "title": "Net electricity generation, wind, Texas, all sectors (thousand megawatthours)",
     "frequency": "monthly, annual", "route": eia._GENERATION},
]


@pytest.fixture
def catalog(monkeypatch):
    async def fake():
        return CATALOG
    monkeypatch.setattr(eia, "_catalog", fake)


class TestSearch:
    @pytest.mark.parametrize("words, first", [
        ("California regular gasoline retail", "PET.EMM_EPMR_PTE_SCA_DPG"),
        ("regular gasoline retail price", "PET.EMM_EPMR_PTE_NUS_DPG"),          # no place: the nation's
        ("net generation coal U.S.", "ELEC.GEN.COW-US-99"),                     # not refined coal in the Pacific
        ("Texas wind generation", "ELEC.GEN.WND-TX-99"),
        ("WTI spot price Cushing", "PET.RWTC"),
    ])
    async def test_the_place_decides_before_the_words(self, catalog, words, first):
        got = await eia.candidates(words)
        assert got and got[0].id == first

    async def test_a_state_asked_for_gets_only_that_states_series(self, catalog):
        got = await eia.candidates("California regular gasoline retail")
        assert all("California" in c.title and not c.title.startswith("West Coast") for c in got[:1])

    async def test_nothing_that_matches_is_nothing(self, catalog):
        assert await eia.candidates("unemployment rate") == []


def candidate(sid):
    s = next(x for x in CATALOG if x["id"] == sid)
    return Candidate(provider="eia", id=s["id"], title=s["title"], units="", frequency=s["frequency"],
                     meta={"route": s["route"]})


@pytest.fixture
def api(monkeypatch):
    asked = []
    answers = {}

    async def get(path, params):
        asked.append((path, params))
        return {"data": answers.get(path, [])}

    async def straight(kind, key, ttl, load):
        return await load()
    monkeypatch.setattr(eia, "_get", get)
    monkeypatch.setattr(eia, "cached", straight)
    return asked, answers


class TestFetch:
    async def test_one_week_of_a_weekly_price(self, api):
        asked, answers = api
        answers["petroleum/pri/gnd/data/"] = [{"period": "2025-03-24", "value": "4.418", "units": "$/GAL",
                                               "series-description": CATALOG[0]["title"]}]
        dp = await eia.fetch(candidate("PET.EMM_EPMR_PTE_SCA_DPG"), 2025, 3, 24)
        path, params = asked[0]
        assert path == "petroleum/pri/gnd/data/" and params["frequency"] == "weekly"
        assert params["start"] == "2025-03-18" and params["end"] == "2025-03-24"
        assert params["facets[series][]"] == "EMM_EPMR_PTE_SCA_DPG"
        assert dp.display_value() == "$4.418" and dp.geography.name == "California"
        assert dp.observed.record_date == "2025-03-24"

    async def test_one_day_of_a_daily_price(self, api):
        asked, answers = api
        answers["petroleum/pri/spt/data/"] = [{"period": "2025-03-10", "value": "66.31", "units": "$/BBL",
                                               "series-description": CATALOG[3]["title"]}]
        dp = await eia.fetch(candidate("PET.RWTC"), 2025, 3, 10)
        assert asked[0][1]["frequency"] == "daily" and asked[0][1]["start"] == "2025-03-10"
        assert dp.quantity.magnitude == 66.31 and dp.quantity.family == "usd"

    async def test_an_answer_for_another_period_is_no_answer(self, api):
        """The old-style address ignores dates and answers with the newest
        week. A price for November 2026 is not March 2025's."""
        _, answers = api
        answers["petroleum/pri/gnd/data/"] = [{"period": "2026-11-16", "value": "5.387", "units": "$/GAL"}]
        assert await eia.fetch(candidate("PET.EMM_EPMR_PTE_SCA_DPG"), 2025, 3, 24) is None

    async def test_generation_is_read_from_its_own_table(self, api):
        asked, answers = api
        answers[eia._GENERATION + "/data/"] = [{"period": "2024", "generation": "652155.92",
                                                "generation-units": "thousand megawatthours",
                                                "fuelTypeDescription": "all coal products",
                                                "stateDescription": "U.S. Total"}]
        dp = await eia.fetch(candidate("ELEC.GEN.COW-US-99"), 2024, None)
        params = asked[0][1]
        assert params["facets[fueltypeid][]"] == "COW" and params["facets[location][]"] == "US"
        assert params["facets[sectorid][]"] == "99" and params["frequency"] == "annual"
        assert dp.quantity.magnitude == 652155.92 and dp.geography.level == "nation"

    async def test_a_frequency_the_series_lacks(self, api):
        """Generation is not published by the day."""
        assert await eia.fetch(candidate("ELEC.GEN.COW-US-99"), 2024, 3, 10) is None


class TestTheLookupStep:
    def test_the_tools_are_offered_and_their_use_explained(self):
        from evidence.datasets.finder import _SYSTEM, _tools
        names = [t["name"] for t in _tools()]
        assert "eia_search" in names and "eia_value" in names
        assert "eia_search" in _SYSTEM

    def test_the_agency_is_named_in_citations(self):
        from evidence.citation import agency_name, agency_short
        assert agency_name("EIA") == "U.S. Energy Information Administration" and agency_short("EIA") == "EIA"
