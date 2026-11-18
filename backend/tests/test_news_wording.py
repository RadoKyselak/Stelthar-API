"""The wording of news stories and of the agencies' own releases.

Every case here is a sentence from a real release or wire story that the
parser read wrongly, or could not read, before. A figure that says how far a
rate moved was taken for the rate; a date a change is counted from was taken
for its period; a quarter named by its months was taken for its last month.
Each was a verdict on something the sentence did not say.
"""
import pytest

from claims.parser import parse_claim


class TestTheAgenciesOwnWording:
    @pytest.mark.parametrize("claim, value", [
        ("Total nonfarm payroll employment rose by 165,000 in October 2026", 165000),
        ("Nonfarm payrolls increased by 228,000 in March 2025", 228000),
        ("Nonfarm payrolls fell by 13,000 in June 2025", -13000),
        ("Payroll employment edged down by 4,000 in August 2025", -4000),
    ])
    def test_a_change_in_payrolls(self, claim, value):
        got = parse_claim(claim)
        assert got.metric.metric == "jobs_added" and got.claimed_value.magnitude == value

    def test_private_payrolls_are_not_total_payrolls(self):
        assert parse_claim("Private payrolls rose by 150,000 in October 2026").metric is None

    def test_one_industrys_payrolls_are_still_declined(self):
        assert parse_claim("Manufacturing payrolls fell by 12,000 in October 2026").structured_blocker

    @pytest.mark.parametrize("claim, value, quarter", [
        ("Real gross domestic product (GDP) increased at an annual rate of 3.4 percent in the third quarter of 2026", 3.4, 3),
        ("Real gross domestic product increased 2.8 percent in 2024", 2.8, None),
        ("Real GDP decreased at an annual rate of 0.6 percent in the first quarter of 2025", -0.6, 1),
        # Read as +0.6 before, which stamped "Contradicted" on a true sentence.
        ("Real GDP fell at an annual rate of 0.6 percent in the first quarter of 2025", -0.6, 1),
        ("Real gross domestic product decreased 2.2 percent in 2020", -2.2, None),
    ])
    def test_growth_in_the_words_of_the_release(self, claim, value, quarter):
        got = parse_claim(claim)
        assert got.metric.metric == "real_gdp_growth"
        assert got.claimed_value.magnitude == pytest.approx(value) and got.quarter == quarter

    def test_a_level_is_still_a_level(self):
        assert parse_claim("GDP fell to $20 trillion in 2020").metric.metric == "gdp"


class TestFiguresThatAreNotTheMeasuresValue:
    @pytest.mark.parametrize("claim", [
        "The number of people marginally attached to the labor force changed little at 1.6 million in October 2026",
        "The number of people employed part time for economic reasons was 4.8 million in October 2026",
        "The number of people without health insurance was 27.1 million in 2024",
    ])
    def test_the_number_of_people_doing_something_is_not_the_population(self, claim):
        got = parse_claim(claim)
        assert got.metric is None or got.metric.metric != "population"

    @pytest.mark.parametrize("claim", [
        "The number of people in Texas is 30 million",
        "The number of people living in the United States reached 340 million in 2024",
        "The population of Texas was 30 million in 2023",
    ])
    def test_the_number_of_people_somewhere_is(self, claim):
        assert parse_claim(claim).metric.metric == "population"

    @pytest.mark.parametrize("claim", [
        "The average workweek for all employees on private nonfarm payrolls remained at 34.4 hours in October 2026",
        "Nonfarm employment has grown for 58 months in a row",
        "The unemployment rate has been below 5 points for 3 years",
    ])
    def test_a_figure_counted_in_something_else(self, claim):
        assert parse_claim(claim).metric is None


class TestWhatRealStoriesShowed:
    """Each of these was read wrongly from a real news story before."""

    @pytest.mark.parametrize("claim, value", [
        ("The unemployment rate rose 0.1 percentage point to 4.2% in September 2026", 4.2),
        ("The unemployment rate rose 0.3% to 4.2% in September 2026", 4.2),
        ("The unemployment rate rose to 4.2% in September 2026", 4.2),
        ("The unemployment rate ticked up to a still-low 4.2% in September 2026", 4.2),
    ])
    def test_the_level_is_the_figure_after_to(self, claim, value):
        got = parse_claim(claim)
        assert got.claimed_value.magnitude == pytest.approx(value) and got.structured_blocker == ""

    @pytest.mark.parametrize("claim", [
        "The unemployment rate rose 0.3% in 2023",
        "Housing investment, hurt by high mortgage rates, dropped 3.3% in 2023",
        "The poverty rate fell 1.2 percent in 2021",
    ])
    def test_how_far_a_rate_moved_is_not_the_rate(self, claim):
        assert "how much the rate changed" in parse_claim(claim).structured_blocker

    def test_a_move_in_points_claims_no_level(self):
        assert parse_claim("Inflation rose 2 percentage points in 2021").claimed_value is None

    @pytest.mark.parametrize("claim", [
        "Wages grew 4.1% in 2023", "Real GDP fell 2.2% in 2020", "Inflation rose 3.4% in 2023",
        "Employers added 165,000 jobs in October 2026",
    ])
    def test_a_series_that_is_itself_a_change_is_untouched(self, claim):
        assert parse_claim(claim).structured_blocker == ""

    @pytest.mark.parametrize("claim", [
        "Hourly wages were up a meager 3.1% from October 2025",
        "Median rents are up 30% compared with 2019",
        "Consumer prices were 3% higher than in October 2025",
    ])
    def test_a_date_a_change_is_counted_from_is_not_its_period(self, claim):
        got = parse_claim(claim)
        assert got.metric is None or "compares with an earlier date" in got.structured_blocker

    @pytest.mark.parametrize("claim", [
        "Unemployment fell from 5.2% in 2021 to 3.6% in 2023",       # two points, each with its year
        "Inflation fell to 3% in October 2026 from 3.7% in September",
        "Consumer prices rose 3.7% in September 2026 from a year earlier",
    ])
    def test_a_comparison_that_dates_its_own_figure_is_untouched(self, claim):
        assert parse_claim(claim).structured_blocker == ""

    @pytest.mark.parametrize("claim", [
        "Average hourly wages rose 0.4% in October 2026 from September",
        "Consumer prices rose 0.4% month-over-month in October 2026",
        "Inflation was 0.3% in October 2026 on a monthly basis",
    ])
    def test_a_change_over_one_month_is_not_the_change_over_twelve(self, claim):
        assert "change over one month" in parse_claim(claim).structured_blocker

    @pytest.mark.parametrize("claim", [
        "The average rate on a 30-year mortgage dipped to 5.98%, its lowest level going back to late 2022",
        "Mortgage rates dipped to 5.98%, the lowest since 2022",
        "Mortgage rates dipped to 5.98%, the lowest since November 2022",
    ])
    def test_the_year_a_record_goes_back_to_is_not_the_claims_year(self, claim):
        got = parse_claim(claim)
        assert got.year is None and got.claimed_value.magnitude == pytest.approx(5.98)

    def test_an_adjective_before_the_count_of_jobs(self):
        got = parse_claim("U.S. employers added a disappointing 29,000 jobs in September 2026")
        assert got.metric.metric == "jobs_added" and got.claimed_value.magnitude == 29000

    @pytest.mark.parametrize("claim, quarter", [
        ("The U.S. economy grew at a healthy 3.4% annual pace from July through September 2026", 3),
        ("The economy expanded at a 3.4% annual rate in the July-September quarter of 2026", 3),
        ("The economy grew at a 2.2% pace from April through June 2026", 2),
        ("GDP grew 1.9% in the October-December period of 2025", 4),
    ])
    def test_a_quarter_named_by_its_months(self, claim, quarter):
        got = parse_claim(claim)
        assert (got.metric.metric, got.quarter, got.month) == ("real_gdp_growth", quarter, None)
        assert got.structured_blocker == ""

    def test_three_months_that_are_not_a_quarter_are_not_one(self):
        assert parse_claim("The economy grew 3% from July through October 2026").quarter is None

    def test_a_quarter_of_jobs_is_still_declined(self):
        """The jobs series here is monthly; a quarter's total is not wired up."""
        assert parse_claim("Employers added 400,000 jobs from July through September 2026").structured_blocker

    def test_an_annual_pace_with_no_quarter_is_still_declined(self):
        assert parse_claim("The economy grew at a 3.4% annual pace in 2026").structured_blocker

    def test_a_pair_of_months_is_not_a_place(self):
        got = parse_claim("The economy expanded at a 3.4% annual rate in the July-September quarter of 2026")
        assert got.geography is None


class TestTheOtherSideOfAFigure:
    """The Census Bureau's own release says "92.0% of the U.S. population had
    health insurance coverage". Read as the uninsured rate, it was held
    against 8.2% and marked Contradicted. Census publishes both shares."""

    @pytest.mark.parametrize("claim", [
        "Meanwhile, 92.0% of the U.S. population had health insurance coverage for all or part of 2024",
        "About 92% of Texans have health insurance",
        "Health insurance coverage reached 92% in 2023",
        "92% of Americans were covered by health insurance in 2023",
    ])
    def test_people_with_coverage(self, claim):
        got = parse_claim(claim)
        assert got.metric.metric == "insured_rate" and got.metric.census_variable == "S2701_C03_001E"

    @pytest.mark.parametrize("claim", [
        "The uninsured rate was 8.2% in 2024",
        "8% of Americans don't have health insurance",
        "8% of Americans do not have health insurance",
        "17% of Texans lack health insurance",
        "8.2% of people were without health insurance coverage in 2024",
        "8% of people had no health insurance coverage in 2024",
    ])
    def test_people_without_it(self, claim):
        assert parse_claim(claim).metric.metric == "uninsured_rate"

    def test_the_two_surveys_differ_by_tenths_not_by_points(self):
        """A claim within a point of the figure may be quoting the other
        survey; one that is five points off is not."""
        from claims.metrics import BY_METRIC
        spec = BY_METRIC["insured_rate"]
        assert spec.alternate_measure and spec.alternate_band <= 0.02


class TestWhatASentenceDoesNotSay:
    """Sentences from encyclopedia articles on the U.S. economy, each of which
    was given a verdict on something it does not claim."""

    @pytest.mark.parametrize("claim", [
        "Fed governor Barr said tariffs caused us to deviate from our path toward the 2% inflation target in September 2026",
        "The unemployment rate was forecast to average 11.5% in 2020 and 9.3% in 2021",
        "Economists had expected GDP to grow 3% in the third quarter of 2026",
        "CBO projected a deficit of $1.9 trillion for fiscal year 2025",
        "The administration's goal is unemployment of 4% in 2026",
    ])
    def test_a_target_or_a_forecast_is_not_a_published_figure(self, claim):
        got = parse_claim(claim)
        assert got.metric is None or "a target, a forecast or an expectation" in got.structured_blocker

    @pytest.mark.parametrize("claim", [
        "The U.S. added $1 trillion to the national debt in fiscal year 2008",
        "The Treasury Department reported an increase in the national debt of $1,017 billion for fiscal year 2008",
        "Trump argued for a $500 billion increase in defense spending in 2026",
        "The national debt rose by $2 trillion in 2023",
        "The deficit fell $300 billion in fiscal year 2022",
    ])
    def test_how_far_a_level_moved_is_not_the_level(self, claim):
        assert "how much the figure changed" in parse_claim(claim).structured_blocker

    @pytest.mark.parametrize("claim", [
        "The national debt rose to $34 trillion in 2023",
        "The national debt was $34 trillion in 2023",
        "The deficit increased by $300 billion to $1.7 trillion in fiscal year 2023",
        "The federal government spent $6.3 trillion in fiscal year 2022",
    ])
    def test_a_level_is_still_checked(self, claim):
        assert parse_claim(claim).structured_blocker == ""

    def test_gdp_as_what_something_is_measured_against(self):
        got = parse_claim("The ratio of household debt to GDP rose from 70% in 2001 to 99% in 2008")
        assert got.metric is None or "against GDP" in got.structured_blocker
        got = parse_claim("Federal spending was 23% of GDP in 2023")
        assert got.metric is None or got.metric.metric not in ("gdp", "real_gdp_growth") or got.structured_blocker

    def test_a_share_of_a_change_is_not_the_change(self):
        got = parse_claim("Dealer markups accounted for 35% of new vehicle inflation in 2022")
        assert got.metric is None or "share of the change" in got.structured_blocker

    @pytest.mark.parametrize("claim, why", [
        ("The Fed says the natural rate of unemployment was around 5% in 2015", "different measure of unemployment"),
        ("The U-6 unemployment rate was 7% in 2023", "different measure of unemployment"),
        ("The Supplemental Poverty Measure showed a poverty rate of 12.9% in 2023", "different measure of poverty"),
        ("The percentage of transgender respondents living in poverty was 34% in 2022", "subgroup"),
        ("Two-person households had a median income of $39,755 in Alabama in 2004", "subgroup"),
        ("The economy added 390,000 jobs at this time in 2022", "by comparison"),
    ])
    def test_another_measure_group_or_period(self, claim, why):
        assert why in parse_claim(claim).structured_blocker

    def test_the_year_of_a_study_is_not_the_year_of_its_figure(self):
        got = parse_claim("According to a 2024 study, climate change raises food inflation by 0.1%")
        assert got.year is None

    def test_a_year_that_names_an_event_and_is_also_one_end_of_a_span(self):
        got = parse_claim("During the 2008 financial crisis, GDP fell by 5.0% from the spring of 2008 to the spring of 2009")
        assert got.all_years == [2008, 2009] and got.is_trend_claim

    @pytest.mark.parametrize("claim, place", [
        ("Guam had a poverty rate of 22.9% in 2010", "Guam"),
        ("The Northern Mariana Islands had a poverty rate of 52.3% in 2010", "Northern Mariana Islands"),
        ("The U.S. Virgin Islands had a poverty rate of 22.4% in 2010", "U.S. Virgin Islands"),
        ("In 2017, American Samoa had a poverty rate of 65%, the highest of any state or territory in the United States",
         "American Samoa"),
    ])
    def test_a_territory_is_not_the_country(self, claim, place):
        got = parse_claim(claim).geography
        assert got.name == place and got.level == "place"

    def test_another_countrys_state_is_not_an_american_one(self):
        got = parse_claim("São Paulo was the only Brazilian state to see GDP growth in 2020, of about 0.4%").geography
        assert got is not None and got.level == "unknown" and got.name == "Brazil"

    @pytest.mark.parametrize("claim", [
        "The New York Times reported that the unemployment rate was 4.1% in 2023",
        "A University of Michigan survey found unemployment at 4.1% in 2023",
        "The Federal Reserve Bank of New York said unemployment was 4.1% in 2023",
    ])
    def test_named_after_a_place_is_not_the_place(self, claim):
        got = parse_claim(claim).geography
        assert got is None or got.level == "nation"

    @pytest.mark.parametrize("claim, place", [
        ("Unemployment in New York was 4.2% in 2023", "New York"),
        ("The Washington Post said unemployment in Texas was 4.1% in 2023", "Texas"),
        ("Michigan's unemployment rate was 4% in 2023", "Michigan"),
        ("About 28% of Californians speak Spanish at home", "California"),
    ])
    def test_a_place_is_still_a_place(self, claim, place):
        assert parse_claim(claim).geography.name == place
