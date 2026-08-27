"""Tests for the v2 evidence engine, against REAL captured API payloads.

The audit found that the existing suite passed 162/162 while production was
returning wrong numbers, because no captured payload existed anywhere in the
repo: `conftest.py` mocked a BEA row with a `LineCode` key BEA never emits, and
`test_new_sources.py` mocked a top-level `fiscal_year` on an endpoint that
returns none. Each invented fixture corresponded exactly to a confirmed defect
the suite therefore could not see.

Every fixture here was captured from the live API (see tests/fixtures/). If an
agency changes its response shape these tests will not notice — no offline test
can — but they can no longer pass against a shape the API never produced, which
is the failure mode that actually happened.

The regression tests are named for the audit findings they lock down.
"""
import json
import pathlib
from typing import Any, Dict, List

import pytest

from claims.geography import resolve_geography
from claims.metrics import match_metric
from claims.parser import parse_claim
from claims.verdict import (
    CONTRADICTED, NO_VERDICT, PERIOD_MISMATCH, SUPPORTED, assess,
)
from evidence.datapoint import (
    ANNUAL, FISCAL_YEAR, POINT_IN_TIME, Datapoint, Geography, Period, Request,
)
from evidence.units import (
    PERCENT, USD, Quantity, humanize, parse_number, parse_quantity, written_precision,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


# ===========================================================================
# units
# ===========================================================================

class TestParseNumber:
    @pytest.mark.parametrize("raw,expected", [
        ("790,895", 790895.0), ("3.7", 3.7), ("(1,234)", -1234.0),
        ("$1,000", 1000.0), ("-2.5", -2.5), ("0", 0.0),
    ])
    def test_parses(self, raw, expected):
        assert parse_number(raw) == expected

    @pytest.mark.parametrize("raw", ["(NA)", "N/A", "-", "", None, "(D)", "(S)", "abc"])
    def test_missing_data_is_none_not_zero(self, raw):
        """A suppressed cell must never be read as the number zero."""
        assert parse_number(raw) is None

    @pytest.mark.parametrize("jam", [-666666666, -999999999, "-666666666"])
    def test_census_jam_values_rejected(self, jam):
        """Census annotation codes are not measurements."""
        assert parse_number(jam) is None


class TestWrittenPrecision:
    @pytest.mark.parametrize("text,expected", [
        ("3.7", 0.1), ("4.0", 0.1), ("9", 1.0), ("4", 1.0),
        ("73000", 1000.0), ("33", 1.0), ("3.75", 0.01),
    ])
    def test_precision(self, text, expected):
        assert written_precision(text) == pytest.approx(expected)

    def test_trailing_zeros_are_not_significant(self):
        """'$73,000' asserts to the nearest thousand, not the nearest dollar."""
        assert written_precision("73000") == 1000.0


class TestQuantityTolerance:
    def test_nine_percent_covers_nine_point_zero_six(self):
        """The June-2022 case: a true claim written as '9%' must not be Contradicted."""
        q = parse_quantity("9%")
        assert abs(q.magnitude - 9.06) <= q.tolerance()

    def test_three_point_seven_does_not_cover_four_point_zero(self):
        q = parse_quantity("3.7%")
        assert abs(q.magnitude - 4.0) > q.tolerance()

    def test_hedge_widens_tolerance(self):
        assert parse_quantity("about $73,000").tolerance() > parse_quantity("$73,000").tolerance()

    def test_comparators_are_not_hedges(self):
        """'over' is directional, not vague; treating it as a hedge loosened
        the tolerance tenfold on a precise assertion."""
        assert parse_quantity("over $33 trillion").approximate is False
        assert parse_quantity("about $33 trillion").approximate is True


class TestParseQuantity:
    @pytest.mark.parametrize("text,mag,family", [
        ("$33 trillion", 33e12, USD),
        ("790.9 billion dollars", 790.9e9, USD),
        ("3.7%", 3.7, PERCENT),
        ("8.3 million", 8.3e6, "unknown"),
    ])
    def test_magnitude_and_family(self, text, mag, family):
        q = parse_quantity(text)
        assert q.magnitude == pytest.approx(mag)
        assert q.family == family

    def test_units_normalise_to_one_space(self):
        """BEA reports millions, Treasury whole dollars; both must compare."""
        bea = Quantity(magnitude=790895 * 1e6, family=USD)
        claim = parse_quantity("$790.9 billion")
        assert claim.compare_to(bea).agrees


# ===========================================================================
# geography — audit #4
# ===========================================================================

class TestGeographyDoesNotSubstituteStates:
    @pytest.mark.parametrize("text", [
        "New York City", "Kansas City", "Virginia Beach", "Oklahoma City",
    ])
    def test_cities_do_not_resolve_to_their_state(self, text):
        """The substring resolver answered NYC's population with New York State's."""
        g = resolve_geography(text)
        assert g is not None and g.level == "place", f"{text} resolved to {g}"

    @pytest.mark.parametrize("text,name", [
        ("Texas", "Texas"), ("New York", "New York"), ("in California", "California"),
    ])
    def test_states_still_resolve(self, text, name):
        g = resolve_geography(text)
        assert g.level == "state" and g.name == name

    def test_national_phrases(self):
        assert resolve_geography("the United States").level == "nation"

    def test_no_place_named(self):
        assert resolve_geography("the unemployment rate rose") is None


class TestGeographyFailsClosedOutsideTheUS:
    """A named place that does not resolve must decline, not fall back to the US.

    Reproduced against production before this fix, tier=structured, 0 model calls:

        "The unemployment rate in France was 4% in 2023."  -> Supported
        "The unemployment rate in Germany was 3% in 2023." -> Contradicted
        "The unemployment rate on Mars was 4% in 2023."    -> Supported

    Each was answered from LNS14000000, the US national series. resolve_geography
    returned None for the unrecognised place, Request.geography was therefore None,
    and mismatch_against skips the geography check entirely when it is None — so
    the only guard that could have caught it was never consulted.
    """

    @pytest.mark.parametrize("country", ["France", "Germany", "Japan", "Canada"])
    def test_foreign_country_does_not_resolve_to_the_nation(self, country):
        g = resolve_geography(f"The unemployment rate in {country} was 4% in 2023.")
        assert g is not None, "a named country must not read as 'no place named'"
        assert not g.is_known
        assert g.level == "unknown"

    def test_unrecognised_proper_noun_declines(self):
        g = resolve_geography("The unemployment rate on Mars was 4% in 2023.")
        assert g is not None and not g.is_known

    def test_continent_beats_the_national_phrase(self):
        # "america" is a national phrase, so an earlier ordering answered
        # "South America" with the US series.
        g = resolve_geography("Unemployment in South America rose in 2023.")
        assert g is not None and not g.is_known

    def test_claim_naming_no_place_still_returns_none(self):
        # None means "no geography constraint", which is legitimate and must
        # keep working — the whole fix rests on these two cases being distinct.
        assert resolve_geography("The unemployment rate was 3.6% in 2023.") is None

    @pytest.mark.parametrize("text", [
        "The unemployment rate in December 2023 was 3.7%.",
        "Inflation was 9.1% in June 2022.",
        "The unemployment rate in Q2 was 4%.",
    ])
    def test_locative_fallback_does_not_fire_on_non_places(self, text):
        assert resolve_geography(text) is None

    def test_us_geographies_are_unaffected(self):
        assert resolve_geography("unemployment in Ohio").level == "state"
        assert resolve_geography("unemployment in the United States").level == "nation"
        assert resolve_geography("the population of New York City").level == "place"

    def test_unknown_geography_is_refused_by_the_guard(self):
        dp = _dp_pct(3.6, 2023)
        request = Request(metric="unemployment_rate", year=2023,
                          geography=Geography.unknown("France"))
        assert not dp.answers(request)
        assert "France" in dp.mismatch_against(request)

    def test_unknown_geography_never_matches(self):
        assert not Geography.national().matches(Geography.unknown("France"))
        assert not Geography.unknown("France").matches(Geography.national())


# ===========================================================================
# metric matching
# ===========================================================================

class TestMetricMatching:
    @pytest.mark.parametrize("text,metric", [
        ("the unemployment rate in 2024", "unemployment_rate"),
        ("inflation hit 9%", "inflation_rate"),
        ("core inflation was 4%", "core_inflation_rate"),
        ("the national debt", "national_debt"),
        ("median household income in Texas", "median_household_income"),
    ])
    def test_matches(self, text, metric):
        assert match_metric(text).metric == metric

    def test_longest_phrase_wins(self):
        """'core inflation' must not be matched as plain 'inflation'."""
        assert match_metric("core inflation rate").metric == "core_inflation_rate"

    def test_unmatched_returns_none(self):
        """A clean miss routes to grounded search; guessing would be worse."""
        assert match_metric("crime is out of control in our cities") is None


# ===========================================================================
# claim parsing
# ===========================================================================

class TestClaimParsing:
    def test_year_is_not_read_as_the_value(self):
        p = parse_claim("Inflation was 9% in 2022")
        assert p.year == 2022
        assert p.claimed_value.magnitude == pytest.approx(9.0)

    def test_bill_number_is_not_read_as_a_year(self):
        """'S. 2024' is a bill designator, not a year."""
        p = parse_claim("S. 2024 was signed into law")
        assert p.year is None

    def test_fiscal_year_is_distinguished(self):
        p = parse_claim("The DoD budget was $800 billion in FY2023")
        assert p.year == 2023 and p.year_basis == "fiscal"

    def test_comparator_extracted(self):
        assert parse_claim("the debt is over $33 trillion").comparator == "greater than"
        assert parse_claim("unemployment was under 4%").comparator == "less than"

    def test_trend_claim_flagged(self):
        p = parse_claim("Unemployment fell from 6.7% in 2021 to 3.7% in 2024")
        assert p.is_trend_claim and p.all_years == [2021, 2024]

    def test_unparseable_claim_says_why(self):
        p = parse_claim("Crime is out of control")
        assert not p.is_actionable and p.unparsed_reason


# ===========================================================================
# Period / Request guard — the audit's root cause
# ===========================================================================

class TestPeriodGuard:
    def test_fiscal_year_does_not_cover_the_calendar_year(self):
        """FY2023 ended 2023-09-30 and says nothing about Oct-Dec 2023."""
        assert Period(year=2023, kind=FISCAL_YEAR).covers_calendar_year(2023) is False
        assert Period(year=2023, kind=FISCAL_YEAR).covers_fiscal_year(2023) is True

    def test_unknown_period_fails_closed(self):
        """A datapoint that cannot state its period is not evidence."""
        dp = _dp(value=100.0, period=Period.unknown())
        assert dp.mismatch_against(Request(metric="m", year=2023)) is not None

    def test_matching_period_passes(self):
        dp = _dp(value=100.0, period=Period(year=2023, kind=ANNUAL))
        assert dp.mismatch_against(Request(metric="m", year=2023)) is None

    def test_geography_mismatch_is_refused(self):
        dp = _dp(value=100.0, period=Period(year=2023, kind=ANNUAL),
                 geo=Geography(name="New York", level="state", fips="36"))
        req = Request(metric="m", year=2023,
                      geography=Geography(name="Texas", level="state", fips="48"))
        assert "Texas" in dp.mismatch_against(req)


def _dp(value, period, geo=None, metric="m"):
    return Datapoint(
        quantity=Quantity(magnitude=value, family=USD), metric=metric, label="L",
        observed=period, geography=geo or Geography.national(),
        source="TEST", source_url="https://example.gov/x",
    )


# ===========================================================================
# Adapter parsing against REAL payloads
# ===========================================================================

class TestTreasuryFixtures:
    def test_calendar_and_fiscal_2023_differ(self):
        """Audit #5: the FY filter silently excluded Oct-Dec of the claim's year."""
        cal = load("treasury_debt_cal2023.json")["data"][0]
        fy = load("treasury_debt_fy2023.json")["data"][0]
        cal_v = parse_number(cal["tot_pub_debt_out_amt"])
        fy_v = parse_number(fy["tot_pub_debt_out_amt"])
        assert cal["record_date"].startswith("2023-12")
        assert fy["record_date"].startswith("2023-09")
        # Roughly $0.8T apart — enough to flip a verdict on a true claim.
        assert cal_v - fy_v > 5e11

    def test_calendar_record_crosses_34_trillion(self):
        cal = load("treasury_debt_cal2023.json")["data"][0]
        assert parse_number(cal["tot_pub_debt_out_amt"]) >= 34e12


class TestUSAspendingFixtures:
    def test_toptier_payload_has_no_fiscal_year_key(self):
        """Audit #1's root cause: `data.get("fiscal_year", year)` always fell
        back to the requested year because the key does not exist."""
        payload = load("usaspending_toptier_agencies.json")
        assert "fiscal_year" not in payload

    def test_budgetary_resources_is_genuinely_fy_aware(self):
        rows = load("usaspending_dod_budgetary_resources.json")["agency_data_by_year"]
        by_fy = {r["fiscal_year"]: r["agency_budgetary_resources"] for r in rows}
        assert len(set(by_fy.values())) == len(by_fy), "values must differ per FY"
        assert by_fy[2023] != by_fy[2025]


class TestBLSFixtures:
    def test_keyless_response_has_no_annual_average_rows(self):
        """The adapter requested M13 and BLS silently did not provide it."""
        rows = load("bls_unemployment_2019_2024.json")["Results"]["series"][0]["data"]
        assert not [r for r in rows if r["period"] == "M13"]

    def test_twelve_monthly_observations_per_complete_year(self):
        rows = load("bls_unemployment_2019_2024.json")["Results"]["series"][0]["data"]
        for year in ("2021", "2022", "2023"):
            months = [r for r in rows if r["year"] == year and r["period"].startswith("M")]
            assert len(months) == 12, f"{year} has {len(months)} months"

    def test_computed_annual_average_matches_published_figure(self):
        """2024 unemployment averaged 4.0%; the mean of the monthlies must agree."""
        import statistics
        rows = load("bls_unemployment_2019_2024.json")["Results"]["series"][0]["data"]
        vals = [parse_number(r["value"]) for r in rows
                if r["year"] == "2024" and r["period"].startswith("M")]
        assert round(statistics.fmean(vals), 1) == pytest.approx(4.0)

    def test_june_2022_cpi_yoy_is_about_nine_percent(self):
        """Audit #9: month-over-same-month, not month-over-annual-average."""
        rows = load("bls_cpi_2021_2022.json")["Results"]["series"][0]["data"]
        cur = next(parse_number(r["value"]) for r in rows
                   if r["year"] == "2022" and r["period"] == "M06")
        prev = next(parse_number(r["value"]) for r in rows
                    if r["year"] == "2021" and r["period"] == "M06")
        assert (cur - prev) / prev * 100 == pytest.approx(9.06, abs=0.1)


# ===========================================================================
# Verdict assembly
# ===========================================================================

class TestAssess:
    def _parsed(self, text):
        return parse_claim(text)

    def test_supported_within_written_precision(self):
        dp = _dp_pct(4.0, 2024)
        v = assess(self._parsed("The unemployment rate in 2024 was 4.0%"), [dp])
        assert v.verdict == SUPPORTED

    def test_contradicted_outside_precision(self):
        dp = _dp_pct(4.0, 2024)
        v = assess(self._parsed("The unemployment rate in 2024 was 3.7%"), [dp])
        assert v.verdict == CONTRADICTED
        assert "3.7%" in v.headline and "4" in v.headline

    def test_directional_claim_uses_threshold_not_equality(self):
        """'over $33 trillion' against $40.1T is Supported, not Contradicted."""
        dp = Datapoint(
            quantity=Quantity(magnitude=40.1e12, family=USD),
            metric="national_debt", label="Debt",
            observed=Period(year=2026, kind=POINT_IN_TIME, record_date="2026-08-25"),
            geography=Geography.national(), source="TREASURY",
            source_url="https://fiscaldata.treasury.gov/x",
        )
        p = self._parsed("The national debt is over $33 trillion")
        assert assess(p, [dp]).verdict == SUPPORTED

    def test_period_mismatch_still_shows_the_number(self):
        """Degrade to the data, not to 'I don't know'."""
        dp = _dp_pct(4.0, 2022)
        v = assess(self._parsed("The unemployment rate in 2024 was 4.0%"), [dp])
        assert v.verdict == NO_VERDICT
        assert v.reason == PERIOD_MISMATCH
        assert v.official_display and "2022" in v.official_display

    def test_no_evidence_names_a_reason(self):
        v = assess(self._parsed("The unemployment rate in 2024 was 4.0%"), [])
        assert v.verdict == NO_VERDICT and v.reason


def _dp_pct(value, year):
    return Datapoint(
        quantity=Quantity(magnitude=value, family=PERCENT),
        metric="unemployment_rate", label="Unemployment rate",
        observed=Period(year=year, kind=ANNUAL, basis="annual average"),
        geography=Geography.national(), source="BLS",
        source_url="https://data.bls.gov/timeseries/LNS14000000",
        series_id="LNS14000000",
    )


class TestHumanize:
    @pytest.mark.parametrize("mag,family,expected", [
        (33e12, USD, "$33.00 trillion"),
        (790.9e9, USD, "$790.90 billion"),
        (3.7, PERCENT, "3.7%"),
    ])
    def test_renders(self, mag, family, expected):
        assert humanize(mag, family) == expected
