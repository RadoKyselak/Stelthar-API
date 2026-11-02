"""Web-search answers stand only on an official or reliable page the search
really returned, and their confidence follows that page.

Live production, 2026-11-02: "A 12-ounce can of Coca-Cola has 140 calories"
came back Supported, naming "USDA FoodData Central" as the source, while every
page the search returned was a diet app (feastapp.ai, bodypalapp.com).
"""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import config
import evidence.cache as cache_mod
from evidence import ai_search, claude_search, fact_checks, source_quality
from services import verify_v2
from utils import spend_guard

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "AI_SEARCH_PROVIDER", "claude")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "AI_MAX_CALLS_PER_DAY", 100)
    monkeypatch.setattr(config, "AI_MAX_CALLS_PER_CLIENT_PER_DAY", 10)
    monkeypatch.setattr(config, "AI_MAX_SPEND_USD_PER_DAY", 1.00)
    monkeypatch.setattr(config, "AI_MAX_CLAIM_CHARS", 300)
    monkeypatch.setattr(config, "REDIS_REST_URL", None)
    monkeypatch.setattr(config, "REDIS_REST_TOKEN", None)
    spend_guard._reset_for_tests()
    claude_search._client = None
    yield
    spend_guard._reset_for_tests()
    claude_search._client = None


def answer(verdict="SUPPORTED", value="138 calories per 12 fl oz can", period="2024",
           org="USDA FoodData Central", url="NONE",
           why="The figure matches the claim."):
    return (f"VERDICT: {verdict}\nOFFICIAL_VALUE: {value}\nPERIOD: {period}\n"
            f"SOURCE_ORG: {org}\nSOURCE_URL: {url}\nEXPLANATION: {why}")


def response(text, results):
    content = [
        NS(type="server_tool_use", input={"query": "q"}),
        NS(type="web_search_tool_result",
           content=[NS(type="web_search_result", url=u, title=t) for u, t in results]),
        NS(type="text", text=text, citations=[]),
    ]
    usage = NS(input_tokens=6000, output_tokens=200,
               server_tool_use=NS(web_search_requests=1, web_fetch_requests=0))
    return NS(content=content, usage=usage, stop_reason="end_turn")


def fake(monkeypatch, resp):
    create = AsyncMock(return_value=resp)
    monkeypatch.setattr(claude_search, "_get_client", lambda: NS(messages=NS(create=create)))
    return create


APPS = [("https://feastapp.ai/coke", "Coke calories"),
        ("https://bodypalapp.com/cola", "Cola nutrition")]
FDC = ("https://fdc.nal.usda.gov/food-details/2678649/nutrients", "Coca-Cola | FoodData Central")
POLITIFACT = ("https://www.politifact.com/factchecks/2020/nov/wi-votes/", "PolitiFact")


async def run(claim, leads=None):
    task = None
    if leads is not None:
        import asyncio

        async def found():
            return leads
        task = asyncio.ensure_future(found())
    token = fact_checks.CURRENT.set(task)
    try:
        return await verify_v2.verify(claim)
    finally:
        fact_checks.CURRENT.reset(token)


# ------------------------------------------------------------------ the page relied on

async def test_answer_resting_on_apps_gets_no_verdict(monkeypatch):
    fake(monkeypatch, response(answer(url="https://feastapp.ai/coke"), APPS))
    v = await run("A 12-ounce can of Coca-Cola has 140 calories.")
    assert v.verdict == verify_v2.NO_VERDICT
    assert "official or reliable source" in v.headline
    assert v.citations == []  # the apps are not shown as sources


async def test_agency_named_but_never_returned_gets_no_verdict(monkeypatch):
    # The answer names an FDC page the search never returned: written from memory.
    fake(monkeypatch, response(answer(url=FDC[0]), APPS))
    v = await run("A 12-ounce can of Coca-Cola has 140 calories.")
    assert v.verdict == verify_v2.NO_VERDICT


async def test_official_page_with_figure_and_period_is_high(monkeypatch):
    fake(monkeypatch, response(answer(url=FDC[0]), APPS + [FDC]))
    v = await run("A 12-ounce can of Coca-Cola has 140 calories.")
    assert v.verdict == verify_v2.SUPPORTED and v.confidence == verify_v2.HIGH
    assert v.citations[0]["url"] == FDC[0] and v.citations[0]["reliability"] == "official"
    assert all(c["reliability"] != "other" for c in v.citations)


async def test_official_page_without_the_figure_is_medium(monkeypatch):
    fake(monkeypatch, response(answer(value="NONE", url=FDC[0]), [FDC]))
    v = await run("A 12-ounce can of Coca-Cola has 140 calories.")
    assert v.confidence == verify_v2.MEDIUM
    assert any("did not give a figure" in c for c in v.caveats)


async def test_url_written_slightly_differently_still_matches(monkeypatch):
    fake(monkeypatch, response(answer(url="http://fdc.nal.usda.gov/food-details/2678649/nutrients/"),
                               [FDC]))
    v = await run("A 12-ounce can of Coca-Cola has 140 calories.")
    assert v.confidence == verify_v2.HIGH


# ------------------------------------------------------------------ fact-checks

WI = "There were more votes than registered voters in Wisconsin in the 2020 election."
WI_ANSWER = answer(verdict="CONTRADICTED", value="NONE", period="November 2020",
                   org="PolitiFact", url=POLITIFACT[0],
                   why="About 3.3 million ballots were cast against 3.7 million registered voters.")
LEAD = {"publisher": "Milwaukee Journal Sentinel", "rating": "False", "title": "No",
        "claim": "More votes than voters in Wisconsin", "date": "2020-11-05",
        "url": "https://www.jsonline.com/story/news/politics/elections/2020/11/05/x/"}


async def test_reliable_page_alone_is_medium(monkeypatch):
    fake(monkeypatch, response(WI_ANSWER, [POLITIFACT]))
    v = await run(WI, leads=[])
    assert v.verdict == verify_v2.CONTRADICTED and v.confidence == verify_v2.MEDIUM
    assert any("not the agency" in c for c in v.caveats)


async def test_reliable_page_and_agreeing_fact_check_is_high(monkeypatch):
    create = fake(monkeypatch, response(WI_ANSWER, [POLITIFACT]))
    v = await run(WI, leads=[LEAD])
    assert v.confidence == verify_v2.HIGH
    # The fact-check was handed to the search as a lead.
    assert "Milwaukee Journal Sentinel" in create.await_args.kwargs["messages"][0]["content"]


async def test_disagreeing_fact_check_caps_at_medium_and_is_named(monkeypatch):
    fake(monkeypatch, response(answer(verdict="SUPPORTED", value="NONE", period="2020",
                                      org="Wisconsin Elections Commission",
                                      url="https://elections.wi.gov/results"),
                               [("https://elections.wi.gov/results", "WEC")]))
    v = await run(WI, leads=[LEAD])
    assert v.confidence == verify_v2.MEDIUM
    assert any("Milwaukee Journal Sentinel rated a similar claim" in c for c in v.caveats)


async def test_fact_check_page_given_as_lead_can_be_the_source(monkeypatch):
    fake(monkeypatch, response(answer(verdict="CONTRADICTED", value="NONE", period="2020",
                                      org="Milwaukee Journal Sentinel", url=LEAD["url"]), APPS))
    v = await run(WI, leads=[LEAD])
    assert v.verdict == verify_v2.CONTRADICTED
    assert v.citations[0]["url"] == LEAD["url"]


@pytest.mark.parametrize("rating,stance", [
    ("False", "CONTRADICTED"), ("Pants on Fire!", "CONTRADICTED"), ("Mostly False", "CONTRADICTED"),
    ("Missing context", "MISLEADING"), ("Half True", "MISLEADING"), ("Mostly True", "MOSTLY_ACCURATE"),
    ("True", "SUPPORTED"), ("Satire", None),
])
async def test_ratings_map_to_the_checker_scale(rating, stance):
    assert fact_checks.stance(rating) == stance


# ------------------------------------------------------------------ what may be checked

@pytest.mark.parametrize("claim", [
    "Vaccines cause autism.",
    "The CHIPS and Science Act was signed into law by President Biden.",
    "Is the federal minimum wage $7.25 an hour?",
    "Did the U.S. add 300,000 jobs in March 2024?",
])
async def test_claims_without_a_number_and_yes_no_questions_reach_search(claim):
    assert ai_search.gate(claim, names_a_metric=False) is None


@pytest.mark.parametrize("claim", [
    "Pineapple is the best pizza topping",
    "Crime is out of control in our cities",
    "I think taxes are too high",
    "What was the unemployment rate in 2023?",
    "Can you tell me the GDP?",
])
async def test_opinions_and_open_questions_are_declined_free(claim):
    assert ai_search.gate(claim, names_a_metric=False)


async def test_a_claim_with_no_number_is_searched(monkeypatch):
    create = fake(monkeypatch, response(
        answer(verdict="CONTRADICTED", value="NONE", period="NONE", org="CDC",
               url="https://www.cdc.gov/vaccine-safety/about/autism.html"),
        [("https://www.cdc.gov/vaccine-safety/about/autism.html", "Autism and Vaccines | CDC")]))
    v = await run("Vaccines cause autism.")
    create.assert_awaited_once()
    assert v.verdict == verify_v2.CONTRADICTED and v.confidence == verify_v2.HIGH


# ------------------------------------------------------------------ a paragraph

PARAGRAPH = ("The city council met on Tuesday to discuss the budget, and members spoke "
             "at length about parks, roads and libraries in every district of the city. "
             "Several residents attended and many of them asked questions about what comes next. "
             "The national unemployment rate was 3.6% in 2023, the council's report said. "
             "The meeting ended at nine in the evening after a long debate among members.")


async def test_a_paragraph_is_cut_to_its_most_checkable_sentence():
    text, cut = verify_v2.one_sentence(PARAGRAPH)
    assert cut and text.startswith("The national unemployment rate was 3.6% in 2023")


async def test_a_short_claim_is_left_alone():
    assert verify_v2.one_sentence("Inflation was 3.4% in 2023.") == ("Inflation was 3.4% in 2023.", False)


async def test_the_checked_sentence_is_named(monkeypatch):
    fake(monkeypatch, response(answer(), []))
    v = await verify_v2.verify(PARAGRAPH, allow_grounding=False)
    assert v.caveats and v.caveats[0].startswith("Only one sentence of the highlighted text was checked")


# ------------------------------------------------------------------ source tiers

@pytest.mark.parametrize("host,tier", [
    ("fdc.nal.usda.gov", "official"), ("elections.wi.gov", "official"), ("www.ons.gov.uk", "official"),
    ("www.politifact.com", "reliable"), ("abcnews.go.com", "reliable"), ("www.harvard.edu", "reliable"),
    ("feastapp.ai", "other"), ("x.gov.evil.com", "other"), ("en.wikipedia.org", "other"),
])
async def test_source_tiers(host, tier):
    assert source_quality.tier(host) == tier


# ------------------------------------------------------------------ a company's own figure

@pytest.mark.parametrize("host,claim,own", [
    ("www.coca-cola.com", "A 12-ounce can of Coca-Cola has 140 calories.", True),
    ("www.mcdonalds.com", "McDonald's Big Mac has 590 calories.", True),
    ("www.mcdonalds.com", "A Big Mac has 590 calories.", False),       # the claim names no company
    ("apple.com", "An apple has 95 calories.", False),                  # a fruit, not a company
    ("apple.com", "Apple has 95 calories.", False),                     # an everyday word opening a sentence
    ("www.healthline.com", "A Big Mac has 590 calories.", False),       # a site cannot name itself in
    ("www.toureiffel.paris", "The Eiffel Tower is in Berlin.", True),   # the subject's own site
    ("parisfoodblog.com", "The best croissant in Paris costs 2 euros.", False),
    ("www.nasa.gov", "NASA landed on the Moon in 1969.", True),         # its own site (official anyway)
])
async def test_own_site(host, claim, own):
    assert source_quality.own_site(host, claim) is own


async def test_the_makers_page_backs_a_product_claim_at_medium(monkeypatch):
    page = ("https://www.coca-cola.com/us/en/brands/coca-cola/products/original", "Coca-Cola Original")
    fake(monkeypatch, response(answer(org="The Coca-Cola Company", url=page[0]), APPS + [page]))
    v = await run("A 12-ounce can of Coca-Cola has 140 calories.")
    assert v.verdict == verify_v2.SUPPORTED and v.confidence == verify_v2.MEDIUM
    assert any("own site" in c for c in v.caveats)
    assert v.citations[0]["url"] == page[0]


async def test_an_opinion_is_said_once():
    v = await run("Pineapple is the best pizza topping.")
    assert v.headline.startswith("That reads as an opinion")
