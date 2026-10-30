"""GDP for a state, county or metro area, from BEA's Regional accounts.

The national GDP series answer "GDP grew 2.5%"; this answers "Texas's
economy grew 3.6%" or "the New York metro's GDP was $2.3 trillion".

  SAGDP1  states            line 1 real GDP, line 3 current-dollar GDP
  CAGDP1  counties, metros  the same lines

Each row's description and unit multiplier are checked before the number is
used, so a renumbered table yields no answer rather than a wrong one.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from config import BEA_API_KEY, logger
from config.constants import API_TIMEOUTS
from evidence.cache import TTL_IMMUTABLE, TTL_VOLATILE, cached
from evidence.datapoint import ANNUAL, Datapoint, Geography, Period
from evidence.excerpt import make as make_excerpt, row as ex_row
from evidence.sources import SourceUnavailable
from evidence.sources.bea import _AUTH_ERROR_HINTS, _ENDPOINT, _bea_error, _limiter
from evidence.units import PERCENT, USD, Quantity, parse_number, scale_from_exponent
from utils.urls import public_url

_REAL, _CURRENT = "1", "3"
_EXPECT = {_REAL: "real gdp", _CURRENT: "current-dollar gdp"}
_PAGE = "https://apps.bea.gov/itable/?ReqID=70&step=1"


def available() -> bool:
    return bool(BEA_API_KEY)


def _geo_fips(geo: Geography) -> Optional[str]:
    if geo.level == "state" and geo.fips:
        return f"{geo.fips}000"
    if geo.level in ("county", "metro") and geo.fips and len(geo.fips) == 5:
        return geo.fips
    return None


async def _rows(table: str, line: str, geofips: str, years: List[int]) -> Dict[int, Dict[str, Any]]:
    params = {"UserID": BEA_API_KEY, "method": "GetData", "datasetname": "Regional",
              "TableName": table, "LineCode": line, "GeoFips": geofips,
              "Year": ",".join(str(y) for y in sorted(set(years))), "ResultFormat": "json"}

    async def load():
        await _limiter.acquire()
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUTS.BEA) as client:
                r = await client.get(_ENDPOINT, params=params)
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            raise SourceUnavailable("BEA", f"request failed: {e}") from e
        except ValueError as e:
            raise SourceUnavailable("BEA", "non-JSON response") from e
        err = _bea_error(payload)
        if err:
            if any(h in err.lower() for h in _AUTH_ERROR_HINTS):
                raise SourceUnavailable("BEA", err[:160])
            logger.info("BEA Regional %s %s: %s", table, geofips, err[:200])
            return None
        res = (payload.get("BEAAPI") or {}).get("Results") or {}
        data = res.get("Data") or []
        if not data:
            return None
        return {"rows": data, "desc": str(res.get("Statistic") or ""),
                "unit": str(res.get("UnitOfMeasure") or ""), "url": public_url(r.url)}

    ttl = TTL_VOLATILE * 24 if max(years) >= datetime.now(timezone.utc).year - 3 else TTL_IMMUTABLE
    blob = await cached("bea_regional", f"{table}:{line}:{geofips}:{params['Year']}", ttl, load)
    if not blob:
        return {}
    if _EXPECT[line] not in blob["desc"].lower().replace("current dollar", "current-dollar"):
        logger.warning("BEA Regional %s line %s is %r; not used", table, line, blob["desc"])
        return {}
    out = {}
    for row in blob["rows"]:
        try:
            y = int(str(row.get("TimePeriod"))[:4])
        except ValueError:
            continue
        v = parse_number(row.get("DataValue"))
        if v is None:
            continue
        out[y] = {"value": v * scale_from_exponent(row.get("UNIT_MULT")), "raw": row.get("DataValue"),
                  "name": row.get("GeoName") or "", "unit": blob["unit"], "url": blob["url"]}
    return out


async def fetch_gdp(metric: str, label: str, geo: Geography, year: int) -> List[Datapoint]:
    """GDP in current dollars (metric "gdp") or real growth from the year before
    (metric "real_gdp_growth") for a state, county or metro area."""
    geofips = _geo_fips(geo)
    if not geofips or not available():
        return []
    table = "SAGDP1" if geo.level == "state" else "CAGDP1"
    if metric == "gdp":
        rows = await _rows(table, _CURRENT, geofips, [year])
        r = rows.get(year)
        if not r:
            return []
        excerpt = make_excerpt([ex_row(f"{r['name'] or geo.name}, {year}", r["raw"], cited=True)],
                               units=r["unit"] or "current dollars")
        return [_point(metric, label, geo, r["value"], USD, year, str(r["raw"]), excerpt, table,
                       "current-dollar GDP", r["url"])]
    if metric == "real_gdp_growth":
        rows = await _rows(table, _REAL, geofips, [year - 1, year])
        a, b = rows.get(year), rows.get(year - 1)
        if not a or not b or not b["value"]:
            return []
        pct = round((a["value"] / b["value"] - 1) * 100, 1)
        unit = a["unit"] or "chained dollars"
        excerpt = make_excerpt([ex_row(f"Real GDP, {year - 1}", b["raw"]),
                                ex_row(f"Real GDP, {year}", a["raw"]),
                                ex_row("Change", f"{pct:+.1f}%", cited=True)], units=unit)
        return [_point(metric, label, geo, pct, PERCENT, year, f"{pct:.1f}", excerpt, table,
                       "change in real GDP from the year before", a["url"])]
    return []


def _point(metric, label, geo, value, family, year, raw, excerpt, table, basis, url):
    return Datapoint(
        quantity=Quantity(magnitude=value, family=family, raw=raw),
        metric=metric, label=label,
        observed=Period(year=year, kind=ANNUAL, basis=basis),
        geography=geo, source="BEA", source_url=url or _PAGE,
        series_id=f"{table} {geo.fips}", raw_value=raw,
        extra={"table": table, "excerpt": excerpt,
               "caveat": "BEA's regional accounts; revised each year."},
    )
