"""FBI Crime Data Explorer: offenses known to law enforcement, by year, for
the nation and each state, read from api.usa.gov/crime/fbi/cde.

  /summarized/national/{offense}?from=01-YYYY&to=12-YYYY
  /summarized/state/{ST}/{offense}?from=01-YYYY&to=12-YYYY

  offense  V violent crime, P property crime, HOM murder and nonnegligent
           manslaughter, ROB robbery, ASS aggravated assault, BUR burglary,
           MVT motor vehicle theft

The answer is monthly: offenses.actuals["<Place> Offenses"]["MM-YYYY"] (the
count agencies reported), offenses.rates["<Place> Offenses"] (per 100,000 of
the population those agencies cover), tooltips["Percent of Population
Coverage"][place] and populations. These are the FBI's reported figures, not
the national estimates in its annual "Crime in the Nation" release: the API
does not serve those. Coverage is kept so the check can say how complete a
year is (2021, the first NIBRS-only year, covers about three quarters of the
population).

Uses DATA_GOV_API_KEY (api.data.gov, 1,000 requests an hour), sent as a
header so it never reaches a URL or a log line. DEMO_KEY allows about 10 an
hour. Late reports keep arriving for past years, so answers are cached for a
week.
"""
from typing import Any, Dict, Optional

import httpx

import config
from config import logger
from evidence.cache import cached
from evidence.sources import SourceUnavailable

_WEEK = 7 * 24 * 3600
_BASE = "https://api.usa.gov/crime/fbi/cde"
PAGE = "https://cde.ucr.cjis.gov/LATEST/webapp/#/pages/explorer/crime/crime-trend"

OFFENSES = {"V", "P", "HOM", "ROB", "ASS", "BUR", "MVT"}

# Census FIPS code -> USPS abbreviation, the FBI's state key.
STATE_ABBR = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
    "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
    "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
    "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY",
}


def _key() -> str:
    return config.DATA_GOV_API_KEY or "DEMO_KEY"


async def _get(path: str, params: Dict[str, str]) -> Any:
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"{_BASE}{path}", params=params, headers={"X-Api-Key": _key()})
    except httpx.HTTPError as e:
        raise SourceUnavailable("FBI", f"request failed: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code in (403, 429) or "OVER_RATE_LIMIT" in r.text[:200]:
        raise SourceUnavailable("FBI", "Crime Data Explorer rate limit reached (set DATA_GOV_API_KEY)")
    if r.status_code >= 400:
        raise SourceUnavailable("FBI", f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        return None


def _series(block: Dict[str, Any], place: str) -> Dict[str, Any]:
    """The "<place> Offenses" monthly series of a rates/actuals block."""
    return (block or {}).get(f"{place} Offenses") or {}


def _year_values(series: Dict[str, Any], year: int):
    vals = [series.get(f"{m:02d}-{year}") for m in range(1, 13)]
    if any(v is None for v in vals):
        return None
    try:
        return [float(v) for v in vals]
    except (TypeError, ValueError):
        return None


def read_year(d: Any, year: int, place: str) -> Optional[Dict[str, Any]]:
    """One calendar year for a place from a /summarized answer, or None when
    the year is not complete (fewer than 12 months published)."""
    if not isinstance(d, dict):
        return None
    offenses = d.get("offenses") or {}
    counts = _year_values(_series(offenses.get("actuals"), place), year)
    rates = _year_values(_series(offenses.get("rates"), place), year)
    if counts is None or rates is None:
        return None
    cov = ((d.get("tooltips") or {}).get("Percent of Population Coverage") or {}).get(place) or {}
    cov_vals = [c for c in (_num(cov.get(f"{m:02d}-{year}")) for m in range(1, 13)) if c is not None]
    pops = ((d.get("populations") or {}).get("population") or {}).get(place) or {}
    pop_vals = [p for p in (_num(pops.get(f"{m:02d}-{year}")) for m in range(1, 13)) if p is not None]
    props = d.get("cde_properties") or {}
    return {
        "year": year, "place": place,
        "count": int(round(sum(counts))),
        "rate": round(sum(rates), 2),          # the sum of the FBI's 12 monthly rates
        "coverage": round(sum(cov_vals) / len(cov_vals), 1) if cov_vals else None,
        "population": int(round(sum(pop_vals) / len(pop_vals))) if pop_vals else None,
        "refreshed": ((props.get("last_refresh_date") or {}).get("UCR")) or "",
        "data_through": ((props.get("max_data_date") or {}).get("UCR")) or "",
    }


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


async def annual(offense: str, year: int, state: Optional[str] = None,
                 place: str = "United States") -> Optional[Dict[str, Any]]:
    """{year, place, count, rate, coverage, population, refreshed, data_through}
    for an offense in a calendar year, nationally or for a state (USPS
    abbreviation, with `place` its full name as the FBI writes it). None when
    the year is not fully published. Raises SourceUnavailable on an outage."""
    if offense not in OFFENSES:
        raise ValueError(offense)
    path = f"/summarized/state/{state}/{offense}" if state else f"/summarized/national/{offense}"
    params = {"from": f"01-{year}", "to": f"12-{year}"}

    async def load():
        return read_year(await _get(path, params), year, place)
    try:
        return await cached("fbi_cde", f"{state or 'US'}|{offense}|{year}", _WEEK, load)
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001 — an odd payload is "not found", not a crash
        logger.exception("FBI CDE %s %s %s failed", state or "US", offense, year)
        return None
