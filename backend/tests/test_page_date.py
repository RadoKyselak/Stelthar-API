"""A page's own date, and what it is allowed to settle.

A news release says "the unemployment rate changed little at 4.1 percent in
October" and never names the year: its date does. These tests hold three
things. Where the date is read from (claims/article.py), what it may fill in
(claims/splitter.py), and what is still not listed when the reading is not
sure (services/check_text.py). They also hold the parser to the wording the
agencies' own releases use, which is the text this feature was built on.
"""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from claims import article, splitter
from claims.parser import parse_claim
from claims.verdict import CONTRADICTED, HIGH, SUPPORTED
from services import check_link as cl
from services import check_text as ct
from services.verify_v2 import VerificationV2
from utils import safe_fetch as sf

NOV_6 = date(2026, 11, 6)
BODY = "<p>The unemployment rate changed little at 4.1 percent in October, the agency reported today.</p>"


def page(head="", body=BODY):
    return f"<html><head><title>Release</title>{head}</head><body><article>{body}</article></body></html>"


class TestWhenAPageWasPublished:
    @pytest.mark.parametrize("text, expected", [
        ("2026-11-06T08:30:00-05:00", date(2026, 11, 6)),
        ("November 6, 2026", date(2026, 11, 6)),
        ("Nov. 6, 2026", date(2026, 11, 6)),
        ("Friday, November 06, 2026", date(2026, 11, 6)),
        ("6 November 2026", date(2026, 11, 6)),
        ("2026-13-40", None),                       # not a date
        ("1985-01-01", None),                       # older than anything checked here
        ("2099-01-01", None),                       # a page is not from the future
        ("sometime last autumn", None),
        ("", None),
    ])
    def test_reading_a_date(self, text, expected):
        assert article.read_date(text) == expected

    @pytest.mark.parametrize("head", [
        '<meta property="article:published_time" content="2026-11-06T13:30:00Z">',
        '<meta name="date" content="2026-11-06">',
        '<meta name="DC.date.created" content="2026-11-06">',
        '<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-11-06T08:30:00-05:00"}</script>',
        '<script type="application/ld+json">{"@graph":[{"@type":"WebPage","datePublished":"2026-11-06","dateModified":"2026-11-07"}]}</script>',
    ])
    def test_the_page_says_so_in_its_markup(self, head):
        assert article.extract(page(head)).published == NOV_6

    def test_a_time_tag_marked_as_the_publication_date(self):
        body = '<time itemprop="datePublished" pubdate>November 06, 2026</time>' + BODY
        assert article.extract(page(body=body)).published == NOV_6
        body = '<time datetime="2026-11-06T08:30" pubdate>Friday</time>' + BODY
        assert article.extract(page(body=body)).published == NOV_6

    def test_any_other_time_tag_dates_nothing(self):
        """A list of headlines carries a date on every row; none is the page's."""
        body = '<time datetime="2026-11-06T08:30">November 6</time>' + BODY
        assert article.extract(page(body=body)).published is None

    def test_a_press_release_dates_itself_in_its_opening_line(self):
        body = ("<pre>Transmission of material in this news release is embargoed until USDL-26-1632 "
                "8:30 a.m. (ET) Friday, November 6, 2026\n\n"
                "Total nonfarm payroll employment rose by 165,000 in October, and the unemployment rate "
                "changed little at 4.1 percent, the U.S. Bureau of Labor Statistics reported today.</pre>")
        assert article.extract(page(body=body)).published == NOV_6

    def test_a_date_deep_in_the_text_is_not_the_pages(self):
        body = BODY * 4 + "<p>The report for November is scheduled for release on December 4, 2026, at 8:30 a.m.</p>"
        assert article.extract(page(body=body)).published is None

    @pytest.mark.parametrize("head", [
        # An encyclopedia entry: created long ago, edited last week.
        '<script type="application/ld+json">{"datePublished":"2003-05-01","dateModified":"2026-11-10"}</script>',
        # A page kept current.
        '<meta property="article:published_time" content="2025-09-09">'
        '<meta property="article:modified_time" content="2026-07-17">',
    ])
    def test_a_page_reworked_long_after_it_came_out_has_no_date(self, head):
        assert article.extract(page(head)).published is None

    def test_a_page_that_only_says_when_it_changed_has_no_date(self):
        assert article.extract(page('<meta property="article:modified_time" content="2026-11-06">')).published is None

    def test_a_page_that_says_nothing_has_no_date(self):
        assert article.extract(page()).published is None


class TestWhatAPagesDateMaySettle:
    @pytest.mark.parametrize("sentence, as_of, words", [
        ("The unemployment rate rose to 4.4 percent in October", NOV_6, "October 2026"),
        ("The unemployment rate rose to 4.4 percent in late October", NOV_6, "October 2026"),
        ("Inflation was 2.7 percent in December", date(2026, 1, 14), "December 2025"),
        ("The unemployment rate has held near 4.1 percent since it rose in March", NOV_6, "March 2026"),
        ("Employers added 165,000 jobs last month", NOV_6, "October 2026"),
        ("Employers added 50,000 jobs last month", date(2026, 1, 9), "December 2025"),
        ("The economy grew 2.8 percent last year", NOV_6, "2025"),
        # A quarter, in the three ways it is written.
        ("The economy grew 3.4 percent in the third quarter", NOV_6, "Q3 2026"),
        ("The economy grew at a 3.4% annual pace from July through September", date(2026, 10, 29), "Q3 2026"),
        ("The economy expanded at a 3.4% annual rate in the July-September quarter", NOV_6, "Q3 2026"),
        ("The economy grew 3 percent in the fourth quarter", date(2027, 1, 29), "Q4 2026"),
    ])
    def test_a_period_without_its_year(self, sentence, as_of, words):
        assert splitter._page_period(sentence, as_of).words == words

    def test_the_year_goes_where_the_period_is_named(self):
        for sentence, as_of, dated in [
            ("The economy grew at a 3.4% annual pace from July through September as consumers spent",
             date(2026, 10, 29), "from July through September 2026 as consumers spent"),
            ("The economy expanded at a 3.4% annual rate in the July-September quarter, driven by spending",
             NOV_6, "in the July-September quarter of 2026, driven by spending"),
            ("Real GDP increased at an annual rate of 3.4 percent in the third quarter",
             NOV_6, "in the third quarter of 2026"),
        ]:
            written = splitter._dated_by_page(sentence, splitter._page_period(sentence, as_of))
            assert written.endswith(dated)
            got = parse_claim(written)
            assert (got.metric.metric, got.year, got.quarter, got.month) == ("real_gdp_growth", 2026, 3, None)
            assert got.structured_blocker == ""

    @pytest.mark.parametrize("sentence", [
        "The unemployment rate rose to 4.4 percent in November",      # the month still running when the page came out
        "The unemployment rate was 4.4 percent in October 2025",       # it names its year
        "Prices rose 0.4 percent in October after rising 0.6 percent in September",   # two months
        "Employers added 165,000 jobs last month, down from last year",
        "The economy grew 3 percent in the fourth quarter",            # the quarter still running on 6 November
        "Growth of 3.4% from July through September beat the second quarter",   # two quarters
        "The deficit was $1.8 trillion in fiscal year 2025",
        "The unemployment rate is 4.4 percent",                        # no period named at all
    ])
    def test_what_is_left_alone(self, sentence):
        assert splitter._page_period(sentence, NOV_6) is None

    def test_no_date_no_reading(self):
        assert splitter._page_period("The unemployment rate rose to 4.4 percent in October", None) is None

    def test_the_year_is_written_into_the_claim_and_said(self):
        spans, _ = splitter.split_claims("The unemployment rate rose to 4.4 percent in October.",
                                         strict=True, as_of=NOV_6)
        assert [s.claim for s in spans] == ["The unemployment rate rose to 4.4 percent in October 2026"]
        assert spans[0].span == "The unemployment rate rose to 4.4 percent in October"
        assert spans[0].inherited == ["period (October 2026) from the page's date (6 November 2026)"]
        period = splitter._page_period(spans[0].span, NOV_6)
        assert (period.year, period.month, period.quarter) == (2026, 10, None)
        assert spans[0].tentative is False

    def test_without_a_date_the_sentence_is_dropped_as_before(self):
        spans, _ = splitter.split_claims("The unemployment rate rose to 4.4 percent in October.", strict=True)
        assert spans == []

    def test_the_jobs_reports_opening_sentence(self):
        text = ("Total nonfarm payroll employment rose by 165,000 in October, and the unemployment rate changed "
                "little at 4.1 percent, the U.S. Bureau of Labor Statistics reported today.")
        spans, _ = splitter.split_claims(text, strict=True, as_of=NOV_6)
        assert [s.claim for s in spans] == [
            "Total nonfarm payroll employment rose by 165,000 in October 2026",
            "the unemployment rate changed little at 4.1 percent in October 2026",
        ]
        assert all("page's date" in s.inherited[0] for s in spans)

    def test_the_country_is_not_lent_as_a_place(self):
        """"The U.S. Bureau of Labor Statistics reported" names an agency."""
        text = ("Unemployment was 4.1% in 2023, and inflation was 3.4% in 2023, "
                "the U.S. Bureau of Labor Statistics reported.")
        spans, _ = splitter.split_claims(text)
        assert all("United States" not in s.claim for s in spans)

    def test_another_year_nearby_makes_the_reading_tentative(self):
        text = ("Inflation soared after the pandemic and peaked in the summer of 2022.\n"
                "The inflation rate hit 9.1 percent in June.")
        spans, _ = splitter.split_claims(text, strict=True, as_of=NOV_6)
        assert [s.claim for s in spans] == ["The inflation rate hit 9.1 percent in June 2026"]
        assert spans[0].tentative is True

    def test_a_year_far_up_the_page_does_not(self):
        text = ("The agency was founded in 1884.\n" + "Filler paragraph with enough words to stand alone here.\n" * 3
                + "The unemployment rate rose to 4.4 percent in October.")
        spans, _ = splitter.split_claims(text, strict=True, as_of=NOV_6)
        assert spans[0].tentative is False

    def test_the_same_year_nearby_does_not(self):
        text = "The 2026 report came out Friday.\nThe unemployment rate rose to 4.4 percent in October."
        spans, _ = splitter.split_claims(text, strict=True, as_of=NOV_6)
        assert spans[0].tentative is False


def answer(claim, verdict):
    return VerificationV2(claim=claim, verdict=verdict, confidence=HIGH, headline="h", tier="structured")


class TestWhatIsListed:
    TEXT = ("Inflation soared after the pandemic and peaked in the summer of 2022.\n"
            "The inflation rate hit 9.1 percent in June.")

    async def check(self, text, verdict):
        async def fake(claim, allow_grounding=True, client_ip=None):
            return answer(claim, verdict)
        with patch.object(ct, "verify", fake):
            return await ct.check_text(text, mode="page", as_of=NOV_6)

    async def test_an_unsure_reading_that_does_not_match_is_not_a_verdict(self):
        """The sentence is about June 2022. Read as June 2026 it would be
        "Contradicted", and it is not wrong."""
        out = await self.check(self.TEXT, CONTRADICTED)
        assert out["claims"] == [] and out["unsettled"] == 1

    async def test_an_unsure_reading_the_figure_bears_out_is_listed(self):
        out = await self.check(self.TEXT, SUPPORTED)
        assert out["count"] == 1 and out["unsettled"] == 0

    async def test_a_sure_reading_is_listed_whatever_it_finds(self):
        out = await self.check("The unemployment rate rose to 9.9 percent in October.", CONTRADICTED)
        assert out["count"] == 1 and out["tally"]["contradicted"] == 1
        assert out["claims"][0]["claim"].endswith("in October 2026")

    async def test_pasted_text_is_never_dated(self):
        """Only a fetched page says when it was published."""
        calls = []

        async def fake(claim, allow_grounding=True, client_ip=None):
            calls.append(claim)
            return answer(claim, SUPPORTED)
        with patch.object(ct, "verify", fake), patch("evidence.fact_checks.start", lambda c: None), \
                patch("evidence.fact_checks.finish", AsyncMock(return_value=[])):
            await ct.check_text("The unemployment rate rose to 4.4 percent in October.", as_of=NOV_6)
        assert calls == ["The unemployment rate rose to 4.4 percent in October."]

    async def test_a_fetched_page_lends_its_date(self):
        html = page('<meta name="date" content="2026-11-06">')
        fetched = AsyncMock(return_value=sf.Page(url="https://example.gov/r", host="example.gov",
                                                 content_type="text/html", text=html))
        checked = AsyncMock(return_value={"claims": [], "count": 0, "tally": {}, "truncated": False})
        cl._CACHE.clear()
        with patch.object(cl, "fetch_public_page", fetched), patch.object(cl, "check_text", checked):
            got = await cl.check_link("https://example.gov/r")
        cl._CACHE.clear()
        assert checked.await_args.kwargs["as_of"] == NOV_6
        assert got["page"]["published"] == "2026-11-06"


class TestWhatRealPagesShowed:
    """Each of these went wrong on a real page before."""

    def test_a_figure_far_from_its_measure_is_not_its_value(self):
        text = ("When the central bank started raising rates to combat an outbreak of inflation in March 2022, "
                "the average rate on a 1-year CD was stuck at a miserly 0.15%, according to FDIC data.")
        spans, _ = splitter.split_claims(text, kinds=(splitter.STATISTIC, splitter.RECORD), strict=True)
        assert spans == []

    def test_a_figure_beside_its_measure_still_is(self):
        text = "In September 2026, the unemployment rate ticked up to a still-low 4.2%."
        spans, _ = splitter.split_claims(text, kinds=(splitter.STATISTIC, splitter.RECORD), strict=True)
        assert len(spans) == 1

    def test_a_sentence_that_only_touches_a_topic_is_not_a_record_to_check(self):
        text = "That is starting to lift the cost of food, one place where prices appeared to have cooled this year."
        spans, _ = splitter.split_claims(text, kinds=(splitter.STATISTIC, splitter.RECORD), strict=True)
        assert spans == []

    def test_a_pointer_to_another_story_is_not_this_storys_text(self):
        html = page(body=BODY + "<p>READ MORE: Inflation worsened last month to 3.7% as midterm elections near</p>"
                    "<p>WATCH: How companies are using AI to make you spend more money this year</p>")
        assert article.extract(html).blocks == [
            "The unemployment rate changed little at 4.1 percent in October, the agency reported today."]

    def test_a_container_that_has_a_sidebar_is_not_the_sidebar(self):
        """A public broadcaster's whole story sits in "page__body--with-sidebar"."""
        html = ('<html><body><main><div class="page__body page__body--with-sidebar"><article>' + BODY
                + '</article><div class="sidebar"><p>Sign up for our newsletter to get the news every single day.</p>'
                '</div></div></main></body></html>')
        assert article.extract(html).blocks == [
            "The unemployment rate changed little at 4.1 percent in October, the agency reported today."]

    def test_a_figure_said_in_the_headline_and_again_in_the_story_is_checked_once(self):
        text = ("U.S. economy grew a solid 2.2% in the second quarter, government says\n"
                "The U.S. economy grew at a solid 2.2% pace from April through June as spending came in strong.\n"
                "The unemployment rate was 4.2% in August.")
        spans, _ = splitter.split_claims(text, kinds=(splitter.STATISTIC, splitter.RECORD), strict=True,
                                         as_of=date(2026, 9, 30))
        assert [s.claim for s in spans] == [
            "U.S. economy grew a solid 2.2% in the second quarter of 2026, government says",
            "The unemployment rate was 4.2% in August 2026"]

    def test_where_a_wire_story_was_filed_is_not_where_it_is_about(self):
        got = parse_claim("WASHINGTON (AP) — The U.S. economy grew at a solid 2.2% pace from April through June 2026")
        assert got.geography is None or got.geography.level == "nation"
        assert parse_claim("WASHINGTON — Unemployment in Texas was 4.1% in 2023").geography.name == "Texas"
        assert parse_claim("Unemployment in Washington was 4.5% in 2023").geography.name == "Washington"
        html = page(body="<p>WASHINGTON (AP) — The unemployment rate changed little at 4.1 percent in October.</p>")
        assert article.extract(html).blocks == ["The unemployment rate changed little at 4.1 percent in October."]

    def test_a_figure_in_another_unit_or_of_another_group_is_never_listed(self):
        text = ("Among those not in the labor force who wanted a job, the number of people marginally attached "
                "to the labor force changed little at 1.6 million in October.\n"
                "The average workweek for all employees on private nonfarm payrolls remained at 34.4 hours in October.")
        spans, _ = splitter.split_claims(text, kinds=(splitter.STATISTIC, splitter.RECORD), strict=True, as_of=NOV_6)
        assert spans == []


class TestASentenceWithNoPlaceInTextAboutAnotherPlace:
    PERU = ("Peru's economy was among the hardest hit in Latin America.\n"
            "In 2020, the economy contracted 13.4% with the sharpest declines in private consumption.")

    def test_it_is_marked_unsure(self):
        spans, _ = splitter.split_claims(self.PERU, kinds=(splitter.STATISTIC,), strict=True)
        assert len(spans) == 1 and spans[0].tentative is True

    def test_one_that_names_its_own_place_is_not(self):
        text = "Peru's economy was among the hardest hit.\nIn 2020, the U.S. economy contracted 2.2%."
        spans, _ = splitter.split_claims(text, kinds=(splitter.STATISTIC,), strict=True)
        assert spans[0].tentative is False

    def test_nor_is_one_in_text_about_no_other_place(self):
        text = "The recession was short.\nIn 2020, the economy contracted 2.2%."
        spans, _ = splitter.split_claims(text, kinds=(splitter.STATISTIC,), strict=True)
        assert spans[0].tentative is False

    async def test_read_as_the_united_states_and_wrong_it_is_not_listed(self):
        async def fake(claim, allow_grounding=True, client_ip=None):
            return answer(claim, CONTRADICTED)
        with patch.object(ct, "verify", fake):
            out = await ct.check_text(self.PERU, mode="page")
        assert out["claims"] == [] and out["unsettled"] == 1


class TestWhatMayHappenIsNotOnRecord:
    @pytest.mark.parametrize("text", [
        "About 6.3 million people could lose eligibility for SNAP by the 2029 fiscal year if the proposed change "
        "takes effect, according to the U.S. Department of Agriculture.",
        "The bill would raise the deficit by $3.4 trillion over a decade, the CBO said in 2025.",
        "Unemployment should fall to 4.0% in 2027, the administration said.",
    ])
    def test_it_is_not_picked_from_a_page(self, text):
        spans, _ = splitter.split_claims(text, kinds=(splitter.STATISTIC, splitter.RECORD), strict=True)
        assert spans == []

    def test_what_did_happen_still_is(self):
        spans, _ = splitter.split_claims("The unemployment rate fell to 3.5% in September 2019.",
                                         kinds=(splitter.STATISTIC, splitter.RECORD), strict=True)
        assert len(spans) == 1
