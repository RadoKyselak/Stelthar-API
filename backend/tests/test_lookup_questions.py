"""A lookup asked as a full question, the way a benchmark or a person asks.

EconWebArena's U.S. agency questions were run through the free lookup. Plain
state questions were refused, and three kinds of question were answered with
a neighbouring measure. Each is held here.
"""
import pytest

from claims.parser import parse_claim
from services import lookup as lk


def place(text):
    parsed = parse_claim(text)
    return lk._place_in_query(text, parsed, parsed.metric)


class TestThePlaceInAQuestion:
    @pytest.mark.parametrize("text, state", [
        ("According to the U.S. Bureau of Labor Statistics, what was the seasonally adjusted unemployment rate "
         "for California in March 2025?", "California"),
        ("According to the U.S. Census Bureau’s 2023 ACS data, what was the median household income in "
         "Massachusetts in 2023?", "Massachusetts"),
        ("As reported by the U.S. Department of Labor, what was the unemployment rate in Texas in 2023?", "Texas"),
    ])
    def test_the_agency_named_is_not_part_of_the_place(self, text, state):
        got = place(text)
        assert got.name == state and got.level == "state"

    @pytest.mark.parametrize("text, name", [
        ("salem oregon population", "Salem, Oregon"), ("chicago population", "Chicago"),
        ("what's the population of seattle", "Seattle"),
    ])
    def test_search_box_words_still_find_their_city(self, text, name):
        assert place(text).name == name


class TestANeighbouringMeasureIsNotTheAnswer:
    @pytest.mark.parametrize("text, why", [
        ("what was the effective federal funds rate on January 10, 2025?", "effective federal funds rate"),
        ("what was the 12-month percent change in the Consumer Price Index (CPI-U) for shelter in February 2025?",
         "one category of prices or a change"),
        ("what was the seasonally adjusted trade balance in goods for the United States in February 2025?",
         "goods only"),
        ("what was the unemployment rate on March 6, 2025?", "one day or week"),
    ])
    def test_it_is_declined(self, text, why):
        got = parse_claim(text)
        assert got.metric is None or why in got.structured_blocker

    @pytest.mark.parametrize("text", [
        "what was the U.S. unemployment rate in January 2025?",
        "what was the monthly U.S. trade deficit in goods and services for January 2025?",
        "what was the national debt on March 6, 2025?",
        "The Consumer Price Index was 319.082 in February 2025",
    ])
    def test_the_measure_asked_for_is_still_answered(self, text):
        got = parse_claim(text)
        assert got.metric is not None and got.structured_blocker == ""
