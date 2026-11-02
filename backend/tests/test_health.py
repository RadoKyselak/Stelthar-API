"""Health claims checked against CDC / NCHS data on data.cdc.gov, stubbed
with rows copied from the real datasets."""
import re

import httpx
import pytest

from claims.parser import parse_claim
from claims.verdict import CONTRADICTED, HIGH, MEDIUM, SUPPORTED
from evidence import cache as cache_mod
from evidence.sources import SourceUnavailable, cdc

# The real request code, kept before conftest's offline stub replaces it.
_REAL_GET = cdc._get
from services import health

# Rows as data.cdc.gov returns them (strings throughout).
TABLES = {
    "w9j2-ggv5": [
        {"year": "2018", "race": "All Races", "sex": "Both Sexes", "average_life_expectancy": "78.7", "mortality": "723.6"},
        {"year": "2018", "race": "All Races", "sex": "Female", "average_life_expectancy": "81.2", "mortality": "611.3"},
        {"year": "2018", "race": "All Races", "sex": "Male", "average_life_expectancy": "76.2", "mortality": "855.5"},
        {"year": "2018", "race": "White", "sex": "Both Sexes", "mortality": "725.4"},
    ],
    "ncvk-7amm": [
        {"state": "United States", "sex": "Total", "leb": "78.8", "quartile": "*"},
        {"state": "United States", "sex": "Male", "leb": "76.3", "quartile": "*"},
        {"state": "United States", "sex": "Female", "leb": "81.4", "quartile": "*"},
    ],
    "ss2j-8ajj": [
        {"state": "United States", "sex": "Total", "le": "77", "quartile": "*"},
        {"state": "United States", "sex": "Male", "le": "74.2", "quartile": "*"},
    ],
    "it4f-frdc": [
        {"area": "Mississippi", "sex": "Total", "leb": "70.9", "se": "0.092", "quartile": "70.9 - 74.5"},
        {"area": "West Virginia", "sex": "Total", "leb": "71.0", "se": "0.117", "quartile": "70.9 - 74.5"},
    ],
    "xkb8-kh2a": [
        {"state": "US", "year": "2022", "month": "December", "period": "12 month-ending",
         "indicator": "Number of Drug Overdose Deaths", "data_value": "109413", "percent_complete": "100",
         "percent_pending_investigation": "0.160081761", "state_name": "United States",
         "footnote": "Numbers may differ from published reports using final data. See Technical Notes.",
         "footnote_symbol": "**", "predicted_value": "110393"},
        {"state": "US", "year": "2021", "month": "December", "period": "12 month-ending",
         "indicator": "Synthetic opioids, excl. methadone (T40.4)", "data_value": "71143",
         "percent_complete": "100", "state_name": "United States", "predicted_value": "71746"},
        {"state": "TX", "year": "2022", "month": "December", "period": "12 month-ending",
         "indicator": "Number of Drug Overdose Deaths", "data_value": "5457", "percent_complete": "100",
         "state_name": "Texas", "predicted_value": "5596"},
    ],
    "bi63-dtpu": [
        {"year": "2017", "_113_cause_name": "Diseases of heart (I00-I09,I11,I13,I20-I51)",
         "cause_name": "Heart disease", "state": "United States", "deaths": "647457", "aadr": "165.0"},
        {"year": "2017", "_113_cause_name": "Intentional self-harm (suicide) (*U03,X60-X84,Y87.0)",
         "cause_name": "Suicide", "state": "United States", "deaths": "47173", "aadr": "14.0"},
    ],
}
# 9dzk-mvmi answers an aggregate: sum over the year's months.
MONTHLY = {("2022", "diseases_of_heart"): ("704786", "12"), ("2023", "diseases_of_heart"): ("469163", "9")}


def _match(row, where):
    for col, val in re.findall(r"(\w+)='((?:[^']|'')*)'", where):
        if row.get(col) != val.replace("''", "'"):
            return False
    return True


@pytest.fixture
def soda(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
    calls = []

    async def get(dataset, params):
        calls.append((dataset, dict(params)))
        where = params.get("$where", "")
        if dataset == "9dzk-mvmi":
            year = re.search(r"year='(\d{4})'", where).group(1)
            column = re.search(r"sum\((\w+)\)", params["$select"]).group(1)
            got = MONTHLY.get((year, column))
            return [{"deaths": got[0], "months": got[1]}] if got else [{"months": "0"}]
        return [r for r in TABLES.get(dataset, []) if _match(r, where)]
    monkeypatch.setattr(cdc, "_get", get)
    return calls


async def _check(text):
    return await health.check(text, parse_claim(text))


async def test_life_expectancy_matches(soda):
    v = await _check("U.S. life expectancy was 78.7 years in 2018.")
    assert v.verdict == SUPPORTED and v.confidence == HIGH
    assert v.headline == ("CDC National Center for Health Statistics: U.S. life expectancy at birth "
                          "was 78.7 years in 2018.")
    assert v.evidence[0].source_url == "https://data.cdc.gov/d/w9j2-ggv5"


async def test_life_expectancy_by_sex(soda):
    v = await _check("Life expectancy for American women was 81.4 years in 2019.")
    assert v.verdict == SUPPORTED and "for females was 81.4 years in 2019" in v.headline
    assert soda[0][0] == "ncvk-7amm" and "sex='Female'" in soda[0][1]["$where"]


async def test_life_expectancy_contradicted(soda):
    v = await _check("U.S. life expectancy was 82 years in 2018.")
    assert v.verdict == CONTRADICTED and "the claim says 82" in v.headline


async def test_state_life_expectancy(soda):
    v = await _check("Life expectancy in Mississippi was 70.9 years in 2021.")
    assert v.verdict == SUPPORTED and "Mississippi's life expectancy at birth was 70.9" in v.headline
    assert v.evidence[0].geography.name == "Mississippi"


async def test_years_not_on_data_cdc_gov_go_to_search(soda):
    assert await _check("U.S. life expectancy was 77.5 years in 2022.") is None
    assert await _check("U.S. life expectancy was 76.4 years in 2021.") is None  # no U.S. row for 2021


async def test_overdoses_with_a_comparator(soda):
    v = await _check("More than 100,000 Americans died of drug overdoses in 2022.")
    assert v.verdict == SUPPORTED and v.confidence == MEDIUM
    assert "(provisional counts): there were 109,413 drug overdose deaths in the 12 months ending December 2022" \
        in v.headline
    assert any("provisional" in c for c in v.caveats)


async def test_overdoses_final_figure_is_a_near_match(soda):
    # NCHS's final 2022 count is 107,941; the provisional one 109,413.
    v = await _check("There were 107,941 drug overdose deaths in 2022.")
    assert v.verdict == SUPPORTED


async def test_overdoses_contradicted(soda):
    v = await _check("Drug overdoses killed 150,000 Americans in 2022.")
    assert v.verdict == CONTRADICTED and "the claim says 150,000" in v.headline


async def test_fentanyl_reads_synthetic_opioids_and_says_so(soda):
    v = await _check("Fentanyl killed 70,000 Americans in 2021.")
    assert v.verdict == SUPPORTED
    assert "synthetic opioids other than methadone" in v.headline
    assert any("fentanyl" in c for c in v.caveats)


async def test_state_overdoses(soda):
    v = await _check("Texas recorded 5,400 overdose deaths in 2022.")
    assert v.verdict == SUPPORTED and "5,457 drug overdose deaths in Texas" in v.headline
    assert "state='TX'" in soda[0][1]["$where"]


async def test_overdoses_for_a_year_not_out_yet(soda):
    assert await _check("There were 60,000 drug overdose deaths in 2026.") is None


async def test_cause_of_death_final_and_provisional(soda):
    v = await _check("Suicide claimed 47,173 lives in 2017.")
    assert v.verdict == SUPPORTED and v.confidence == HIGH and "(final counts)" in v.headline
    v = await _check("Heart disease killed 700,000 Americans in 2022.")
    assert v.verdict == SUPPORTED and v.confidence == MEDIUM and "704,786 deaths from heart disease" in v.headline
    v = await _check("Heart disease killed 900,000 Americans in 2017.")
    assert v.verdict == CONTRADICTED


async def test_partial_year_of_causes(soda):
    assert await _check("Heart disease killed 680,000 Americans in 2023.") is None  # 9 months published


@pytest.mark.parametrize("claim", [
    "Lung cancer killed 130,000 Americans in 2022.",
    "The overdose death rate was 32 per 100,000 in 2022.",
    "Black life expectancy was 71 years in 2021.",
    "Overdose deaths fell to 80,000 in 2024.",
    "Life expectancy in New York City was 80 years in 2019.",
    "Drug overdoses among teens reached 1,500 in 2022.",
    "Heroin and cocaine killed 30,000 people in 2021.",
    "Diabetes affects 38 million Americans in 2021.",
])
async def test_claims_this_check_leaves_alone(soda, claim):
    assert await _check(claim) is None


async def test_outage_is_not_an_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)

    async def down(dataset, params):
        raise SourceUnavailable("CDC", "HTTP 503")
    monkeypatch.setattr(cdc, "_get", down)
    assert await _check("U.S. life expectancy was 78.7 years in 2018.") is None


async def test_http_errors_raise_source_unavailable(monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(cdc.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kw))
    with pytest.raises(SourceUnavailable):
        await _REAL_GET("xkb8-kh2a", {"$where": "year='2022'"})
