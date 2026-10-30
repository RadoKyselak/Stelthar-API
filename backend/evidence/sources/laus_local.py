"""Unemployment for a city, county or metro area: BLS Local Area
Unemployment Statistics (LAUS), with no key and no model call.

Series ids are built from the codes Census gives the place:
  county  LAUCN + state+county FIPS + 00000000 + 03
  city    LAUCT + state+place FIPS  + 000000   + 03   (cities of 25,000+)
  metro   LAUMT + state FIPS + CBSA + 000000   + 03   (each of the metro's states is tried)
Local figures are not seasonally adjusted; the answer says so.
"""
from dataclasses import replace
from typing import List, Optional

from claims.geography import USPS, _STATE_FIPS
from evidence.datapoint import Datapoint, Geography
from evidence.sources import bls as bls_source

NOTE = "Local figures are not seasonally adjusted."


def series_ids(geo: Geography) -> List[str]:
    f = geo.fips or ""
    if geo.level == "county" and len(f) == 5:
        return [f"LAUCN{f}0000000003"]
    if geo.level == "place" and len(f) == 7:
        return [f"LAUCT{f}00000003"]
    if geo.level == "metro" and len(f) == 5:
        _, _, rest = geo.name.partition(", ")
        states = [USPS.get(a, "") for a in (rest.split() or [""])[0].split("-")]
        return [f"LAUMT{_STATE_FIPS[s]}{f}00000003" for s in states if s in _STATE_FIPS]
    return []


async def fetch(metric: str, label: str, family: str, geo: Geography, year: Optional[int],
                month: Optional[int], present: bool) -> List[Datapoint]:
    from datetime import datetime, timezone
    for sid in series_ids(geo):
        if present and month is None and year is None:
            got = await bls_source.fetch_latest_monthly(metric, label, sid, family)
        elif month is not None and year is not None:
            got = await bls_source.fetch_monthly(metric, label, sid, family, year, month)
        else:
            y = year or datetime.now(timezone.utc).year - 1
            got = await bls_source.fetch_annual(metric, label, sid, family, y)
            if not got and year is None:
                got = await bls_source.fetch_annual(metric, label, sid, family, y - 1)
        if got:
            out = []
            for dp in got:
                extra = dict(dp.extra or {})
                extra["caveat"] = " ".join(x for x in (extra.get("caveat"), NOTE) if x)
                out.append(replace(dp, geography=geo, extra=extra))
            return out
    return []
