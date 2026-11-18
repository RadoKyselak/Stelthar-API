"""A passage is checked claim by claim (claims/splitter.py, services/check_text.py).

The splitter is tested on the sentences that broke the one-claim engine and on
the ones a careless splitter breaks: abbreviations, comparisons that must stay
whole, and context a clause may or may not borrow from its sentence.

The service is tested with the engine replaced, so what is under test is the
routing: which claims get the full pipeline, which get free data only, and
that nothing paid ever runs for a passage.
"""
from unittest.mock import AsyncMock, patch

import pytest

import config
from claims import splitter
from claims.splitter import OTHER, RECORD, STATISTIC, split_claims
from claims.verdict import (
    CONTRADICTED, HIGH, LOW, NO_SERIES, NO_VERDICT, NOT_EMPIRICAL, PERIOD_UNAVAILABLE, SUPPORTED,
)
from services import check_text as ct
from services.verify_v2 import VerificationV2


@pytest.fixture(autouse=True)
def fresh_counters():
    from utils import spend_guard
    spend_guard._reset_for_tests()
    yield
    spend_guard._reset_for_tests()


def claims_of(text, **kw):
    return [s.claim for s in split_claims(text, **kw)[0]]


class TestSentences:
    def test_one_claim_per_sentence(self):
        got = claims_of("The U.S. economy grew 2.5% in 2023. The debt passed $40 trillion. That is a lot.")
        assert got == ["The U.S. economy grew 2.5% in 2023", "The debt passed $40 trillion"]

    def test_an_abbreviation_does_not_end_a_sentence(self):
        got = claims_of("Sen. Smith said gas cost $5 a gallon in Jan. 2022. Unemployment was 3.6% in 2023.")
        assert got == ["Sen. Smith said gas cost $5 a gallon in Jan. 2022", "Unemployment was 3.6% in 2023"]

    def test_a_line_break_ends_a_sentence(self):
        got = claims_of("Unemployment was 3.6% in 2023\nInflation hit 9.1% in June 2022")
        assert got == ["Unemployment was 3.6% in 2023", "Inflation hit 9.1% in June 2022"]

    def test_a_sentence_with_nothing_to_check_is_left_out(self):
        assert claims_of("Nothing to see here. Voters care about the economy.") == []

    def test_offsets_point_at_the_text_as_written(self):
        text = "First, a word.  Unemployment was 3.6% in 2023! Then inflation hit 9.1% in June 2022."
        for s in split_claims(text)[0]:
            assert text[s.start:s.end] == s.span


class TestClauses:
    def test_three_statistics_in_one_sentence_are_three_claims(self):
        spans, _ = split_claims("Inflation is now 3%, unemployment is 4%, and wages are up 5% from last year.")
        assert [s.span for s in spans] == ["Inflation is now 3%", "unemployment is 4%",
                                           "wages are up 5% from last year"]
        assert [s.kind for s in spans][:2] == [STATISTIC, STATISTIC]

    def test_a_comparison_stays_one_claim(self):
        text = "The unemployment rate fell from 5.2% in 2021 to 3.6% in 2023."
        assert claims_of(text) == [text.rstrip(".")]

    def test_a_statistic_is_not_carried_to_a_clause_without_one(self):
        # "in California 5.3%" names no statistic: guessing one would be a guess.
        text = "Unemployment in Texas was 4.1% and in California 5.3% in 2023."
        assert claims_of(text) == [text.rstrip(".")]

    def test_and_inside_a_subject_does_not_split(self):
        text = "The unemployment rate for Black and Hispanic workers was 5% in 2023."
        assert claims_of(text) == [text.rstrip(".")]

    def test_two_figures_for_one_statistic_stay_together(self):
        text = "Median household income rose between 2019 and 2023 from $68,700 to $80,610."
        assert claims_of(text) == [text.rstrip(".")]


class TestInheritedContext:
    def test_the_year_is_shared(self):
        spans, _ = split_claims("In 2023 unemployment was 3.6% and inflation was 4.1%.")
        assert [s.claim for s in spans] == ["In 2023 unemployment was 3.6%", "inflation was 4.1% in 2023"]
        assert spans[1].span == "inflation was 4.1%"            # shown as written
        assert spans[1].inherited and "2023" in spans[1].inherited[0]

    def test_a_month_is_shared_with_its_year(self):
        got = claims_of("Inflation hit 9.1% in June 2022 and unemployment was 3.6%.")
        assert got[1] == "unemployment was 3.6% in June 2022"

    def test_a_present_tense_clause_keeps_its_own_period(self):
        got = claims_of("Unemployment is 4% and inflation was 9.1% in June 2022.")
        assert got[0] == "Unemployment is 4%"

    def test_two_years_are_not_shared(self):
        got = claims_of("Unemployment was 3.6% and inflation was 9.1% in 2022, up from 2021.")
        assert got[0] == "Unemployment was 3.6%"

    def test_a_place_named_once_for_the_sentence_is_shared(self):
        got = claims_of("In Texas, unemployment is 4.1% and median household income is $75,000.")
        assert got == ["unemployment is 4.1% in Texas", "median household income is $75,000 in Texas"]

    def test_a_place_in_one_clause_is_not_lent_to_another(self):
        got = claims_of("Unemployment in Texas is 4% and inflation is 3%.")
        assert got == ["Unemployment in Texas is 4%", "inflation is 3%"]


class TestOnlyRealValues:
    """A sentence picked out of an article names a measure and holds a number
    far more often than it states that measure's value."""

    @pytest.mark.parametrize("text", [
        "The Panic of 1837 was followed by a five-year depression and unprecedented unemployment.",
        "After 1700, the United States gained population rapidly.",
        "The U.S. ranks 10th-highest in GDP per capita by purchasing power.",
        "The unemployment office served 165,000 people last year.",         # a count, not a rate
        "Median household income rose in 2,300 counties.",                  # not dollars
    ])
    def test_a_number_that_is_not_the_measures_value_is_not_a_statistic(self, text):
        assert [s.kind for s in split_claims(text)[0]] in ([], [OTHER])

    @pytest.mark.parametrize("text", [
        "Unemployment was 3.6% in 2023.",
        "The unemployment rate fell to 4.1 percent in October 2026.",
        "The national debt passed $40 trillion in 2026.",
        "The national debt passed 40 trillion in 2026.",                    # money by its scale word
        "Texas has 31 million people.",
    ])
    def test_a_value_written_as_the_measure_is_written_is(self, text):
        assert [s.kind for s in split_claims(text)[0]] == [STATISTIC]

    def test_such_sentences_never_reach_a_page_scan(self):
        text = ("The Panic of 1837 was followed by unprecedented unemployment. "
                "Unemployment was 3.6% in 2023.")
        assert claims_of(text, kinds=(STATISTIC, RECORD)) == ["Unemployment was 3.6% in 2023"]


class TestKinds:
    def test_claims_with_no_figure_that_the_series_settle(self):
        spans, _ = split_claims("The economy shrank in the first quarter of 2022. "
                                "Unemployment is the lowest in 50 years.")
        assert [s.kind for s in spans] == [STATISTIC, STATISTIC]

    def test_a_regulatory_claim_is_a_record(self):
        spans, _ = split_claims("The FDA approved Leqembi in 2023.")
        assert [s.kind for s in spans] == [RECORD]

    def test_a_figure_with_no_series_is_other(self):
        spans, _ = split_claims("About 2 million people attended the parade last weekend.")
        assert [s.kind for s in spans] == [OTHER]

    def test_a_page_scan_keeps_only_what_free_data_settles(self):
        text = "Unemployment was 3.6% in 2023. About 2 million people attended the parade last weekend."
        assert claims_of(text, kinds=(STATISTIC, RECORD)) == ["Unemployment was 3.6% in 2023"]

    def test_the_cap_is_reported(self):
        text = " ".join(f"Unemployment was {n}.{n}% in 20{10 + n}." for n in range(1, 9))
        spans, more = split_claims(text, max_claims=3)
        assert len(spans) == 3 and more is True


# ---------------------------------------------------------------------------

def answer(claim, verdict=SUPPORTED, reason=None, calls=0, tier="structured"):
    return VerificationV2(claim=claim, verdict=verdict, confidence=HIGH if verdict != NO_VERDICT else LOW,
                          headline="h", reason=reason, tier=tier, model_calls=calls)


@pytest.fixture
def engine():
    """The engine, replaced: records how each claim was sent."""
    calls = []

    async def fake(claim, allow_grounding=True, client_ip=None):
        calls.append((claim, allow_grounding))
        if "parade" in claim:
            return answer(claim, NO_VERDICT, NO_SERIES, tier="none")
        if "9.9%" in claim:
            return answer(claim, CONTRADICTED)
        return answer(claim)

    with patch.object(ct, "verify", fake), \
            patch("evidence.fact_checks.start", lambda c: None), \
            patch("evidence.fact_checks.finish", AsyncMock(return_value=[])):
        yield calls


class TestRouting:
    async def test_one_claim_gets_the_full_pipeline(self, engine):
        out = await ct.check_text("Unemployment was 3.6% in 2023.")
        assert engine == [("Unemployment was 3.6% in 2023.", True)]
        assert out["count"] == 1 and out["claims"][0]["deeper"] is False

    async def test_several_claims_use_free_data_only(self, engine):
        out = await ct.check_text("Unemployment was 3.6% in 2023. Inflation was 9.9% in 2022. "
                                  "About 2 million people attended the parade last weekend.")
        assert [grounding for _, grounding in engine] == [False, False]
        assert out["tally"]["supported"] == 1 and out["tally"]["contradicted"] == 1
        assert out["tally"]["no-verdict"] == 1 and out["model_calls"] == 0

    async def test_a_bare_figure_is_listed_but_never_run_through_the_engine(self, engine):
        """No measure was read from it with confidence; whatever the engine
        made of it would be a guess with a verdict on it."""
        out = await ct.check_text("Unemployment was 3.6% in 2023. "
                                  "About 2 million people attended the parade last weekend.")
        assert [claim for claim, _ in engine] == ["Unemployment was 3.6% in 2023"]
        parade = out["claims"][1]
        assert parade["kind"] == "other" and parade["deeper"] is True
        assert parade["result"]["display"]["verdict"]["label"] == "Can't verify"

    async def test_an_unsettled_claim_offers_the_full_check(self, engine):
        out = await ct.check_text("Unemployment was 3.6% in 2023. "
                                  "About 2 million people attended the parade last weekend.")
        assert [c["deeper"] for c in out["claims"]] == [False, True]

    async def test_answers_keep_the_order_of_the_text(self, engine):
        out = await ct.check_text("Inflation was 9.9% in 2022. Unemployment was 3.6% in 2023.")
        assert [c["index"] for c in out["claims"]] == [0, 1]
        assert out["claims"][0]["span"] == "Inflation was 9.9% in 2022"

    async def test_a_page_scan_never_runs_the_full_pipeline(self, engine):
        out = await ct.check_text("Unemployment was 3.6% in 2023.", mode="page")
        assert engine == [("Unemployment was 3.6% in 2023", False)] and out["mode"] == "page"

    async def test_a_page_with_nothing_checkable_does_no_work(self, engine):
        out = await ct.check_text("Nothing to see here.", mode="page")
        assert engine == [] and out["claims"] == [] and out["count"] == 0

    async def test_text_with_no_claim_still_gets_the_engines_own_answer(self, engine):
        out = await ct.check_text("Is the economy doing well?")
        assert engine == [("Is the economy doing well?", True)] and out["count"] == 1

    async def test_switched_off_any_text_is_one_claim(self, engine, monkeypatch):
        monkeypatch.setattr(config, "MULTI_CLAIM", False)
        text = "Unemployment was 3.6% in 2023. Inflation was 9.9% in 2022."
        out = await ct.check_text(text)
        assert engine == [(text, True)] and out["count"] == 1

    async def test_a_splitter_failure_falls_back_to_one_claim(self, engine, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("splitter bug")
        monkeypatch.setattr(splitter, "split_claims", boom)
        out = await ct.check_text("Unemployment was 3.6% in 2023. Inflation was 9.9% in 2022.")
        assert out["count"] == 1 and engine[0][1] is True


class TestPages:
    """A page is text nobody picked sentence by sentence."""

    async def test_only_claims_that_ended_in_a_verdict_are_listed(self):
        async def fake(claim, allow_grounding=True, client_ip=None):
            if "4.2%" in claim:
                return answer(claim, NO_VERDICT, NO_SERIES, tier="none")
            return answer(claim)
        with patch.object(ct, "verify", fake):
            out = await ct.check_text("Unemployment was 3.6% in 2023.\nUnemployment was 4.2% in 2024.", mode="page")
        assert [c["span"] for c in out["claims"]] == ["Unemployment was 3.6% in 2023"]
        assert out["count"] == 1 and out["unsettled"] == 1 and out["claims"][0]["index"] == 0

    async def test_a_passage_someone_pasted_lists_everything(self, engine):
        out = await ct.check_text("Unemployment was 3.6% in 2023. "
                                  "About 2 million people attended the parade last weekend.")
        assert out["count"] == 2 and out["unsettled"] == 0

    @pytest.mark.parametrize("text", [
        "GDP fell 3.7% following the 1973 oil crisis.",                           # a year that names an event
        "The economy grew by an average of 3.8% from 1946 to 1973.",              # a span of years
        "The child poverty rate was 13.7% in 2023.",                              # a subgroup the series lacks
        "Unemployment peaked at 10% after the 2008 financial crisis.",
    ])
    def test_statistics_one_figure_cannot_settle_are_not_checked_on_a_page(self, text):
        assert claims_of(text, kinds=(STATISTIC, RECORD), strict=True) == []
        assert claims_of(text) != []                                             # still listed in a pasted passage

    def test_a_page_does_not_say_when_it_was_written(self):
        """So "is" has no date: only a claim that states its period is checked."""
        assert claims_of("Unemployment is 3.5%, the lowest in decades.", kinds=(STATISTIC, RECORD), strict=True) == []
        assert claims_of("Inflation below 5% is in green.", kinds=(STATISTIC, RECORD), strict=True) == []
        assert claims_of("Unemployment is 3.5%.") == ["Unemployment is 3.5%"]      # typed today, it means today

    def test_two_points_are_one_claim_a_page_can_settle(self):
        text = "The unemployment rate fell from 5.2% in 2021 to 3.6% in 2023."
        assert claims_of(text, kinds=(STATISTIC, RECORD), strict=True) == [text.rstrip(".")]

    def test_a_dependent_clause_is_never_cut_loose(self):
        text = ("It was the worst downturn since the Great Recession, when GDP fell 4.3% in 2009, "
                "and unemployment reached 10% in 2009.")
        got = claims_of(text)
        assert not any(c.startswith("when ") for c in got)


class TestDeeper:
    @pytest.mark.parametrize("reason,expected", [
        (NO_SERIES, True),
        ("no_source_found", True),
        (PERIOD_UNAVAILABLE, False),      # the figure does not exist yet
        (NOT_EMPIRICAL, False),           # an opinion
        ("source_unavailable", False),    # an outage; searching again will not help
    ])
    def test_only_when_the_full_check_could_help(self, reason, expected):
        assert ct._could_go_deeper(answer("c", NO_VERDICT, reason, tier="none")) is expected

    def test_never_for_a_claim_that_has_an_answer(self):
        assert ct._could_go_deeper(answer("c", CONTRADICTED)) is False


class TestEndpoint:
    def test_answers_with_claims_and_a_tally(self, test_client, engine):
        r = test_client.post("/v2/check", json={
            "text": "Unemployment was 3.6% in 2023. Inflation was 9.9% in 2022."})
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2 and body["tally"]["contradicted"] == 1
        assert body["claims"][0]["result"]["display"]["verdict"]["key"] == "supported"

    def test_line_breaks_survive_the_sanitizer(self, test_client, engine):
        r = test_client.post("/v2/check", json={
            "text": "Unemployment was 3.6% in 2023\nInflation was 9.9% in 2022", "mode": "page"})
        assert r.json()["count"] == 2

    def test_rejects_an_unknown_mode(self, test_client):
        assert test_client.post("/v2/check", json={"text": "GDP grew 2.5%", "mode": "all"}).status_code == 422

    def test_rejects_text_over_the_cap(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "CHECK_MAX_CHARS", 700)
        assert test_client.post("/v2/check", json={"text": "x" * 800}).status_code == 422

    def test_has_its_own_smaller_allowance(self, test_client, engine, monkeypatch):
        monkeypatch.setattr(config, "CHECK_PER_MINUTE", 2)
        codes = [test_client.post("/v2/check", json={"text": "Unemployment was 3.6% in 2023."}).status_code
                 for _ in range(3)]
        assert codes == [200, 200, 429]

    def test_the_older_endpoint_is_untouched(self, test_client):
        fake = AsyncMock(return_value=answer("Unemployment was 3.6% in 2023."))
        with patch("main.verify_v2", fake), patch("evidence.fact_checks.start", lambda c: None), \
                patch("evidence.fact_checks.finish", AsyncMock(return_value=[])):
            r = test_client.post("/v2/verify", json={"claim": "Unemployment was 3.6% in 2023."})
        assert r.status_code == 200 and r.json()["verdict"] == SUPPORTED
