"""Claims as people actually write them.

Each case here was answered wrongly, or not at all, by the free (no AI) path
in a real-world benchmark, while a web search would have got it right. All
run offline: no network, no model call.
"""
import pytest

from claims.parser import parse_claim
from claims.verdict import CONTRADICTED, MOSTLY_ACCURATE, SUPPORTED, assess
from evidence.datapoint import ANNUAL, MONTHLY, Datapoint, Geography, Period
from evidence.units import JOBS, PERCENT, USD, Quantity


def point(value, family, metric, year, month=None, source="BLS"):
    return Datapoint(
        quantity=Quantity(magnitude=value, family=family), metric=metric, label=metric,
        observed=Period(year=year, kind=MONTHLY if month else ANNUAL, month=month),
        geography=Geography.national(), source=source, source_url="https://example.gov",
        series_id="X", raw_value=str(value),
    )


class TestComparisonWords:
    def test_under_a_president_is_not_less_than(self):
        assert parse_claim("Under Trump, unemployment hit 3.5% in September 2019").comparator == "equals"

    def test_under_a_figure_is_less_than(self):
        assert parse_claim("Unemployment was under 4% in 2023").comparator == "less than"

    def test_over_the_year_is_not_greater_than(self):
        assert parse_claim("Prices rose 3.4% over the year to December 2023").comparator == "equals"

    def test_passed_a_figure_is_greater_than(self):
        assert parse_claim("The national debt passed $34 trillion in January 2024").comparator == "greater than"

    def test_or_more_still_counts(self):
        assert parse_claim("Inflation was 9% or more in June 2022").comparator == "at least"


class TestPlaces:
    @pytest.mark.parametrize("claim", ["Inflation in the UK was 11.1% in October 2022",
                                       "U.K. inflation hit 11% in 2022"])
    def test_the_uk_is_not_the_us(self, claim):
        geo = parse_claim(claim).geography
        assert geo is not None and geo.level == "unknown"


class TestDirection:
    @pytest.mark.parametrize("claim", ["Real GDP fell 2.2% in 2020", "GDP shrank by about 2% in 2020",
                                       "The economy contracted 2.2% in 2020"])
    def test_a_fall_is_negative_growth(self, claim):
        p = parse_claim(claim)
        assert p.metric.metric == "real_gdp_growth" and p.claimed_value.magnitude < 0

    def test_fell_to_is_a_level(self):
        p = parse_claim("Inflation fell to 3% in 2023")
        assert p.metric.metric == "inflation_rate" and p.claimed_value.magnitude == 3.0

    def test_prices_rose_is_inflation(self):
        assert parse_claim("Prices rose 3.4% over the year to December 2023").metric.metric == "inflation_rate"

    def test_one_items_prices_are_not_overall_inflation(self):
        p = parse_claim("Egg prices rose 30% in 2023")
        assert p.metric is None or p.metric.metric != "inflation_rate"


class TestJobs:
    def test_added_is_a_change_in_payrolls(self):
        p = parse_claim("The economy added 3 million jobs in 2023")
        assert p.metric.metric == "jobs_added" and p.claimed_value.magnitude == 3_000_000

    def test_lost_is_negative(self):
        p = parse_claim("The economy lost 20.5 million jobs in April 2020")
        assert p.metric.metric == "jobs_added" and p.claimed_value.magnitude == -20_500_000
        assert p.month == 4

    def test_a_level_is_still_a_level(self):
        assert parse_claim("Nonfarm payrolls were 157 million in December 2023").metric.metric == "nonfarm_employment"


class TestSpansAndSuperlatives:
    def test_thirty_year_is_not_a_value(self):
        p = parse_claim("30-year mortgage rates hit 8% in 2023")
        assert p.metric.metric == "mortgage_rate" and p.claimed_value.magnitude == 8.0
        assert not p.is_trend_claim

    def test_lowest_in_n_years_is_noted_not_ignored(self):
        p = parse_claim("Unemployment fell to 3.4% in January 2023, the lowest in 54 years")
        assert p.claimed_value.magnitude == 3.4 and not p.is_trend_claim
        assert "lowest in 54 years" in p.aside


class TestPresentTense:
    def test_is_means_now(self):
        p = parse_claim("Unemployment is at 4.1%")
        assert p.present and p.year is None

    def test_a_year_is_not_now(self):
        assert not parse_claim("Unemployment was 3.6% in 2023").present


class TestCloseness:
    def test_a_revision_of_cents_is_close_not_false(self):
        p = parse_claim("Average hourly earnings were $34.27 in December 2023")
        v = assess(p, [point(34.29, USD, "avg_hourly_earnings", 2023, 12)])
        assert v.verdict == MOSTLY_ACCURATE

    def test_a_round_count_well_off_is_not_supported(self):
        p = parse_claim("The economy added 3 million jobs in 2023")
        v = assess(p, [point(2_520_000, JOBS, "jobs_added", 2023)])
        assert v.verdict == MOSTLY_ACCURATE

    def test_a_round_rate_still_rounds(self):
        p = parse_claim("Unemployment was 4% in 2023")
        assert assess(p, [point(3.6, PERCENT, "unemployment_rate", 2023)]).verdict == SUPPORTED

    def test_far_off_is_still_false(self):
        p = parse_claim("Gas was $3.10 a gallon in January 2021")
        assert assess(p, [point(2.326, USD, "gas_price", 2021, 1)]).verdict == CONTRADICTED


# ---------------------------------------------------------------- FRED, no key

@pytest.fixture
def fred_rows(monkeypatch):
    from evidence.sources import fred_free
    data = {}

    async def rows(series_id, start, end):
        return [(d, v) for d, v in data.get(series_id, []) if start.isoformat() <= d <= end.isoformat()]
    monkeypatch.setattr(fred_free, "_rows", rows)
    return data


@pytest.mark.asyncio
class TestFredFree:
    async def test_mortgage_hit_is_the_highest_week(self, fred_rows):
        from evidence.sources import fred_free
        fred_rows["MORTGAGE30US"] = [("2023-10-05", 7.49), ("2023-10-12", 7.57),
                                     ("2023-10-19", 7.63), ("2023-10-26", 7.79)]
        [dp] = await fred_free.fetch("mortgage_rate", "Mortgage rates hit 7.79% in October 2023",
                                     7.79, 2023, 10, False)
        assert dp.quantity.magnitude == 7.79 and dp.extra["publisher_short"] == "Freddie Mac"
        assert dp.extra["excerpt"]["rows"][-1]["cited"]

    async def test_mortgage_average(self, fred_rows):
        from evidence.sources import fred_free
        fred_rows["MORTGAGE30US"] = [("2023-10-05", 7.0), ("2023-10-12", 8.0)]
        [dp] = await fred_free.fetch("mortgage_rate", "Mortgage rates averaged 7.5% in October 2023",
                                     7.5, 2023, 10, False)
        assert dp.quantity.magnitude == 7.5

    async def test_fed_rate_either_limit_of_the_range(self, fred_rows):
        from evidence.sources import fred_free
        fred_rows["DFEDTARU"] = [("2023-07-26", 5.5), ("2023-07-31", 5.5)]
        fred_rows["DFEDTARL"] = [("2023-07-26", 5.25), ("2023-07-31", 5.25)]
        claim = "The Fed raised interest rates to {} in July 2023"
        [hi] = await fred_free.fetch("fed_funds_rate", claim.format("5.5%"), 5.5, 2023, 7, False)
        [lo] = await fred_free.fetch("fed_funds_rate", claim.format("5.25%"), 5.25, 2023, 7, False)
        assert (hi.quantity.magnitude, lo.quantity.magnitude) == (5.5, 5.25)
        assert "5.25% to 5.50%" in hi.extra["excerpt"]["note"]

    async def test_trade_deficit_is_the_negated_annual_sum(self, fred_rows):
        from evidence.sources import fred_free
        fred_rows["BOPGSTB"] = [(f"2023-{m:02d}-01", -60000.0) for m in range(1, 13)]
        [dp] = await fred_free.fetch("trade_deficit", "The trade deficit was $720 billion in 2023",
                                     720e9, 2023, None, False)
        assert dp.quantity.magnitude == pytest.approx(720e9)

    async def test_trade_year_not_complete_is_not_answered(self, fred_rows):
        from evidence.sources import fred_free
        fred_rows["BOPGSTB"] = [(f"2026-{m:02d}-01", -60000.0) for m in range(1, 9)]
        assert await fred_free.fetch("trade_deficit", "x", 1.0, 2026, None, False) == []

    async def test_minimum_wage_now(self, fred_rows):
        from evidence.sources import fred_free
        from datetime import date
        today = date.today()
        fred_rows["FEDMINNFRWG"] = [(today.replace(day=1).isoformat(), 7.25)]
        [dp] = await fred_free.fetch("minimum_wage", "The minimum wage is $7.25", 7.25, None, None, True)
        assert dp.quantity.magnitude == 7.25 and dp.extra["publisher_short"] == "The Labor Department"

    async def test_state_minimum_wage_is_not_answered_with_the_federal_one(self):
        from services.verify_v2 import _structured_is_applicable
        assert _structured_is_applicable(parse_claim("California's minimum wage is $16 an hour")) is not None


# ---------------------------------------------------------------- cities, income bands

_LISTS = {
    ("acs1", "place", ""): [["New York city, New York", 8_300_000, "3651000"],
                            ["Kansas City city, Missouri", 510_000, "2938000"],
                            ["Kansas City city, Kansas", 155_000, "2036000"],
                            ["Chicago city, Illinois", 2_660_000, "1714000"],
                            ["Portland city, Oregon", 630_000, "4159000"],
                            ["Portland city, Maine", 68_000, "2360545"],
                            ["Springfield city, Missouri", 170_000, "2970000"],
                            ["Springfield city, Illinois", 114_000, "1772000"],
                            ["Springfield city, Massachusetts", 155_000, "2567000"],
                            ["Austin city, Texas", 980_000, "4805000"],
                            ["Nashville-Davidson metropolitan government (balance), Tennessee", 690_000, "4752006"],
                            ["Urban Honolulu CDP, Hawaii", 340_000, "1571550"],
                            ["San Buenaventura (Ventura) city, California", 110_000, "0665042"]],
    ("acs1", "county", ""): [["Kings County, New York", 2_600_000, "36047"],
                             ["Kings County, California", 150_000, "06031"],
                             ["New York County, New York", 1_600_000, "36061"],
                             ["Richmond County, New York", 490_000, "36085"],
                             ["Bronx County, New York", 1_400_000, "36005"],
                             ["Westchester County, New York", 1_000_000, "36119"],
                             ["Cook County, Illinois", 5_100_000, "17031"]],
    ("acs1", "metro", ""): [["New York-Newark-Jersey City, NY-NJ Metro Area", 19_900_000, "35620"],
                            ["San Francisco-Oakland-Fremont, CA Metro Area", 4_600_000, "41860"],
                            ["Dallas-Fort Worth-Arlington, TX Metro Area", 8_100_000, "19100"],
                            ["Atlanta-Sandy Springs-Roswell, GA Metro Area", 6_300_000, "12060"],
                            ["Washington-Arlington-Alexandria, DC-VA-MD-WV Metro Area", 6_300_000, "47900"]],
    ("acs5", "place", "16"): [["Ketchum city, Idaho", 3_600, "1642580"]],
}


@pytest.fixture
def places(monkeypatch):
    from evidence.datasets import census_profile

    async def rows(year, level, survey="acs1", state=""):
        return _LISTS.get((survey, level, state), [])
    monkeypatch.setattr(census_profile, "_places", rows)
    return census_profile


@pytest.mark.asyncio
class TestCities:
    @pytest.mark.parametrize("name", ["New York City", "NYC", "N.Y.C.", "New York City's", "the city of New York"])
    async def test_new_york_city(self, places, name):
        g = await places.resolve_place(name, 2024, f"Median household income in {name} was $80,000 in 2024")
        assert g is not None and g.name == "New York city, New York" and g.fips == "3651000"

    @pytest.mark.parametrize("name,county", [("Brooklyn", "Kings County, New York"),
                                             ("Manhattan", "New York County, New York"),
                                             ("the Bronx", "Bronx County, New York"),
                                             ("Staten Island", "Richmond County, New York")])
    async def test_boroughs_are_counties(self, places, name, county):
        g = await places.resolve_place(name, 2024, f"Rent in {name} was $2,000 in 2024")
        assert g is not None and g.name == county and g.level == "county"

    async def test_x_city_city(self, places):
        g = await places.resolve_place("Kansas City", 2024, "Poverty in Kansas City, Missouri was 15% in 2024")
        assert g is not None and g.name == "Kansas City city, Missouri"

    async def test_the_much_bigger_namesake_wins(self, places):
        g = await places.resolve_place("Kansas City", 2024, "Poverty in Kansas City was 15% in 2024")
        assert g is not None and g.name == "Kansas City city, Missouri"
        g = await places.resolve_place("Portland", 2024, "Rent in Portland was $1,600 in 2024")
        assert g.name == "Portland city, Oregon"

    async def test_namesakes_of_similar_size_are_declined(self, places):
        assert await places.resolve_place("Springfield", 2024, "Income in Springfield was $50,000") is None

    @pytest.mark.parametrize("name,claim", [("Springfield, Illinois", "x"), ("Springfield, IL", "x"),
                                            ("Springfield", "Income in Springfield, IL was $60,000")])
    async def test_the_state_picks_the_namesake(self, places, name, claim):
        g = await places.resolve_place(name, 2024, claim)
        assert g is not None and g.name == "Springfield city, Illinois"

    async def test_portland_maine_when_named(self, places):
        assert (await places.resolve_place("Portland, Maine", 2024, "x")).name == "Portland city, Maine"

    @pytest.mark.parametrize("name,expect", [("Nashville", "Nashville-Davidson metropolitan government (balance), Tennessee"),
                                             ("Honolulu", "Urban Honolulu CDP, Hawaii"),
                                             ("Ventura", "San Buenaventura (Ventura) city, California"),
                                             ("Westchester", "Westchester County, New York"),
                                             ("Cook County, Illinois", "Cook County, Illinois")])
    async def test_names_census_writes_differently(self, places, name, expect):
        g = await places.resolve_place(name, 2024, "x")
        assert g is not None and g.name == expect

    async def test_a_small_town_comes_from_the_5_year_survey(self, places):
        g, survey = await places.locate("Ketchum, Idaho", 2024, "Median home value in Ketchum, Idaho was $1,000,000")
        assert g.name == "Ketchum city, Idaho" and survey == "acs5"

    @pytest.mark.parametrize("name,expect", [("New York", "35620"), ("San Francisco", "41860"),
                                             ("Dallas-Fort Worth", "19100"), ("Atlanta", "12060"),
                                             ("Washington", "47900")])
    async def test_metro_areas(self, places, name, expect):
        g = await places.resolve_place(name, 2024, "x", level="metro")
        assert g is not None and g.level == "metro" and g.fips == expect

    async def test_zip_code(self, places):
        g, survey = await places.locate("ZIP Code 10001", 2024, "x", "zcta")
        assert (g.level, g.fips, survey) == ("zcta", "10001", "acs5")
        assert places._geo_params(g) == {"for": "zip code tabulation area:10001"}

    async def test_brooklyn_is_not_kings_county_california(self, places):
        g = await places.resolve_place("Brooklyn", 2024, "Rent in Brooklyn was $2,800")
        assert g.fips == "36047"

    async def test_plain_city_still_works(self, places):
        g = await places.resolve_place("Chicago", 2024, "x")
        assert g is not None and g.name == "Chicago city, Illinois"


class TestPlaceNames:
    def test_possessive_city_name(self):
        assert parse_claim("New York City's median household income was $79,713 in 2024").geography.name == "New York City"

    @pytest.mark.parametrize("claim", ["The median household income in NYC was $80,000 in 2024",
                                       "Median household income in New York City was $79,713 in 2024"])
    def test_a_city_is_not_the_state(self, claim):
        g = parse_claim(claim).geography
        assert g is not None and g.level != "state"


class TestIncomeBands:
    @pytest.mark.parametrize("claim", [
        "Area median income in New York City is $162,000 for a family of four in 2024",
        "The area median income in New York was $145,800 in 2024",
        "80% of area median income in New York City is $116,480 in 2024",
        "Middle income in New York City in 2024 is $80,000 to $240,000",
        "A middle-class household income in New York is $100,000 in 2024",
    ])
    def test_not_checked_against_census_median(self, claim):
        from services.verify_v2 import _structured_is_applicable
        from claims.parser import income_band
        p = parse_claim(claim)
        assert income_band(claim)
        assert p.metric is None or _structured_is_applicable(p) is not None

    @pytest.mark.parametrize("claim", ["Median household income in New York was $81,000 in 2023",
                                       "Low-income countries grew 4% in 2023",
                                       "The poverty rate was 11.1% in 2023"])
    def test_ordinary_claims_are_not_income_bands(self, claim):
        from claims.parser import income_band
        assert not income_band(claim)

    @pytest.mark.asyncio
    async def test_dataset_search_skips_income_bands(self):
        from services import dataset_tier
        claim = "Area median income in New York City is $162,000 for a family of four in 2024"
        assert await dataset_tier.verify(claim, parse_claim(claim), None, 0.0) is None


class TestPlaceParsing:
    @pytest.mark.parametrize("claim,level,name", [
        ("Median income in Austin, Texas was $90,000 in 2023", "place", "Austin, Texas"),
        ("Median income in Austin, TX was $90,000 in 2023", "place", "Austin, Texas"),
        ("Median home value in Boise, Idaho was $450,000 in 2023", "place", "Boise, Idaho"),
        ("Median rent in Manhattan, Kansas was $1,000 in 2023", "place", "Manhattan, Kansas"),
        ("Unemployment in Baton Rouge, LA was 4% in 2023", "place", "Baton Rouge, Louisiana"),
        ("Rent in LA was $2,000 in 2023", "place", "Los Angeles, California"),
        ("Rent in SF was $3,000 in 2023", "place", "San Francisco, California"),
        ("Rent in Philly was $1,500 in 2023", "place", "Philadelphia, Pennsylvania"),
        ("LA County median income was $80,000 in 2023", "county", "Los Angeles County, California"),
        ("Median income in Westchester County, NY was $110,000", "county", "Westchester County, New York"),
        ("Unemployment in the Bay Area was 4% in 2023", "metro", "San Francisco"),
        ("Unemployment in the New York metro area was 5% in 2023", "metro", "New York"),
        ("Poverty in the Dallas-Fort Worth area was 10% in 2023", "metro", "Dallas-Fort Worth"),
        ("Unemployment in Greater Boston was 3% in 2023", "metro", "Boston"),
        ("Median income in Silicon Valley was $150,000 in 2023", "metro", "San Jose"),
        ("GDP of the Washington, D.C. metro area was $600 billion in 2023", "metro", "Washington"),
        ("Median income in ZIP code 10001 was $90,000 in 2023", "zcta", "ZIP Code 10001"),
        ("Median income in 90210 was $150,000 in 2023", "zcta", "ZIP Code 90210"),
    ])
    def test_local_places(self, claim, level, name):
        g = parse_claim(claim).geography
        assert (g.level, g.name) == (level, name)

    @pytest.mark.parametrize("claim,level", [
        ("However, Texas had 4% unemployment in 2023", "state"),
        ("In 2023, Texas had 4% unemployment", "state"),
        ("Rent in Washington, D.C. was $2,200 in 2023", "state"),
        ("Unemployment in Texas was 4% in 2023", "state"),
        ("The unemployment rate in the US was 3.6% in 2023", "nation"),
    ])
    def test_states_and_the_nation_are_unchanged(self, claim, level):
        assert parse_claim(claim).geography.level == level

    def test_a_count_is_not_a_zip_code(self):
        g = parse_claim("The economy added 10000 jobs in 2023").geography
        assert g is None or g.level != "zcta"


class TestLocalSeries:
    def test_laus_ids(self):
        from evidence.datapoint import Geography
        from evidence.sources.laus_local import series_ids
        assert series_ids(Geography("Wayne County, Michigan", "county", "26163")) == ["LAUCN261630000000003"]
        assert series_ids(Geography("Detroit city, Michigan", "place", "2622000")) == ["LAUCT262200000000003"]
        assert series_ids(Geography("New York-Newark-Jersey City, NY-NJ Metro Area", "metro", "35620")) == [
            "LAUMT363562000000003", "LAUMT343562000000003"]
        assert all(len(x) == 20 for x in series_ids(Geography("x, NY Metro Area", "metro", "35620")))

    @pytest.mark.asyncio
    async def test_city_unemployment_end_to_end(self, places, monkeypatch):
        from evidence.sources import bls
        from services import dataset_tier
        monkeypatch.setattr(dataset_tier.census_profile, "available", lambda: True)
        _LISTS[("acs1", "place", "")].append(["Detroit city, Michigan", 630_000, "2622000"])

        async def raw(sid, a, b):
            assert sid == "LAUCT262200000000003"
            return [{"year": "2023", "period": "M13", "periodName": "Annual", "value": "7.4"}]
        monkeypatch.setattr(bls, "_fetch_raw", raw)
        claim = "The unemployment rate in Detroit was 7% in 2023"
        r = await dataset_tier.verify(claim, parse_claim(claim), None, 0.0)
        _LISTS[("acs1", "place", "")].pop()
        assert r is not None and r.model_calls == 0
        assert "not seasonally adjusted" in " ".join(r.caveats).lower()

    @pytest.mark.asyncio
    async def test_state_gdp_growth(self, monkeypatch):
        from evidence.sources import bea_regional
        from evidence.datapoint import Geography
        monkeypatch.setattr(bea_regional, "available", lambda: True)

        async def rows(table, line, geofips, years):
            assert (table, line, geofips) == ("SAGDP1", "1", "48000")
            return {2022: {"value": 2.0e12, "raw": "2,000,000", "name": "Texas", "unit": "x", "url": ""},
                    2023: {"value": 2.08e12, "raw": "2,080,000", "name": "Texas", "unit": "x", "url": ""}}
        monkeypatch.setattr(bea_regional, "_rows", rows)
        [dp] = await bea_regional.fetch_gdp("real_gdp_growth", "Real GDP", Geography("Texas", "state", "48"), 2023)
        assert dp.quantity.magnitude == 4.0


class TestLocalPrices:
    @pytest.mark.parametrize("name,level,code", [
        ("Chicago, Illinois", "place", "S23A"), ("New York City", "place", "S12A"),
        ("San Francisco", "metro", "S49B"), ("Dallas-Fort Worth", "metro", "S37A"),
        ("Fort Worth, Texas", "place", "S37A"), ("Honolulu", "unknown", "S49F"),
        ("Washington", "metro", "S35A"), ("St. Louis", "unknown", "S24B")])
    def test_price_areas(self, name, level, code):
        from evidence.datapoint import Geography
        from evidence.sources.cpi_local import area_for
        assert area_for(Geography(name, level)) == code

    def test_no_price_area_is_no_answer(self):
        from evidence.datapoint import Geography
        from evidence.sources.cpi_local import area_for
        assert area_for(Geography("Boise, Idaho", "place")) is None

    @pytest.mark.asyncio
    async def test_metro_inflation_end_to_end(self, monkeypatch):
        from evidence.sources import bls
        from services import dataset_tier

        async def raw(sid, a, b):
            assert sid == "CUURS23ASA0"
            return [{"year": "2023", "period": "M13", "value": "300.0"},
                    {"year": "2022", "period": "M13", "value": "289.0"}]
        monkeypatch.setattr(bls, "_fetch_raw", raw)
        claim = "Inflation in the Chicago area was 3.8% in 2023"
        r = await dataset_tier.verify(claim, parse_claim(claim), None, 0.0)
        assert r is not None and r.model_calls == 0 and r.verdict == SUPPORTED


@pytest.mark.parametrize("claim", ["Inflation in the US was 4.1% in 2023", "Inflation in the country was 4.1% in 2023",
                                   "Inflation in the United States was 4.1% in 2023", "Inflation in 2023 was 4.1%",
                                   "Inflation in the Chicago area was 3.8% in 2023"])
def test_national_and_place_inflation_are_not_a_price_category(claim):
    assert not parse_claim(claim).structured_blocker
