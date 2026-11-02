"""Computed checks (services/derived.py) and the three benchmark slips they
came with: a city named without its state, a hedge that covered anything, and
a 5-year Census vintage asked for before its release."""
from datetime import date

import pytest

from claims.parser import parse_claim
from claims.verdict import CONTRADICTED, MISLEADING, MOSTLY_ACCURATE, NO_VERDICT, SUPPORTED, assess
from evidence.datapoint import ANNUAL, Datapoint, Geography, Period
from evidence.units import PERSONS, Quantity
from services import derived
from services.verify_v2 import verify

_REAL_DEBT_CHECK = derived._debt_held_by_public  # read before the offline fixture swaps it


def series(table):
    """A fake _rows: {(sid, vintage or None): [(date, value)]}, filtered by window."""
    async def rows(s, start=None, end=None, vintage=None):
        data = table.get((s.sid, vintage)) or table.get((s.sid, "any"), [])
        return [(d, v) for d, v in data
                if (start is None or d >= start) and (end is None or d <= end)]
    return rows


class TestSlips:
    def test_city_named_without_its_state_is_a_place(self):
        p = parse_claim("Nashville has about 720,000 people.")
        assert p.geography is not None and p.geography.level == "place"
        assert p.geography.name == "Nashville"

    @pytest.mark.parametrize("claim", [
        "Biden has added 15 million jobs.",
        "Texas has 31 million people.",
        "America has 340 million people.",
        "Median household income was about $87,500 in 2025.",
    ])
    def test_subjects_that_are_not_cities(self, claim):
        g = parse_claim(claim).geography
        assert g is None or g.level != "place"

    def test_a_hedge_does_not_cover_a_figure_far_off(self):
        p = parse_claim("Boise, Idaho has a population of about 400,000.")
        dp = Datapoint(quantity=Quantity(238422, PERSONS), metric="population",
                       label="Total population", observed=Period(2025, ANNUAL),
                       geography=Geography("Boise City city, Idaho", "place", "1608830"),
                       source="CENSUS", source_url="x")
        assert assess(p, [dp]).verdict == CONTRADICTED

    def test_a_hedge_still_allows_a_close_round_figure(self):
        p = parse_claim("Nashville has about 720,000 people.")
        dp = Datapoint(quantity=Quantity(704963, PERSONS), metric="population",
                       label="Total population", observed=Period(2025, ANNUAL),
                       geography=Geography("Nashville", "place", "4752006"),
                       source="CENSUS", source_url="x")
        assert assess(p, [dp]).verdict == SUPPORTED

    async def test_five_year_survey_falls_back_to_the_released_vintage(self, monkeypatch, tmp_path):
        from evidence import cache as cache_mod
        from evidence.datasets import census_profile as cp
        monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
        asked = []

        async def get(url, params):
            asked.append(url)
            return {"variables": {"x": {}}} if "/2024/" in url else None
        monkeypatch.setattr(cp, "_get_json", get)
        assert await cp.published_year(2025, "acs5") == 2024
        assert await cp.published_year(2025, "acs1") == 2025
        assert any("/2025/acs/acs5/" in u for u in asked)


GDP_Q1 = [(date(2026, 1, 1), 2.5)]
PAY = [(date(2026, 6, 1), 158892.0), (date(2026, 7, 1), 158882.0)]
PAY_FIRST = [(date(2026, 6, 1), 158881.0), (date(2026, 7, 1), 158858.0)]


class TestDirection:
    async def test_economy_did_not_shrink(self, monkeypatch):
        monkeypatch.setattr(derived, "_rows", series({("A191RL1Q225SBEA", "any"): GDP_Q1}))
        r = await verify("The U.S. economy shrank in the first quarter of 2026.", allow_grounding=False)
        assert r.verdict == CONTRADICTED and r.model_calls == 0 and "2.5%" in r.headline

    async def test_lost_jobs_with_first_print(self, monkeypatch):
        monkeypatch.setattr(derived, "_rows", series({("PAYEMS", None): PAY, ("PAYEMS", "any"): PAY_FIRST}))
        r = await verify("The U.S. lost jobs in July 2026.", allow_grounding=False)
        assert r.verdict == SUPPORTED and r.model_calls == 0
        assert any("First reported as -23k" in c for c in r.caveats)

    async def test_a_figure_is_left_to_the_normal_check(self):
        assert await derived._direction("The economy added 250,000 jobs in September 2026.") is None


class TestChangeSince:
    async def test_prices_since_a_month(self, monkeypatch):
        cpi = [(date(2021, 1, 1), 262.687), (date(2026, 9, 1), 336.298)]
        monkeypatch.setattr(derived, "_rows", series({("CPIAUCSL", "any"): cpi}))
        r = await verify("Prices have risen more than 25% since January 2021.", allow_grounding=False)
        assert r.verdict == SUPPORTED and "28.0%" in r.headline
        r = await verify("Prices have risen more than 35% since January 2021.", allow_grounding=False)
        assert r.verdict == CONTRADICTED

    async def test_uses_the_unadjusted_index_bls_publishes(self, monkeypatch):
        # Jan 2020 -> Sep 2026: 257.971 -> 336.940 unadjusted (+30.6%), 259.127 -> 336.298
        # adjusted (+29.8%). BLS's CPI calculator uses the unadjusted index.
        async def bls(sid, start, end):
            return [(date(2020, 1, 1), 257.971), (date(2026, 9, 1), 336.940)]
        monkeypatch.setattr(derived, "_bls_monthly", bls)
        r = await verify("Consumer prices are up more than 30% since January 2020.", allow_grounding=False)
        assert r.verdict == SUPPORTED and "30.6%" in r.headline
        assert r.evidence[0]["source"] == "BLS"

    async def test_yearly_rate_is_not_a_total_change(self):
        assert await derived._change_since("Prices have risen 3% a year since 2021.") is None


UN = [(date(2016, m, 1), 4.9) for m in range(1, 13)] + [(date(2020, 4, 1), 14.8), (date(2026, 9, 1), 4.2)]


class TestRecords:
    async def test_highest_in_a_decade_is_false(self, monkeypatch):
        monkeypatch.setattr(derived, "_rows", series({("UNRATE", "any"): UN}))
        r = await verify("Unemployment is now the highest it has been in a decade.", allow_grounding=False)
        assert r.verdict == CONTRADICTED and "April 2020" in r.headline

    async def test_record_true_as_first_published(self, monkeypatch):
        rows = [(date(1969, 5, 1), 3.4), (date(2019, 9, 1), 3.5), (date(2023, 1, 1), 3.5)]
        monkeypatch.setattr(derived, "_rows", series({
            ("UNRATE", None): rows, ("UNRATE", "any"): [(date(2023, 1, 1), 3.4)]}))
        v = await derived._record("Unemployment fell to 3.4% in January 2023, the lowest since 1969.")
        assert v.verdict == SUPPORTED and "first published" in v.headline


class TestFirstPrint:
    async def test_revised_figure_matches_the_first_release(self, monkeypatch):
        monkeypatch.setattr(derived, "_rows", series({("UNRATE", "any"): [(date(2023, 1, 1), 3.4)]}))
        p = parse_claim("Unemployment was 3.4% in January 2023.")
        dp = Datapoint(quantity=Quantity(3.5, "percent"), metric="unemployment_rate",
                       label="Unemployment rate", observed=Period(2023, "monthly", month=1),
                       geography=Geography.national(), source="BLS", source_url="x")
        v = assess(p, [dp])
        assert v.verdict == MOSTLY_ACCURATE
        got = await derived.first_print_match(p, v)
        assert got is not None and got.verdict == SUPPORTED and "first published as 3.4%" in got.headline

    async def test_no_rescue_when_the_first_release_also_differs(self, monkeypatch):
        monkeypatch.setattr(derived, "_rows", series({("UNRATE", "any"): [(date(2023, 1, 1), 3.5)]}))
        p = parse_claim("Unemployment was 3.4% in January 2023.")
        dp = Datapoint(quantity=Quantity(3.5, "percent"), metric="unemployment_rate",
                       label="Unemployment rate", observed=Period(2023, "monthly", month=1),
                       geography=Geography.national(), source="BLS", source_url="x")
        assert await derived.first_print_match(p, assess(p, [dp])) is None


class TestFixedFacts:
    def test_took_office_names_the_month(self):
        text, note = derived.took_office("Biden inherited 9% inflation when he took office.")
        assert text == "Biden inherited 9% inflation in January 2021." and "January 20, 2021" in note

    def test_trump_is_ambiguous_without_a_year(self):
        assert derived.took_office("Trump inherited 9% inflation when he took office.")[1] == ""

    async def test_decennial_count(self):
        r = await verify("The 2020 Census counted 331.4 million people.", allow_grounding=False)
        assert r.verdict == SUPPORTED and r.model_calls == 0

    async def test_future_year_is_declined_for_free(self):
        r = await verify("The unemployment rate in 2031 was 3.5%.")
        assert r.verdict == NO_VERDICT and r.model_calls == 0 and "2031" in r.headline


async def test_debt_held_by_the_public_is_not_the_total(monkeypatch):
    rows = [{"record_date": "2026-10-29", "debt_held_public_amt": "32710000000000",
             "intragov_hold_amt": "7880000000000", "tot_pub_debt_out_amt": "40590000000000"}]

    async def cached(ns, key, ttl, load):
        return rows
    monkeypatch.setattr(derived, "cached", cached)
    p = parse_claim("Federal debt held by the public is $40 trillion.")
    v = await _REAL_DEBT_CHECK(p.text, p)
    assert v.verdict == MISLEADING and "$32.71 trillion" in v.headline
