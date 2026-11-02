"""Crime claims checked against the FBI Crime Data Explorer (/summarized),
stubbed with the API's real answer shape and real monthly values."""
import httpx
import pytest

from claims.parser import parse_claim
from claims.verdict import CONTRADICTED, MEDIUM, SUPPORTED
from evidence import cache as cache_mod
from evidence.sources import SourceUnavailable, fbi_crime

# The real request code, kept before conftest's offline stub replaces it.
_REAL_GET = fbi_crime._get
from services import crime

# Real monthly figures from /summarized (national V 2022, HOM 2023; Texas V 2022).
REAL = {
    ("US", "V", 2022): dict(
        rates=[30.51, 28.11, 32.22, 32.42, 35.41, 34.38, 35.97, 34.45, 33.6, 33.43, 29.52, 38.13],
        actuals=[96216, 88867, 103141, 103855, 113675, 110297, 115496, 110351, 107061, 106472, 94017, 121850],
        coverage=[93.65, 93.9, 95.07, 95.13, 95.32, 95.28, 95.36, 95.13, 94.62, 94.59, 94.57, 94.89],
        population=336746447),
    ("US", "HOM", 2023): dict(
        rates=[0.51, 0.43, 0.48, 0.49, 0.52, 0.53, 0.57, 0.5, 0.48, 0.49, 0.44, 0.65],
        actuals=[1662, 1370, 1557, 1582, 1690, 1714, 1847, 1643, 1572, 1600, 1448, 2110],
        coverage=[95.55, 95, 95.89, 96, 95.83, 95.97, 96.02, 96.25, 96.39, 96.24, 96.23, 96.22],
        population=338357687),
    # 2021: the NIBRS transition year, about 77% coverage
    ("US", "HOM", 2021): dict(
        rates=[0.5, 0.45, 0.55, 0.56, 0.61, 0.62, 0.65, 0.6, 0.57, 0.6, 0.55, 0.66],
        actuals=[1270, 1150, 1400, 1420, 1560, 1590, 1660, 1530, 1450, 1530, 1400, 1972],
        coverage=[74.69] + [77.2] * 11, population=335395039),
    ("US", "ROB", 2022): dict(
        rates=[5.5, 5.0, 5.6, 5.6, 5.9, 5.8, 6.1, 6.0, 5.9, 6.1, 5.8, 6.4],
        actuals=[17600, 15900, 17900, 17900, 18900, 18500, 19500, 19200, 18900, 19500, 18500, 20164],
        coverage=[93.65, 93.9, 95.07, 95.13, 95.32, 95.28, 95.36, 95.13, 94.62, 94.59, 94.57, 94.89],
        population=336746447),
    ("TX", "V", 2022): dict(
        rates=[36.28, 32.2, 37.46, 38.77, 42.28, 38.52, 39.67, 37.46, 36.28, 36.58, 32.37, 34.44],
        actuals=[10759, 9554, 11109, 11499, 12554, 11436, 11770, 11124, 10763, 10852, 9613, 10222],
        coverage=[98.75, 98.8, 98.75, 98.78, 98.87, 98.86, 98.81, 98.9, 98.79, 98.79, 98.91, 98.84],
        population=30029572),
}
NAMES = {"US": "United States", "TX": "Texas"}


def _answer(state, offense, year, months=12):
    """A /summarized answer as the API writes it; a state answer also carries the U.S. rates."""
    real = REAL.get((state, offense, year))
    if real is None:
        return {"offenses": {"rates": {}, "actuals": {}}, "tooltips": {}, "populations": {},
                "cde_properties": {"max_data_date": {"UCR": "10/2026"}}}
    place = NAMES[state]
    keys = [f"{m:02d}-{year}" for m in range(1, months + 1)]
    series = lambda vals: dict(zip(keys, vals))  # noqa: E731
    d = {
        "offenses": {
            "rates": {f"{place} Offenses": series(real["rates"]),
                      f"{place} Clearances": series([round(v * 0.37, 2) for v in real["rates"]])},
            "actuals": {f"{place} Offenses": series(real["actuals"]),
                        f"{place} Clearances": series([int(v * 0.37) for v in real["actuals"]])},
        },
        "tooltips": {"leftYAxisHeaders": {"yAxisHeaderRates": "Offenses per 100,000 people",
                                          "yAxisHeaderActual": "Offenses"},
                     "Percent of Population Coverage": {place: series(real["coverage"])}},
        "populations": {"population": {place: series([real["population"]] * 12)},
                        "participated_population": {place: series([int(real["population"] * 0.95)] * 12)}},
        "cde_properties": {"max_data_date": {"UCR": "10/2026"}, "last_refresh_date": {"UCR": "10/15/2026"}},
    }
    if state != "US":
        d["offenses"]["rates"]["United States Offenses"] = series(REAL[("US", "V", 2022)]["rates"])
    return d


@pytest.fixture
def fbi(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
    calls = []

    async def get(path, params):
        calls.append((path, dict(params)))
        parts = path.strip("/").split("/")
        state = parts[2] if parts[1] == "state" else "US"
        offense = parts[-1]
        year = int(params["from"][-4:])
        if year == 2026:  # published through October only
            REAL[("US", "V", 2026)] = REAL[("US", "V", 2022)]
            return _answer("US", "V", 2026, months=10)
        return _answer(state, offense, year)
    monkeypatch.setattr(fbi_crime, "_get", get)
    return calls


async def _check(text):
    return await crime.check(text, parse_claim(text))


async def test_national_rate_matches(fbi):
    v = await _check("The U.S. violent crime rate was 380 per 100,000 in 2022.")
    assert v.verdict == SUPPORTED and v.confidence == MEDIUM
    assert v.headline.startswith("FBI Uniform Crime Reporting (offenses reported by agencies covering 95%")
    assert "398.1 per 100,000 people in 2022" in v.headline
    assert fbi == [("/summarized/national/V", {"from": "01-2022", "to": "12-2022"})]
    dp = v.evidence[0]
    assert dp.source == "FBI" and dp.source_url.startswith("https://cde.ucr.cjis.gov/")
    assert any("estimates" in c for c in v.caveats)


async def test_near_match_on_a_murder_rate(fbi):
    # The FBI's estimate for 2023 is 5.7; agencies' reported rate sums to 6.09.
    v = await _check("The U.S. murder rate was 5.7 per 100,000 in 2023.")
    assert v.verdict == SUPPORTED
    assert "6.09 per 100,000" in v.headline


async def test_count_with_a_hedge(fbi):
    v = await _check("There were about 19,000 murders in 2023.")
    assert v.verdict == SUPPORTED
    assert "19,795 murders and nonnegligent manslaughters were reported in 2023" in v.headline


async def test_contradiction(fbi):
    v = await _check("There were 300,000 robberies in 2022.")
    assert v.verdict == CONTRADICTED
    assert "the claim says 300,000" in v.headline


async def test_too_close_to_call_goes_to_search(fbi):
    # 345 is 13% under the reported 398: neither a match nor clearly wrong.
    assert await _check("The U.S. violent crime rate was 345 per 100,000 in 2022.") is None


@pytest.mark.parametrize("claim,expected", [
    ("More than 1 million violent crimes were reported in 2022.", SUPPORTED),
    ("Fewer than 1 million violent crimes were reported in 2022.", CONTRADICTED),
    ("There were 1.2 million violent crimes in the US in 2022.", SUPPORTED),
])
async def test_comparators(fbi, claim, expected):
    v = await _check(claim)
    assert v is not None and v.verdict == expected, v and v.headline


async def test_state_level(fbi):
    v = await _check("Texas had a violent crime rate of 432 per 100,000 in 2022.")
    assert v.verdict == SUPPORTED
    assert fbi[0][0] == "/summarized/state/TX/V"
    assert "violent crime rate in Texas was 442.3" in v.headline
    assert v.evidence[0].geography.name == "Texas"


async def test_2021_rate_carries_the_nibrs_caveat_and_counts_are_left_alone(fbi):
    v = await _check("The murder rate was 6.8 per 100,000 in 2021.")
    assert v.verdict == SUPPORTED
    assert any("NIBRS" in c for c in v.caveats)
    assert await _check("There were 22,900 murders in 2021.") is None  # 77% coverage


async def test_unpublished_year(fbi):
    assert await _check("The U.S. violent crime rate was 350 per 100,000 in 2026.") is None


@pytest.mark.parametrize("claim", [
    "Chicago had 617 murders in 2023.",                         # a city the parser did not resolve
    "The murder rate in New York City was 4.5 in 2023.",
    "There were 24,000 homicides in 2021.",                      # CDC's count or the FBI's?
    "The U.S. murder rate fell to 5.7 per 100,000 in 2023.",     # a change
    "Violent crime rose 5% in 2022.",
    "Police made 7 million arrests in 2022.",
    "Gun murders rose to 20,000 in 2021.",
    "Violent crime and property crime totaled 8 million in 2022.",
    "The unemployment rate was 3.6% in 2022.",
])
async def test_claims_this_check_leaves_alone(fbi, claim):
    assert await _check(claim) is None
    assert fbi == []


async def test_outage_is_not_an_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)

    async def down(path, params):
        raise SourceUnavailable("FBI", "Crime Data Explorer rate limit reached")
    monkeypatch.setattr(fbi_crime, "_get", down)
    assert await _check("The U.S. violent crime rate was 380 per 100,000 in 2022.") is None


async def test_key_travels_in_a_header_and_rate_limits_raise(monkeypatch):
    seen = []
    real_client = httpx.AsyncClient

    def handler(request):
        seen.append(request)
        return httpx.Response(429, json={"error": {"code": "OVER_RATE_LIMIT"}})

    monkeypatch.setattr(fbi_crime.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(SourceUnavailable):
        await _REAL_GET("/summarized/national/V", {"from": "01-2022", "to": "12-2022"})
    assert seen[0].headers["X-Api-Key"]
    assert "api_key" not in str(seen[0].url).lower() and "DEMO_KEY" not in str(seen[0].url)
