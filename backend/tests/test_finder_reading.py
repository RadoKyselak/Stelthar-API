"""What the dataset finder may say about a claim, and what the engine does
with it (services/dataset_tier.py).

Real fact-checked claims rarely state a figure an agency publishes as such.
"Every child inherits a $30,000 share of the national debt" is two published
figures and a division. The finder may now name the claim's own number and
ask for two fetched figures to be combined; the arithmetic and the verdict
stay with the engine, and a model still never supplies a number.
"""
import time

import pytest

from claims.parser import parse_claim
from claims.verdict import CONTRADICTED, MEDIUM, SUPPORTED
from evidence.datapoint import Datapoint, Geography, Period
from evidence.datasets.finder import Found, OPERATIONS, _reading, _tools
from evidence.units import Quantity
from services.dataset_tier import combine, judge_found


def point(value, family, label, year=2011, month=None, place=None, metric="m"):
    return Datapoint(quantity=Quantity(value, family), metric=metric, label=label,
                     observed=Period(year, "monthly" if month else "annual", month=month),
                     geography=place or Geography.national(), source="BLS", source_url="x", series_id="S")


DEBT = point(14.27e12, "usd", "Total public debt outstanding", metric="national_debt")
PEOPLE = point(309.35e6, "persons", "Total population", year=2010, metric="population")
JOBS_NOW = point(144.97e6, "jobs", "Total nonfarm employment", 2016, 9, metric="nonfarm_employment")
JOBS_THEN = point(134.08e6, "jobs", "Total nonfarm employment", 2009, 1, metric="nonfarm_employment")


class TestCombining:
    def test_dollars_per_person(self):
        got = combine(Found(DEBT, True, second=PEOPLE, operation="ratio"))
        assert got.quantity.family == "usd" and got.quantity.magnitude == pytest.approx(46129, rel=1e-3)
        assert "Computed" in got.extra["excerpt"]["note"]

    def test_a_change_between_two_dates(self):
        got = combine(Found(JOBS_NOW, True, second=JOBS_THEN, operation="difference"))
        assert got.quantity.family == "jobs" and got.quantity.magnitude == pytest.approx(10.89e6)

    def test_a_percent_change(self):
        got = combine(Found(JOBS_NOW, True, second=JOBS_THEN, operation="percent_change"))
        assert got.quantity.family == "percent" and got.quantity.magnitude == pytest.approx(8.12, abs=0.01)

    @pytest.mark.parametrize("found", [
        Found(DEBT, True, second=JOBS_NOW, operation="difference"),          # dollars less jobs
        Found(JOBS_NOW, True, second=DEBT, operation="ratio"),               # jobs per dollar
        Found(DEBT, True, second=point(0.0, "persons", "p"), operation="ratio"),
        Found(point(4.0, "percent", "rate"), True, second=point(8.0, "percent", "rate"), operation="percent_change"),
        Found(DEBT, True, second=point(5e6, "persons", "p", place=Geography("Texas", "state", "48")),
              operation="ratio"),                                             # two different places
        Found(DEBT, True, operation="ratio"),                                 # no second figure
    ])
    def test_what_cannot_honestly_be_combined(self, found):
        assert combine(found) is None


class TestJudging:
    def judge(self, text, found):
        return judge_found(text, parse_claim(text), found, time.monotonic())

    def test_the_claims_figure_comes_from_the_text_and_the_arithmetic_from_here(self):
        text = "The national debt comes to $20,000 for every American."
        got = self.judge(text, Found(DEBT, True, second=PEOPLE, operation="ratio", claim_figure="$20,000"))
        assert got.verdict == CONTRADICTED and got.confidence == MEDIUM
        assert "No agency publishes this figure as such" in got.caveats[0]

    def test_a_share_of_the_debt_may_be_a_share_of_its_other_total(self):
        """PolitiFact rated this Mostly True: $30,419 a person is the debt
        held by the public over the population. Against all of the debt it is
        $46,000, and it was marked Contradicted."""
        text = "Every child born today inherits a $30,000 share in a national debt that stands at more than $13 trillion."
        assert self.judge(text, Found(DEBT, True, second=PEOPLE, operation="ratio", claim_figure="$30,000")) is None

    def test_arithmetic_on_two_figures_is_not_held_to_the_dollar(self):
        """PolitiFact rated this True ("about $48,000"). It was Contradicted
        for being 1.2% off."""
        text = "The national debt is $47,900 for every living American."
        debt = point(15.11e12, "usd", "Total public debt outstanding", metric="national_debt")
        people = point(311.59e6, "persons", "Total population", metric="population")
        got = self.judge(text, Found(debt, True, second=people, operation="ratio", claim_figure="$47,900"))
        assert got.verdict == "Mostly accurate" and "worked out from two others" in got.headline

    def test_a_promise_is_not_settled_by_the_figure_it_names(self):
        text = 'President Obama pushed through the stimulus "based on the promise of keeping unemployment under 8 percent."'
        rate = point(8.3, "percent", "Unemployment rate", 2012, 1, metric="unemployment_rate")
        assert self.judge(text, Found(rate, True, claim_figure="8 percent", comparator="less than")) is None

    def test_a_sentence_the_rules_cannot_read(self):
        text = 'Says that during President Barack Obama’s tenure, the United States has created "15 million new jobs."'
        got = self.judge(text, Found(JOBS_NOW, True, second=JOBS_THEN, operation="difference",
                                     claim_figure="15 million"))
        assert got is not None and got.verdict == CONTRADICTED and "10.89 million" in got.headline

    def test_a_figure_that_is_not_in_the_text_is_not_the_claims(self):
        """A model never supplies a number: not the official one, not the claim's."""
        text = 'The United States has created "15 million new jobs."'
        got = self.judge(text, Found(JOBS_NOW, True, second=JOBS_THEN, operation="difference",
                                     claim_figure="11 million"))
        assert got is None

    def test_a_threshold_read_by_the_finder(self):
        text = "The national debt stands at more than $13 trillion."
        got = self.judge(text, Found(DEBT, True, claim_figure="$13 trillion", comparator="greater than"))
        assert got.verdict == SUPPORTED

    def test_not_exact_is_still_no_answer(self):
        text = 'The United States has created "15 million new jobs."'
        assert self.judge(text, Found(JOBS_NOW, False, second=JOBS_THEN, operation="difference",
                                      claim_figure="15 million")) is None


class TestTheAnswerTool:
    def test_it_offers_the_reading_and_the_operations(self):
        answer = next(t for t in _tools() if t["name"] == "answer")["input_schema"]
        assert {"claim_figure", "comparator", "operation", "second"} <= set(answer["properties"])
        assert answer["properties"]["operation"]["enum"] == list(OPERATIONS)
        assert answer["required"] == ["figure", "exact", "place"]        # an old-style answer still works

    def test_an_unknown_operation_or_comparator_is_dropped(self):
        got = _reading({"operation": "multiply", "comparator": "roughly", "claim_figure": "  $30,000  "})
        assert got == {"operation": "value", "comparator": "", "claim_figure": "$30,000"}
