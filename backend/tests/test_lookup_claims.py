"""Extension 2.1.2 and older send highlighted text without a figure to
/v2/lookup. A sentence there gets the claim check, drawn as a lookup card."""
import pytest

from services import lookup as lookup_mod
from services import verify_compat

HEADLINE = ("2026 Recalls of Food Products Associated with Fresh Jalapeños from Sinaloa, Mexico and "
            "distributed by Coast Citrus Distributors due to the Potential Risk of Salmonella")


@pytest.fixture
def checked(monkeypatch):
    asked = []

    async def verify(claim, client_ip=None):
        asked.append(claim)
        if "pineapple" in claim.lower():
            return {"verdict_detail": "No verdict", "headline": "That reads as an opinion.", "degraded": False}
        return {"verdict_detail": "Supported", "confidence_tier": "Medium", "model_calls": 0,
                "headline": "FDA's recall records show it: July 22, 2026 · Class I · Salmonella Javiana.",
                "evidence": [{"source": "FDA", "source_url": "https://www.accessdata.fda.gov/scripts/ires/index.cfm"}],
                "caveats": [], "citations": []}
    monkeypatch.setattr(verify_compat, "verify", verify)
    return asked


@pytest.mark.parametrize("text,expected", [
    (HEADLINE, True),
    ("Vaccines cause autism", True),
    ("Is the minimum wage $7.25?", True),
    ("unemployment texas 2023", False),
    ("poverty rate 2023", False),
    ("what was unemployment in 2023", False),
])
def test_reads_as_claim(text, expected):
    assert lookup_mod.reads_as_claim(text) is expected


async def test_sentence_gets_the_claim_check(checked):
    r = await lookup_mod.lookup(HEADLINE, deep=True)
    assert checked == [HEADLINE]
    assert r.ok and r.value == "Supported" and r.label == "Claim check"
    assert "July 22, 2026" in r.note and r.citations["agency"] == "FDA"
    assert r.citations["url"].startswith("https://www.accessdata.fda.gov")


async def test_no_verdict_stays_a_miss(checked):
    r = await lookup_mod.lookup("Pineapple is the best pizza topping, and that is that", deep=True)
    assert not r.ok and r.headline == "That reads as an opinion."


async def test_typing_never_runs_a_check(checked):
    await lookup_mod.lookup(HEADLINE, deep=False)
    assert checked == []


async def test_a_measure_is_still_looked_up(checked):
    await lookup_mod.lookup("poverty rate 2023", deep=True)
    assert checked == []
