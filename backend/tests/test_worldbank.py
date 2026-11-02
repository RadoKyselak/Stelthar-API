"""Country figures checked against the World Bank's World Development
Indicators (stubbed; the shapes and values are api.worldbank.org/v2 answers
as fetched in November 2026, lastupdated 2026-10-08)."""
import pytest

from claims.parser import parse_claim
from claims.verdict import CONTRADICTED, HIGH, MEDIUM, MOSTLY_ACCURATE, SUPPORTED
from evidence import cache as cache_mod
from evidence.sources import SourceUnavailable, worldbank as wb_source
from services import worldbank


def _country(iso3, iso2, name, capital, region="Europe & Central Asia"):
    return {"id": iso3, "iso2Code": iso2, "name": name,
            "region": {"id": "ECS", "iso2code": "Z7", "value": region},
            "adminregion": {"id": "", "iso2code": "", "value": ""},
            "incomeLevel": {"id": "HIC", "iso2code": "XD", "value": "High income"},
            "lendingType": {"id": "LNX", "iso2code": "XX", "value": "Not classified"},
            "capitalCity": capital, "longitude": "", "latitude": ""}


def _aggregate(iso3, iso2, name):
    return {"id": iso3, "iso2Code": iso2, "name": name,
            "region": {"id": "NA", "iso2code": "NA", "value": "Aggregates"},
            "adminregion": {"id": "", "iso2code": "", "value": ""},
            "incomeLevel": {"id": "NA", "iso2code": "NA", "value": "Aggregates"},
            "lendingType": {"id": "", "iso2code": "", "value": "Aggregates"},
            "capitalCity": "", "longitude": "", "latitude": ""}


COUNTRIES = [
    {"page": 1, "pages": 1, "per_page": "400", "total": 15},
    [_country("DEU", "DE", "Germany", "Berlin"), _country("IND", "IN", "India", "New Delhi", "South Asia"),
     _country("JPN", "JP", "Japan", "Tokyo", "East Asia & Pacific"),
     _country("GBR", "GB", "United Kingdom", "London"), _country("ESP", "ES", "Spain", "Madrid"),
     _country("KOR", "KR", "Korea, Rep.", "Seoul", "East Asia & Pacific"),
     _country("PRK", "KP", "Korea, Dem. People's Rep.", "Pyongyang", "East Asia & Pacific"),
     _country("COD", "CD", "Congo, Dem. Rep.", "Kinshasa", "Sub-Saharan Africa "),
     _country("COG", "CG", "Congo, Rep.", "Brazzaville", "Sub-Saharan Africa "),
     _country("TUR", "TR", "Turkiye", "Ankara"), _country("GEO", "GE", "Georgia", "Tbilisi"),
     _country("USA", "US", "United States", "Washington D.C.", "North America"),
     _country("PRI", "PR", "Puerto Rico (US)", "San Juan", "Latin America & Caribbean "),
     _aggregate("WLD", "1W", "World"), _aggregate("EMU", "XC", "Euro area")],
]

_NAMES = {
    "SL.UEM.TOTL.ZS": "Unemployment, total (% of total labor force) (modeled ILO estimate)",
    "SP.POP.TOTL": "Population, total",
    "NY.GDP.MKTP.KD.ZG": "GDP growth (annual %)",
    "NY.GDP.MKTP.CD": "GDP (current US$)",
    "FP.CPI.TOTL.ZG": "Inflation, consumer prices (annual %)",
    "SP.DYN.TFRT.IN": "Fertility rate, total (births per woman)",
    "SP.DYN.LE00.IN": "Life expectancy at birth, total (years)",
    "MS.MIL.XPND.GD.ZS": "Military expenditure (% of GDP)",
}
_ISO2 = {"DEU": ("DE", "Germany"), "IND": ("IN", "India"), "JPN": ("JP", "Japan"),
         "GBR": ("GB", "United Kingdom"), "ESP": ("ES", "Spain"), "KOR": ("KR", "Korea, Rep."),
         "TUR": ("TR", "Turkiye")}

# (iso3, code): {year: value}, newest first, as the API lists them.
SERIES = {
    ("DEU", "SL.UEM.TOTL.ZS"): {2025: 3.711, 2024: 3.4, 2023: 3.071, 2022: 3.137, 2021: 3.594},
    ("ESP", "SL.UEM.TOTL.ZS"): {2025: 10.376, 2024: 11.4, 2023: 12.179, 2022: 13.033},
    ("IND", "SP.POP.TOTL"): {2025: 1463865525, 2024: 1450935791, 2023: 1438069596, 2022: 1425423212},
    ("DEU", "SP.POP.TOTL"): {2025: 83491249, 2024: 83516593, 2023: 83287273, 2022: 83177813},
    ("JPN", "NY.GDP.MKTP.KD.ZG"): {2025: 1.19311288444599, 2024: -0.240084236041696,
                                   2023: 0.720785304749811, 2022: 1.33164750653741},
    ("DEU", "NY.GDP.MKTP.KD.ZG"): {2025: 0.239578342117881, 2024: -0.495851897260366,
                                   2023: -0.869647578573321, 2022: 1.8092581243061},
    ("GBR", "FP.CPI.TOTL.ZG"): {2025: 3.88306881625999, 2024: 3.2715729463592, 2023: 6.79396706793962},
    ("TUR", "FP.CPI.TOTL.ZG"): {2025: 34.8811700293777, 2024: 58.5064492168435, 2023: 53.8594084019764},
    ("KOR", "SP.DYN.TFRT.IN"): {2025: None, 2024: 0.748, 2023: 0.721, 2022: 0.778},
    ("JPN", "SP.DYN.LE00.IN"): {2025: None, 2024: 84.0363414634146, 2023: 84.0412195121951},
    ("JPN", "NY.GDP.MKTP.CD"): {2025: 4279828472980.84, 2024: 4027597523596.11, 2023: 4204494802431.55},
}


def _series_payload(iso3, code):
    data = SERIES.get((iso3, code))
    if data is None:
        return [{"message": [{"id": "120", "key": "Invalid value",
                              "value": "The provided parameter value is not valid"}]}]
    iso2, name = _ISO2[iso3]
    rows = [{"indicator": {"id": code, "value": _NAMES[code]}, "country": {"id": iso2, "value": name},
             "countryiso3code": iso3, "date": str(y), "value": v, "unit": "", "obs_status": "",
             "decimal": 1} for y, v in data.items()]
    return [{"page": 1, "pages": 1, "per_page": 100, "total": len(rows), "sourceid": "2",
             "lastupdated": "2026-10-08"}, rows]


@pytest.fixture
def wb(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
    calls = []

    async def get(path, params):
        calls.append(path)
        if path == "/country":
            return COUNTRIES
        _, _, iso3, _, code = path.split("/")
        return _series_payload(iso3, code)
    monkeypatch.setattr(wb_source, "_get", get)
    return calls


async def _check(text):
    return await worldbank.check(text, parse_claim(text))


async def test_match(wb):
    v = await _check("Germany's unemployment rate was 3% in 2023.")
    assert v.verdict == SUPPORTED and v.confidence == HIGH, v.headline
    assert v.headline == ("World Bank (World Development Indicators): Germany's unemployment rate was "
                          "3.1% in 2023 (ILO modeled estimate).")
    dp = v.evidence[0]
    assert dp.source == "World Bank" and dp.series_id == "SL.UEM.TOTL.ZS"
    assert dp.source_url == "https://data.worldbank.org/indicator/SL.UEM.TOTL.ZS?locations=DE"
    assert dp.observed.year == 2023 and dp.quantity.magnitude == 3.071
    assert dp.geography.name == "Germany" and dp.as_of == "2026-10-08"
    assert any("International Labour Organization" in c for c in v.caveats)
    rows = dp.extra["excerpt"]["rows"]
    assert [r for r in rows if r.get("cited")] == [{"label": "2023", "value": "3.1%", "cited": True}]


async def test_near_match_is_mostly_accurate(wb):
    v = await _check("Germany's unemployment rate was 3.2% in 2023.")
    assert v.verdict == MOSTLY_ACCURATE, v.headline
    assert "the claim says 3.2%, off by 0.13 percentage point" in v.headline
    assert v.confidence == MEDIUM  # a national measure can differ from the ILO estimate


async def test_contradiction(wb):
    v = await _check("Spain's unemployment rate was 20% in 2023.")
    assert v.verdict == CONTRADICTED and "was 12.2% in 2023" in v.headline and "the claim says 20%" in v.headline


async def test_contradiction_hard_measure_is_high_confidence(wb):
    v = await _check("Germany's population was 90 million in 2023.")
    assert v.verdict == CONTRADICTED and v.confidence == HIGH, v.headline
    assert "83.29 million in 2023" in v.headline


@pytest.mark.parametrize("claim,expected", [
    ("India's population passed 1.4 billion in 2023.", SUPPORTED),       # 1.438 billion
    ("India's population passed 1.5 billion in 2023.", CONTRADICTED),
    ("Germany's population was more than 85 million in 2023.", CONTRADICTED),
    ("Germany's population was under 85 million in 2023.", SUPPORTED),
    ("Türkiye's inflation was over 50% in 2023.", SUPPORTED),            # 53.9%
    ("Turkey's inflation rate was about 54% in 2023.", SUPPORTED),
    ("Japan's GDP was about $4.2 trillion in 2023.", SUPPORTED),
    ("Japan's GDP was more than $5 trillion in 2023.", CONTRADICTED),
])
async def test_comparators(wb, claim, expected):
    v = await _check(claim)
    assert v is not None and v.verdict == expected, (claim, v and v.headline)


async def test_comparator_is_named_when_wrong(wb):
    v = await _check("India's population passed 1.5 billion in 2023.")
    assert v.headline.endswith("; the claim says more than 1.5 billion."), v.headline


async def test_growth_and_contraction(wb):
    v = await _check("Japan's GDP grew 1.9% in 2023.")
    assert v.verdict == CONTRADICTED and v.confidence == MEDIUM, v.headline
    assert "Japan's economy grew 0.7% in 2023" in v.headline and "it grew 1.9%" in v.headline
    v = await _check("Germany's economy shrank 0.9% in 2023.")
    assert v.verdict == SUPPORTED and "Germany's economy shrank 0.9% in 2023" in v.headline, v.headline
    v = await _check("Germany's economy grew 0.9% in 2023.")
    assert v.verdict == CONTRADICTED, v.headline


async def test_latest_year_when_none_is_named(wb):
    v = await _check("South Korea's fertility rate is 0.75.")
    assert v.verdict == SUPPORTED and v.evidence[0].observed.year == 2024, v.headline  # 2025 is not out yet
    assert "0.75 births per woman in 2024" in v.headline
    assert any("names no year" in c and "(2024)" in c for c in v.caveats)
    v = await _check("Life expectancy in Japan is more than 85 years.")
    assert v.verdict == CONTRADICTED and v.confidence == MEDIUM  # no year named: may mean another year


async def test_unpublished_year_is_left_to_search(wb):
    assert await _check("South Korea's fertility rate was 0.7 in 2025.") is None
    assert await _check("Spain's unemployment rate was 12% in 2030.") is None


@pytest.mark.parametrize("claim", [
    "The unemployment rate in 2023 was 3.6%.",
    "The U.S. unemployment rate was 3.6% in 2023.",
    "America's GDP grew 2.5% in 2023.",
    "The unemployment rate in Texas was 4% in 2023.",
    "Georgia's population is 11 million.",
    "Puerto Rico's population was 3.2 million in 2023.",
    "Germany's unemployment rate was lower than the U.S. rate in 2023 at 3%.",
])
async def test_us_claims_are_not_ours(wb, claim):
    assert await _check(claim) is None


@pytest.mark.parametrize("claim", [
    "Wakanda's GDP was $1 trillion in 2023.",
    "Congo's population was 100 million in 2023.",            # which Congo?
    "Korea's fertility rate was 0.72 in 2023.",               # which Korea?
    "The world's population passed 8 billion in 2023.",
    "Unemployment in the euro area was 6.5% in 2023.",
])
async def test_unknown_or_ambiguous_place(wb, claim):
    assert await _check(claim) is None


@pytest.mark.parametrize("claim", [
    "Youth unemployment in Spain was 28% in 2023.",            # a subgroup
    "Tokyo's population is 14 million.",                       # a city
    "The population of Tokyo, Japan is 14 million.",
    "Scotland's unemployment rate in the UK was 4% in 2023.",
    "Germany's GDP was €4.1 trillion in 2023.",                # another currency
    "India's GDP at PPP was $14 trillion in 2023.",
    "Japan's GDP grew 2.9% in Q3 2023.",                       # a quarter
    "UK inflation hit 11.1% in October 2022.",                 # a month
    "Germany's unemployment rate rose from 3.1% in 2022 to 3.4% in 2024.",
    "India's population passed China's in 2023.",              # two countries, no figure
    "Japan has the highest life expectancy in the world.",
    "Germany's unemployment rate was not 3% in 2023.",
    "2 million people in Germany lost their jobs in 2023.",
])
async def test_claims_this_cannot_settle(wb, claim):
    assert await _check(claim) is None


async def test_aliases(wb):
    for claim in ("The UK's inflation rate was 6.8% in 2023.", "Britain's inflation was 6.8% in 2023.",
                  "Inflation in the United Kingdom was 6.8% in 2023."):
        v = await _check(claim)
        assert v is not None and v.verdict == SUPPORTED and v.evidence[0].geography.name == "United Kingdom", claim


async def test_aggregates_are_not_countries(wb):
    countries = await wb_source.countries()
    isos = {c["iso3"] for c in countries}
    assert "WLD" not in isos and "EMU" not in isos and "DEU" in isos


async def test_api_failure_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)

    async def down(path, params):
        raise SourceUnavailable("World Bank", "HTTP 503")
    monkeypatch.setattr(wb_source, "_get", down)
    assert await _check("Germany's unemployment rate was 3% in 2023.") is None


async def test_api_error_message_is_none(wb):
    # The API answers an unknown series with a message, HTTP 200.
    assert await wb_source.indicator("DEU", "XX.NOT.REAL") == []
    assert await _check("Germany's life expectancy was 81 years in 2023.") is None


async def test_indicator_by_year(wb):
    rows = await wb_source.indicator("DEU", "SL.UEM.TOTL.ZS", 2023)
    assert rows == [{"year": 2023, "value": 3.071, "decimal": 1, "indicator": _NAMES["SL.UEM.TOTL.ZS"],
                     "last_updated": "2026-10-08", "country": "Germany"}]
    latest = await wb_source.indicator("KOR", "SP.DYN.TFRT.IN")
    assert latest[0]["year"] == 2024  # 2025 is listed with no value


async def test_series_is_cached(wb):
    await _check("Germany's unemployment rate was 3% in 2023.")
    await _check("Germany's unemployment rate was 3.4% in 2024.")
    assert wb.count("/country/DEU/indicator/SL.UEM.TOTL.ZS") == 1 and wb.count("/country") == 1
