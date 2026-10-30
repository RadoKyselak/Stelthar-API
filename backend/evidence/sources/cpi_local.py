"""Inflation for the 23 metro areas BLS publishes a Consumer Price Index for.

"Inflation in the Chicago area was 4%" is checked against CPI-U for
Chicago-Naperville-Elgin, not the national figure. BLS prices whole metro
areas, so a city claim is answered with its metro area and the answer says so.
Many areas are published every other month; a month BLS did not publish
yields no answer.
"""
import re
from dataclasses import replace
from typing import List, Optional

from evidence.datapoint import Geography
from evidence.sources import bls as bls_source

AREAS = {
    "S12A": "New York-Newark-Jersey City, NY-NJ-PA",
    "S12B": "Philadelphia-Camden-Wilmington, PA-NJ-DE-MD",
    "S11A": "Boston-Cambridge-Newton, MA-NH",
    "S23A": "Chicago-Naperville-Elgin, IL-IN-WI",
    "S23B": "Detroit-Warren-Dearborn, MI",
    "S24A": "Minneapolis-St. Paul-Bloomington, MN-WI",
    "S24B": "St. Louis, MO-IL",
    "S35A": "Washington-Arlington-Alexandria, DC-VA-MD-WV",
    "S35B": "Miami-Fort Lauderdale-West Palm Beach, FL",
    "S35C": "Atlanta-Sandy Springs-Roswell, GA",
    "S35D": "Tampa-St. Petersburg-Clearwater, FL",
    "S35E": "Baltimore-Columbia-Towson, MD",
    "S37A": "Dallas-Fort Worth-Arlington, TX",
    "S37B": "Houston-The Woodlands-Sugar Land, TX",
    "S48A": "Phoenix-Mesa-Scottsdale, AZ",
    "S48B": "Denver-Aurora-Lakewood, CO",
    "S49A": "Los Angeles-Long Beach-Anaheim, CA",
    "S49B": "San Francisco-Oakland-Hayward, CA",
    "S49C": "Riverside-San Bernardino-Ontario, CA",
    "S49D": "Seattle-Tacoma-Bellevue, WA",
    "S49E": "San Diego-Carlsbad, CA",
    "S49F": "Urban Hawaii",
    "S49G": "Urban Alaska",
}
_EXTRA = {"new york city": "S12A", "nyc": "S12A", "manhattan": "S12A", "brooklyn": "S12A",
          "honolulu": "S49F", "hawaii": "S49F", "anchorage": "S49G", "alaska": "S49G",
          "st paul": "S24A", "saint paul": "S24A", "saint louis": "S24B", "dc": "S35A"}
_SERIES = {"inflation_rate": "SA0", "core_inflation_rate": "SA0L1E"}
NOTE = ("BLS publishes consumer prices for the whole metro area, not the city alone; "
        "local indexes are not seasonally adjusted.")


def _key(s: str) -> str:
    return " ".join(re.sub(r"[^a-z ]", " ", s.lower().replace("st.", "st")).split())


_BY_CITY = {}
for _code, _name in AREAS.items():
    for _part in _name.split(",")[0].split("-"):
        _BY_CITY.setdefault(_key(_part), _code)
_BY_CITY.update(_EXTRA)


def area_for(geo: Optional[Geography]) -> Optional[str]:
    """The BLS price area a city or metro claim is about, or None."""
    if geo is None or not geo.name:
        return None
    name = re.sub(r"\s+(city|metro.*|area|region|cdp)$", "", geo.name.split(",")[0].strip(), flags=re.I)
    for cand in (name, *name.split("-")):
        code = _BY_CITY.get(_key(cand))
        if code:
            return code
    return None


async def fetch(metric: str, label: str, geo: Geography, year: Optional[int],
                month: Optional[int], present: bool) -> List:
    code, item = area_for(geo), _SERIES.get(metric)
    if not code or not item:
        return []
    sid = f"CUUR{code}{item}"
    if year is None and month is None and present:
        latest = await bls_source.fetch_latest_monthly(metric, label, sid, "percent")
        if not latest:
            return []
        year, month = latest[0].observed.year, latest[0].observed.month
    if year is None:
        from datetime import datetime, timezone
        year = datetime.now(timezone.utc).year - 1
    got = await bls_source.fetch_inflation(metric, label, sid, year, month=month)
    where = Geography(name=f"{AREAS[code]} (BLS price area)", level="metro", fips=f"CPI{code}")
    out = []
    for dp in got:
        extra = dict(dp.extra or {})
        extra["caveat"] = " ".join(x for x in (extra.get("caveat"), NOTE) if x)
        out.append(replace(dp, geography=where, extra=extra))
    return out
