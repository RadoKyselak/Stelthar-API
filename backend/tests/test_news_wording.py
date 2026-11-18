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
