"""Area Median Income claims against HUD's income limits (stubbed HUD API)."""
import pytest

import config
from claims.verdict import CONTRADICTED, NO_VERDICT, SUPPORTED
from evidence import cache as cache_mod
from evidence.sources import hud
from services import ami
from services.verify_v2 import verify

METROS = [{"cbsa_code": "METRO12420M12420", "area_name": "Austin-Round Rock-San Marcos, TX MSA"},
          {"cbsa_code": "METRO19740M19740", "area_name": "Denver-Aurora-Centennial, CO MSA"}]
COUNTIES_TX = [{"fips_code": "4845399999", "county_name": "Travis County"}]
AUSTIN = {"data": {
    "area_name": "Austin-Round Rock-San Marcos, TX MSA", "year": "2026", "median_income": 126000,
    "extremely_low": {f"il30_p{n}": v for n, v in enumerate(
        [26500, 30300, 34100, 37850, 40900, 43950, 46950, 50000], 1)},
    "very_low": {f"il50_p{n}": v for n, v in enumerate(
        [44150, 50450, 56750, 63050, 68100, 73150, 78200, 83250], 1)},
    "low": {f"il80_p{n}": v for n, v in enumerate(
        [70650, 80750, 90850, 100900, 109000, 117050, 125150, 133200], 1)},
}}


@pytest.fixture
def hud_api(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "HUD_API_TOKEN", "t")
    calls = []

    async def get(path, params=None):
        calls.append(path)
        if path == "/fmr/listMetroAreas":
            return METROS
        if path == "/fmr/listCounties/TX":
            return COUNTIES_TX
        if path in ("/il/data/METRO12420M12420", "/il/data/4845399999"):
            return AUSTIN
        return None
    monkeypatch.setattr(hud, "_get", get)
    return calls


@pytest.mark.parametrize("claim,expected", [
    ("80% of AMI for a family of four in Austin is $100,900.", SUPPORTED),
    ("80% of AMI for a family of four in Austin is $150,000.", CONTRADICTED),
    ("The area median income in Travis County, Texas is $126,000.", SUPPORTED),
    ("Very low income for a family of 3 in Austin is $56,750.", SUPPORTED),
    ("60% AMI for a two-person household in Austin is $60,540.", SUPPORTED),  # 1.2 x 50,450
])
async def test_ami_claims(hud_api, claim, expected):
    r = await verify(claim, allow_grounding=False)
    assert r.verdict == expected, r.headline
    assert r.model_calls == 0 and r.evidence and r.evidence[0]["source"] == "HUD"


async def test_household_size_defaults_to_four_and_says_so(hud_api):
    r = await verify("80% AMI in Austin is $100,900.", allow_grounding=False)
    assert r.verdict == SUPPORTED
    assert any("family of four" in c for c in r.caveats)


async def test_without_a_token_nothing_is_called(monkeypatch):
    monkeypatch.setattr(config, "HUD_API_TOKEN", None)
    r = await verify("80% of AMI for a family of four in Austin is $100,900.", allow_grounding=False)
    assert r.verdict == NO_VERDICT


def test_reading():
    assert ami.read("80% of AMI for a family of four") == (80, 4, True)
    assert ami.read("extremely low income limit for a single person in Denver is $25,000") == (30, 1, True)
    assert ami.read("Low-income families struggle.") is None


async def test_lookup_returns_hud_figure(hud_api):
    from services.lookup import lookup
    r = await lookup("80% AMI Austin TX family of 4")
    assert r.ok and "100,900" in (r.headline or ""), r.headline
