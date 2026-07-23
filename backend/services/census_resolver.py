"""Resolve friendly Census concepts + geographies into exact ACS API params.

The old flow required the LLM to hand-author ACS variable codes (e.g.
``DP05_0001E``) and FIPS geographies (``state:01``). It frequently guessed
wrong, yielding empty/204 responses that got silently dropped. This resolver
maps a high-value vocabulary of concepts and all state names to correct
parameters, so the planner can emit:

    {"concept": "population", "geography": "Alabama", "year": "2022"}

and get back a valid ACS query. Raw params (year/dataset/get/for) still pass
through untouched for advanced cases.
"""
from typing import Dict, Any, Optional, List

# concept -> ACS dataset + variable. Datasets chosen for 1-year ACS coverage.
_CONCEPT_VARIABLES: Dict[str, Dict[str, str]] = {
    "population":              {"dataset": "acs/acs1/profile", "get": "NAME,DP05_0001E",  "var": "DP05_0001E",  "label": "Total population"},
    "median_household_income": {"dataset": "acs/acs1",         "get": "NAME,B19013_001E", "var": "B19013_001E", "label": "Median household income"},
    "per_capita_income":       {"dataset": "acs/acs1",         "get": "NAME,B19301_001E", "var": "B19301_001E", "label": "Per capita income"},
    "poverty_rate":            {"dataset": "acs/acs1/profile", "get": "NAME,DP03_0128PE", "var": "DP03_0128PE", "label": "Poverty rate (%)"},
    "median_age":              {"dataset": "acs/acs1",         "get": "NAME,B01002_001E", "var": "B01002_001E", "label": "Median age"},
    "median_home_value":       {"dataset": "acs/acs1/profile", "get": "NAME,DP04_0089E",  "var": "DP04_0089E",  "label": "Median home value"},
    "unemployment_rate":       {"dataset": "acs/acs1/profile", "get": "NAME,DP03_0009PE", "var": "DP03_0009PE", "label": "Unemployment rate (%)"},
    "bachelors_or_higher":     {"dataset": "acs/acs1/profile", "get": "NAME,DP02_0068PE", "var": "DP02_0068PE", "label": "Bachelor's degree or higher (%)"},
    "uninsured_rate":          {"dataset": "acs/acs1/profile", "get": "NAME,DP03_0099PE", "var": "DP03_0099PE", "label": "Uninsured rate (%)"},
    "households":              {"dataset": "acs/acs1",         "get": "NAME,B11001_001E", "var": "B11001_001E", "label": "Total households"},
}

_STATE_FIPS: Dict[str, str] = {
    "alabama": "01", "alaska": "02", "arizona": "04", "arkansas": "05", "california": "06",
    "colorado": "08", "connecticut": "09", "delaware": "10", "district of columbia": "11",
    "washington dc": "11", "washington d.c.": "11", "florida": "12", "georgia": "13",
    "hawaii": "15", "idaho": "16", "illinois": "17", "indiana": "18", "iowa": "19", "kansas": "20",
    "kentucky": "21", "louisiana": "22", "maine": "23", "maryland": "24", "massachusetts": "25",
    "michigan": "26", "minnesota": "27", "mississippi": "28", "missouri": "29", "montana": "30",
    "nebraska": "31", "nevada": "32", "new hampshire": "33", "new jersey": "34", "new mexico": "35",
    "new york": "36", "north carolina": "37", "north dakota": "38", "ohio": "39", "oklahoma": "40",
    "oregon": "41", "pennsylvania": "42", "rhode island": "44", "south carolina": "45",
    "south dakota": "46", "tennessee": "47", "texas": "48", "utah": "49", "vermont": "50",
    "virginia": "51", "washington": "53", "west virginia": "54", "wisconsin": "55", "wyoming": "56",
}

_ALIASES: Dict[str, str] = {
    "pop": "population",
    "total population": "population",
    "income": "median_household_income",
    "median income": "median_household_income",
    "household income": "median_household_income",
    "poverty": "poverty_rate",
    "age": "median_age",
    "home value": "median_home_value",
    "house value": "median_home_value",
    "college": "bachelors_or_higher",
    "educational attainment": "bachelors_or_higher",
    "uninsured": "uninsured_rate",
    "unemployment": "unemployment_rate",
}


def _resolve_geography(geography: str) -> Optional[str]:
    g = (geography or "").strip().lower()
    if not g or g in ("us", "usa", "united states", "national", "nation", "america"):
        return "us:1"
    fips = _STATE_FIPS.get(g)
    if fips:
        return f"state:{fips}"
    # substring fallback (e.g. "the state of Texas")
    for name, code in sorted(_STATE_FIPS.items(), key=lambda kv: -len(kv[0])):
        if name in g:
            return f"state:{code}"
    return None


def resolve_census(concept: str, geography: str, year: Any) -> Optional[Dict[str, Any]]:
    """Return exact ACS query params, or None if concept/geo/year can't be mapped."""
    key = (concept or "").strip().lower().replace("-", "_")
    key = _ALIASES.get(key, key)
    spec = _CONCEPT_VARIABLES.get(key)
    if not spec:
        return None

    for_clause = _resolve_geography(geography)
    if not for_clause:
        return None

    yr = str(year).strip()
    if not yr.isdigit():
        return None

    return {
        "year": yr,
        "dataset": spec["dataset"],
        "get": spec["get"],
        "for": for_clause,
        "_concept_label": spec["label"],
    }


def supported_concepts() -> List[str]:
    return sorted(_CONCEPT_VARIABLES.keys())
