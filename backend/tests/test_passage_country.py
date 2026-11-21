"""A sentence picked from a passage about another country is about that country.

Found on an IMF report on Japan: "Inflation was stronger than expected in
2025, boosted by soaring rice prices" names no place, was read as the United
States, and was called contradicted by BLS prices. The passage said "BOJ" and
"yen", never "Japan".
"""
import asyncio
from unittest.mock import AsyncMock, patch

from claims.geography import passage_country, with_place
from services import verify_v2
from services.verify_v2 import VerificationV2

IMF_JAPAN = (
    "Growth has been resilient, recovering to exceed potential in the first half of 2025, but has started to "
    "moderate. Despite elevated uncertainty and the introduction of U.S. tariffs, domestic demand has remained "
    "firm, with business investment and consumption strong. Inflation was stronger than expected in 2025, "
    "boosted by soaring rice prices, and inflationary pressures have broadened across products. Services "
    "inflation has been more moderate, while yen depreciation has had a limited impact on headline inflation, "
    "with import prices declining on average in 2025. Medium-term inflation expectations have been rising and "
    "are gradually realigning with the BOJ's target. Labor market tightness is supporting higher nominal wage "
    "growth than has been seen in decades, but real wages have continued to contract as headline price "
    "increases outpace wage gains.")


class TestWhichCountry:
    def test_central_bank_and_currency_name_the_country(self):
        assert passage_country(IMF_JAPAN) == "Japan"

    def test_one_mention_is_not_enough(self):
        assert passage_country("The dollar rose against the yen.") is None

    def test_a_us_passage_stays_american(self):
        assert passage_country("The Fed held rates. U.S. unemployment was 4.1%, BLS said.") is None
        assert passage_country("U.S. exports to Japan rose; the Bureau of Labor Statistics said U.S. prices fell.") is None

    def test_others(self):
        assert passage_country("The ECB cut rates as euro area inflation fell to 2%.") == "the euro area"
        assert passage_country("The RBI said the rupee weakened; inflation was 5%.") == "India"

    def test_the_place_is_written_into_the_sentence(self):
        assert with_place("Inflation was 3% in 2025", "Japan") == "In Japan, inflation was 3% in 2025"
        assert with_place("GDP grew 2%", "Japan") == "In Japan, GDP grew 2%"


def test_the_picked_sentence_is_checked_as_japan():
    seen = {}

    async def fake(claim, allow_grounding=True, client_ip=None, said_on=""):
        seen["claim"] = claim
        return VerificationV2(claim=claim, verdict="NO_VERDICT", confidence="LOW", headline="x")

    with patch.object(verify_v2, "_verify_one", AsyncMock(side_effect=fake)):
        v = asyncio.run(verify_v2.verify(IMF_JAPAN))
    assert seen["claim"].startswith("In Japan, ")
    assert any("about Japan" in c for c in v.caveats)
    assert any(c.startswith("Only one sentence") and "In Japan" not in c for c in v.caveats)


def test_a_us_paragraph_is_unchanged():
    seen = {}

    async def fake(claim, allow_grounding=True, client_ip=None, said_on=""):
        seen["claim"] = claim
        return VerificationV2(claim=claim, verdict="NO_VERDICT", confidence="LOW", headline="x")

    us = ("The labor market cooled through the year as hiring slowed across most industries and sectors. "
          "Unemployment rose to 4.2% in 2024 according to the Bureau of Labor Statistics and its survey. "
          "Wage growth slowed as well while inflation eased toward the Federal Reserve's target of two percent.")
    with patch.object(verify_v2, "_verify_one", AsyncMock(side_effect=fake)):
        asyncio.run(verify_v2.verify(us))
    assert not seen["claim"].startswith("In ")


class TestWhoOwnsTheFigure:
    """Found in the live test: "Despite a stronger euro and slower growth in
    Germany, U.S. GDP grew 2.8% in 2024" was held against Germany's GDP."""

    def test_the_place_named_last_before_the_figure_owns_it(self):
        from claims.geography import resolve_geography as g
        assert g("Despite a stronger euro and slower growth in Germany, U.S. GDP grew 2.8% in 2024.").level == "nation"
        assert g("Unlike in China, American inflation fell to 3% in 2023").level == "nation"
        assert g("German GDP shrank 0.5% in 2024 while the U.S. economy grew").name == "Germany"

    def test_one_place_is_unchanged(self):
        from claims.geography import resolve_geography as g
        assert g("Unemployment in Texas was 4.1% in 2023").name == "Texas"
        assert g("Canada unemployment was 6.9% in 2025").name == "Canada"
        assert g("U.S. GDP grew 2.8% in 2024").level == "nation"


def test_a_year_is_never_contradicted_by_one_month():
    """Found in the live test: "Inflation eased to 2.1% in 2025" (the euro
    area's annual rate) was called contradicted by December's 1.9%."""
    from evidence.grounded import _build_prompt
    p = _build_prompt("x")
    assert "One month's figure (December's rate) is a" in p and "never set CONTRADICTED from it" in p


def test_the_world_bank_check_sees_us_with_a_dot():
    """'U.S. GDP' was not seen as the United States (no word boundary after
    a dot), so the World Bank check answered with Germany's GDP."""
    from services import worldbank as w
    assert w._US.search("Despite slower growth in Germany, U.S. GDP grew 2.8% in 2024.")
    assert not w._US.search("Germany's GDP shrank 0.5% in 2024")
