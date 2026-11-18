"""The day a claim was said, and what real fact-checked claims showed.

QuanTemp's test split (835 U.S. statistical claims ruled on by PolitiFact and
others) was run through the service in November 2026. Twenty were read as
checkable, six got a verdict, and all six were wrong readings: a claim from
2012 with no year in it held against 2026, a proposal held against today, a
forecast held against what happened. Each kind is held here.
"""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from claims.parser import parse_claim
from claims.verdict import CONTRADICTED, HIGH, NO_VERDICT, PERIOD_NOT_STATED, SUPPORTED
from services import verify_v2 as engine
from services.verify_v2 import VerificationV2, as_said, verify

FEB_2012 = date(2012, 2, 8)


class TestWhatAnUndatedClaimMeans:
    def test_a_monthly_figure_is_the_month_before(self):
        assert as_said("The unemployment rate is 8.3%.", FEB_2012) == (
            "The unemployment rate is 8.3% in January 2012", "January 2012")

    def test_january_looks_back_to_december(self):
        assert as_said("The unemployment rate is 8.5%", date(2012, 1, 20))[1] == "December 2011"

    def test_a_yearly_figure_is_the_year_before(self):
        assert as_said("The poverty rate is 15%", FEB_2012)[1] == "2011"

    @pytest.mark.parametrize("claim", [
        "The unemployment rate was 8.3% in January 2012",        # it names its period
        "The moon is made of cheese",                             # nothing checkable
        "Unemployment would be 11% if participation were the same",
    ])
    def test_what_is_left_alone(self, claim):
        assert as_said(claim, FEB_2012) is None

    def test_said_lately_is_said_now(self):
        assert as_said("The unemployment rate is 4.1%", date.today() - timedelta(days=10)) is None
        assert as_said("The unemployment rate is 4.1%", None) is None


def answering(verdict):
    seen = []

    async def fake(claim, allow_grounding=True, client_ip=None):
        seen.append((claim, allow_grounding))
        return VerificationV2(claim=claim, verdict=verdict, confidence=HIGH, headline="h", tier="structured")
    return fake, seen


class TestHowItIsChecked:
    async def test_against_the_figure_of_the_day_on_free_data(self):
        fake, seen = answering(SUPPORTED)
        with patch.object(engine, "_verify_one", fake):
            v = await verify("The unemployment rate is 8.3%.", said_on=FEB_2012)
        assert seen == [("The unemployment rate is 8.3% in January 2012", False)]
        assert v.verdict == SUPPORTED and v.claim == "The unemployment rate is 8.3%."
        assert "read as January 2012" in v.caveats[0] and "February 8, 2012" in v.caveats[0]

    async def test_a_mismatch_is_not_a_finding_that_it_is_wrong(self):
        fake, _ = answering(CONTRADICTED)
        with patch.object(engine, "_verify_one", fake):
            v = await verify("The unemployment rate is 9.9%.", said_on=FEB_2012)
        assert v.verdict == NO_VERDICT and v.reason == PERIOD_NOT_STATED
        assert "may be about another period" in v.headline

    async def test_without_the_day_the_answer_says_it_assumed_today(self):
        fake, seen = answering(CONTRADICTED)
        with patch.object(engine, "_verify_one", fake):
            v = await verify("The unemployment rate is 8.3%.")
        assert seen[0][0] == "The unemployment rate is 8.3%."
        assert any("give the date it was said" in c for c in v.caveats)

    async def test_a_claim_that_names_its_year_gets_no_such_note(self):
        fake, _ = answering(SUPPORTED)
        with patch.object(engine, "_verify_one", fake):
            v = await verify("The unemployment rate was 3.6% in 2023.")
        assert v.caveats == []

    def test_the_reason_has_words_for_the_card(self):
        from services.presentation import _REASONS
        assert PERIOD_NOT_STATED in _REASONS


class TestTheEndpoint:
    def test_the_day_reaches_the_check(self, test_client):
        from routes import check as route
        seen = {}

        async def fake(text, mode="text", client_ip=None, said_on=None):
            seen["said_on"] = said_on
            return {"claims": [], "count": 0, "tally": {}, "truncated": False, "model_calls": 0,
                    "mode": "text", "unsettled": 0, "elapsed_seconds": 0}
        with patch.object(route, "check_text", fake):
            r = test_client.post("/v2/check", json={"text": "The unemployment rate is 8.3%.", "said_on": "2012-02-08"})
        assert r.status_code == 200 and seen["said_on"] == FEB_2012 and r.json()["said_on"] == "2012-02-08"

    @pytest.mark.parametrize("day", ["2012-13-40", "yesterday", "1850-01-01", "2999-01-01"])
    def test_a_day_that_cannot_be_is_refused(self, test_client, day):
        r = test_client.post("/v2/check", json={"text": "The unemployment rate is 8.3%.", "said_on": day})
        assert r.status_code == 422


class TestWhatTheBenchmarkShowed:
    """The six verdicts given on QuanTemp's test claims, all wrong readings."""

    @pytest.mark.parametrize("claim", [
        'Raising the minimum wage to $10.10 an hour "would help lift over a million Americans out of poverty."',
        "The CBO says that if you raise the minimum wage to $10.10 an hour, half a million people would lose their jobs.",
        "Says if labor force participation rate were the same as when Barack Obama became president, "
        "unemployment would be 11 percent.",
        "This is what President Obama said the jobless rate would be if we passed the stimulus: 5.6 percent.",
        "If you take into account all the people who are struggling for work, the real unemployment rate is over 15 percent.",
    ])
    def test_what_would_happen_is_not_a_published_figure(self, claim):
        got = parse_claim(claim)
        assert got.metric is None or got.structured_blocker

    def test_an_amount_something_was_reduced_by_is_not_its_level(self):
        got = parse_claim("The Affordable Care Act has already reduced the number of uninsured in Oregon by 10 percent.")
        assert got.metric is None or "how much the figure changed" in got.structured_blocker

    @pytest.mark.parametrize("claim", [
        "The unemployment rate was 3.6% in 2023", "The national debt rose to $34 trillion in 2023",
        "Employers added 165,000 jobs in October 2026", "Inflation fell to 3% in October 2026",
    ])
    def test_plain_claims_are_untouched(self, claim):
        assert parse_claim(claim).structured_blocker == ""
