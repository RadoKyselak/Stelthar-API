"""CDC / NCHS vital statistics read from data.cdc.gov (Socrata SODA API, no
key needed).

  w9j2-ggv5  NCHS - Death rates and life expectancy at birth (1900-2018):
             year, race ("All Races"), sex ("Both Sexes" | "Male" | "Female"),
             average_life_expectancy
  a5a8-jsrq, ncvk-7amm, ss2j-8ajj, it4f-frdc
             U.S. State Life Expectancy by Sex, 2018 / 2019 / 2020 / 2021:
             state (2021: area), sex ("Total" | "Male" | "Female"),
             leb (2020: le). 2018-2020 carry a "United States" row; 2021 does not.
  xkb8-kh2a  VSRR Provisional Drug Overdose Death Counts: state ("US", "TX"),
             year, month, period ("12 month-ending"), indicator, data_value
             (reported), predicted_value (adjusted for pending investigations)
  bi63-dtpu  NCHS - Leading Causes of Death: United States (1999-2017, final):
             year, cause_name, state, deaths
  9dzk-mvmi  Monthly Provisional Counts of Deaths by Select Causes, 2020-2023:
             year, month, jurisdiction_of_occurrence, one column per cause

  r8kw-7aab  Provisional COVID-19 Death Counts by Week Ending Date and State:
             group ("By Year"), year, state ("United States", "Texas"),
             covid_19_deaths (deaths with COVID-19 on the death certificate)
  t6u2-f84c  Mapping Injury, Overdose, and Violence - National: intent
             ("FA_Deaths", "FA_Homicide", "FA_Suicide", "All_Homicide",
             "All_Suicide"), type ("year"), period ("2022-01-01T00:00:00.000"),
             count
  fpsi-y8tj  the same by state: name ("Texas"), period ("2022"), count_sup

Answers are cached for a week; NCHS revises provisional counts monthly.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from config import logger
from evidence.cache import cached
from evidence.sources import SourceUnavailable

_WEEK = 7 * 24 * 3600
_BASE = "https://data.cdc.gov/resource"


def page(dataset: str) -> str:
    """The dataset's page on data.cdc.gov, for a reader."""
    return f"https://data.cdc.gov/d/{dataset}"


async def _get(dataset: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"{_BASE}/{dataset}.json", params=params)
    except httpx.HTTPError as e:
        raise SourceUnavailable("CDC", f"request failed: {e}") from e
    if r.status_code == 404:
        return []
    if r.status_code in (403, 429):
        raise SourceUnavailable("CDC", "data.cdc.gov rate limit reached")
    if r.status_code >= 400:
        raise SourceUnavailable("CDC", f"HTTP {r.status_code}")
    try:
        d = r.json()
    except ValueError:
        return []
    return d if isinstance(d, list) else []


def _q(v: str) -> str:
    """A SoQL string literal."""
    return "'" + str(v).replace("'", "''") + "'"


def _num(v) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


async def _rows(namespace: str, dataset: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
    async def load():
        return await _get(dataset, params)
    key = dataset + "|" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    try:
        return await cached(namespace, key, _WEEK, load) or []
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001 — an odd payload is "not found", not a crash
        logger.exception("data.cdc.gov %s failed", dataset)
        return []


# ------------------------------------------------------------------ life expectancy

LE_HISTORIC = "w9j2-ggv5"
# year: (dataset, place column, value column)
LE_BY_STATE = {
    2018: ("a5a8-jsrq", "state", "leb"),
    2019: ("ncvk-7amm", "state", "leb"),
    2020: ("ss2j-8ajj", "state", "le"),
    2021: ("it4f-frdc", "area", "leb"),
}


async def life_expectancy(year: int, sex: str = "Total", place: str = "United States"
                          ) -> Optional[Dict[str, Any]]:
    """{value, year, sex, place, dataset} for life expectancy at birth.
    sex is "Total", "Male" or "Female"; place "United States" or a state name."""
    if place == "United States" and year <= 2018 and year >= 1900:
        want = {"Total": "Both Sexes"}.get(sex, sex)
        rows = await _rows("cdc_le", LE_HISTORIC, {
            "$where": f"year={_q(year)} AND race={_q('All Races')} AND sex={_q(want)}"})
        v = _num(rows[0].get("average_life_expectancy")) if rows else None
        if v is not None:
            return {"value": v, "shown": rows[0]["average_life_expectancy"], "year": year, "sex": sex,
                    "place": place, "dataset": LE_HISTORIC}
        return None
    spec = LE_BY_STATE.get(year)
    if spec is None:
        return None
    dataset, col, val = spec
    rows = await _rows("cdc_le", dataset, {"$where": f"{col}={_q(place)} AND sex={_q(sex)}"})
    v = _num(rows[0].get(val)) if rows else None
    if v is None:
        return None
    return {"value": v, "shown": rows[0][val], "year": year, "sex": sex, "place": place, "dataset": dataset}


# ------------------------------------------------------------------ drug overdoses

OVERDOSE = "xkb8-kh2a"
ALL_DRUGS = "Number of Drug Overdose Deaths"


async def overdose_deaths(year: int, indicator: str = ALL_DRUGS, state: str = "US"
                          ) -> Optional[Dict[str, Any]]:
    """{reported, predicted, year, state, state_name, indicator, percent_complete,
    footnote} for the 12 months ending December of `year`. None when that
    December is not published yet."""
    rows = await _rows("cdc_overdose", OVERDOSE, {
        "$where": (f"state={_q(state)} AND year={_q(year)} AND month={_q('December')} "
                   f"AND period={_q('12 month-ending')} AND indicator={_q(indicator)}")})
    if not rows:
        return None
    r = rows[0]
    reported, predicted = _num(r.get("data_value")), _num(r.get("predicted_value"))
    if reported is None and predicted is None:
        return None
    return {"reported": reported, "predicted": predicted, "year": year, "state": state,
            "state_name": r.get("state_name") or state, "indicator": indicator,
            "percent_complete": _num(r.get("percent_complete")), "footnote": r.get("footnote") or ""}


# ------------------------------------------------------------------ causes of death

LEADING_FINAL = "bi63-dtpu"
MONTHLY_PROVISIONAL = "9dzk-mvmi"
# cause key: (bi63-dtpu cause_name, 9dzk-mvmi column)
CAUSES = {
    "all": ("All causes", "all_cause"),
    "heart": ("Heart disease", "diseases_of_heart"),
    "cancer": ("Cancer", "malignant_neoplasms"),
    "stroke": ("Stroke", "cerebrovascular_diseases"),
    "alzheimers": ("Alzheimer's disease", "alzheimer_disease"),
    "diabetes": ("Diabetes", "diabetes_mellitus"),
    "suicide": ("Suicide", "intentional_self_harm_suicide"),
    "accidents": ("Unintentional injuries", "accidents_unintentional"),
    "clrd": ("CLRD", "chronic_lower_respiratory"),
    "flu_pneumonia": ("Influenza and pneumonia", "influenza_and_pneumonia"),
    "kidney": ("Kidney disease", "nephritis_nephrotic_syndrome"),
}


async def cause_deaths(year: int, cause: str) -> Optional[Dict[str, Any]]:
    """{deaths, year, cause, final, dataset} for the United States: final
    counts for 1999-2017, provisional monthly counts summed for full years
    from 2020. None for other years."""
    name, column = CAUSES[cause]
    if 1999 <= year <= 2017:
        rows = await _rows("cdc_causes", LEADING_FINAL, {
            "$where": f"year={_q(year)} AND state={_q('United States')} AND cause_name={_q(name)}"})
        v = _num(rows[0].get("deaths")) if rows else None
        if v is None:
            return None
        return {"deaths": v, "year": year, "cause": cause, "final": True, "dataset": LEADING_FINAL}
    if year >= 2020:
        rows = await _rows("cdc_causes", MONTHLY_PROVISIONAL, {
            "$select": f"sum({column}) AS deaths, count(*) AS months",
            "$where": f"year={_q(year)} AND jurisdiction_of_occurrence={_q('United States')}"})
        if not rows:
            return None
        v, months = _num(rows[0].get("deaths")), _num(rows[0].get("months"))
        if v is None or months != 12:
            return None  # a year still being published
        return {"deaths": v, "year": year, "cause": cause, "final": False, "dataset": MONTHLY_PROVISIONAL}
    return None


def _year_is_over(year: int) -> bool:
    """A count for a year still running is not that year's count."""
    return year < datetime.now(timezone.utc).year


# ------------------------------------------------------------------ COVID-19

COVID = "r8kw-7aab"


async def covid_deaths(year: int, place: str = "United States") -> Optional[Dict[str, Any]]:
    """{involving, underlying?, year, dataset} for a finished year from 2020.

    CDC publishes two official counts. `involving`: COVID-19 anywhere on the
    death certificate (385,676 in 2020). `underlying`: COVID-19 as the cause
    that started the chain (351,829 in 2020), which is the figure in "leading
    causes of death". A claim may quote either, so both come back where the
    second is on data.cdc.gov (the United States, 2020-2022)."""
    if year < 2020 or not _year_is_over(year):
        return None
    rows = await _rows("cdc_covid", COVID, {
        "$where": f"`group`={_q('By Year')} AND state={_q(place)} AND year={_q(year)}"})
    involving = _num(rows[0].get("covid_19_deaths")) if rows else None
    if involving is None:
        return None
    out: Dict[str, Any] = {"involving": involving, "year": year, "dataset": COVID}
    if place == "United States":
        more = await _rows("cdc_causes", MONTHLY_PROVISIONAL, {
            "$select": "sum(covid_19_underlying_cause) AS deaths, count(*) AS months",
            "$where": f"year={_q(year)} AND jurisdiction_of_occurrence={_q('United States')}"})
        if more and _num(more[0].get("months")) == 12 and _num(more[0].get("deaths")):
            out["underlying"] = _num(more[0].get("deaths"))
    return out


# ------------------------------------------------------------------ firearms, homicide, suicide

INJURY_US = "t6u2-f84c"
INJURY_STATE = "fpsi-y8tj"
# what a claim counts: the dataset's name for it
INTENTS = {
    "firearm": "FA_Deaths",
    "firearm_homicide": "FA_Homicide",
    "firearm_suicide": "FA_Suicide",
    "homicide": "All_Homicide",
    "suicide": "All_Suicide",
}


async def injury_deaths(year: int, kind: str, state_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """{deaths, year, kind, dataset} from CDC's injury and violence counts
    (death certificates), for the United States or one state, for a finished
    year from 2019. None when the year or place is not published, or the
    state's count is suppressed for being small."""
    if year < 2019 or not _year_is_over(year):
        return None
    intent = INTENTS[kind]
    if state_name:
        rows = await _rows("cdc_injury", INJURY_STATE, {
            "$where": f"name={_q(state_name)} AND intent={_q(intent)} AND period={_q(year)}"})
        value = _num(rows[0].get("count_sup")) if rows else None
        dataset = INJURY_STATE
    else:
        rows = await _rows("cdc_injury", INJURY_US, {
            "$where": f"type={_q('year')} AND intent={_q(intent)} AND period={_q(f'{year}-01-01T00:00:00.000')}"})
        value = _num(rows[0].get("count")) if rows else None
        dataset = INJURY_US
    if value is None:
        return None
    return {"deaths": value, "year": year, "kind": kind, "dataset": dataset}
