"""FRED (Federal Reserve Bank of St. Louis) as a searchable dataset.

FRED republishes 800,000+ series from BLS, BEA, Census, the Federal Reserve,
Freddie Mac, the EIA and others, with the original source named. It covers
much of what the fixed metric list does not: mortgage rates, the federal
funds rate, housing starts, retail sales, subgroup unemployment rates,
industrial production, and so on.

Requires FRED_API_KEY (free). Without it this provider is off.
Aggregation (annual average, highest or lowest value in a year) is done by
FRED itself, so the figure is FRED's, not a computation of ours.
"""
import calendar
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

import config
from config import logger
from evidence.cache import cached
from evidence.datapoint import ANNUAL, MONTHLY, POINT_IN_TIME, Datapoint, Geography, Period
from evidence.datasets import Candidate, query_tokens
from evidence.excerpt import make as make_excerpt, row as ex_row
from evidence.sources import SourceUnavailable
from evidence.units import PERCENT, PERSONS, USD, Quantity

_BASE = "https://api.stlouisfed.org/fred"
_DAY = 24 * 3600
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def available() -> bool:
    return bool(config.FRED_API_KEY)


async def _get(path: str, params: Dict[str, str]) -> Any:
    q = {**params, "api_key": config.FRED_API_KEY or "", "file_type": "json"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{_BASE}/{path}", params=q)
    except httpx.HTTPError as e:
        raise SourceUnavailable("FRED", f"request failed: {e}") from e
    if r.status_code == 400:
        logger.warning("FRED rejected %s: %s", path, (r.text or "")[:200])
        return None
    if r.status_code >= 400:
        raise SourceUnavailable("FRED", f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        return None


async def candidates(claim: str, drop_words, limit: int = 12) -> List[Candidate]:
    words = sorted(query_tokens(claim, drop_words))
    if not words:
        return []
    text = " ".join(words)

    async def load():
        data = await _get("series/search", {"search_text": text, "order_by": "popularity",
                                            "sort_order": "desc", "limit": str(limit)})
        return (data or {}).get("seriess") or []

    rows = await cached("fred_search", text, 7 * _DAY, load) or []
    out = []
    for s in rows[:limit]:
        if s.get("observation_end", "") < "2000":
            continue  # discontinued long ago
        out.append(Candidate(
            provider="fred", id=str(s.get("id")), title=str(s.get("title") or ""),
            units=str(s.get("units") or ""), frequency=str(s.get("frequency") or "").lower(),
            detail=str(s.get("seasonal_adjustment_short") or ""),
            meta={"observation_end": str(s.get("observation_end") or "")},
        ))
    return out


async def _source_name(series_id: str) -> str:
    """The agency FRED got the series from, for the citation."""
    async def load():
        rel = await _get("series/release", {"series_id": series_id})
        rid = ((rel or {}).get("releases") or [{}])[0].get("id")
        if not rid:
            return {}
        src = await _get("release/sources", {"release_id": str(rid)})
        names = [s.get("name") for s in (src or {}).get("sources") or [] if s.get("name")]
        return {"name": "; ".join(names[:2])}
    data = await cached("fred_source", series_id, 30 * _DAY, load) or {}
    return data.get("name", "")


def _family(units: str):
    u = units.lower()
    if "percent" in u:
        return PERCENT, 1.0
    scale = 1.0
    if u.startswith("billions"):
        scale = 1e9
    elif u.startswith("millions"):
        scale = 1e6
    elif u.startswith("thousands"):
        scale = 1e3
    if "dollars" in u:
        return USD, scale
    if "persons" in u or "people" in u:
        return PERSONS, scale
    return "unknown", scale


_AGG = {"average": "avg", "value": "avg", "peak": "max", "low": "min"}
_STAT_TEXT = {"average": "annual average", "value": "annual average",
              "peak": "highest value in the year", "low": "lowest value in the year"}


async def fetch(c: Candidate, year: Optional[int], month: Optional[int], stat: str) -> Optional[Datapoint]:
    sid = c.id
    params: Dict[str, str] = {"series_id": sid}
    if year and month:
        last = calendar.monthrange(year, month)[1]
        params.update(observation_start=f"{year}-{month:02d}-01",
                      observation_end=f"{year}-{month:02d}-{last:02d}",
                      frequency="m", aggregation_method=_AGG.get(stat, "avg"))
        kind, basis = MONTHLY, ("monthly average" if c.frequency not in ("monthly",) else "")
    elif year:
        params.update(observation_start=f"{year}-01-01", observation_end=f"{year}-12-31")
        if c.frequency != "annual":
            params.update(frequency="a", aggregation_method=_AGG.get(stat, "avg"))
        kind, basis = ANNUAL, (_STAT_TEXT.get(stat, "") if c.frequency != "annual" else "")
    else:
        params.update(sort_order="desc", limit="1")
        kind, basis = POINT_IN_TIME, "latest observation"

    async def load():
        data = await _get("series/observations", params)
        return [o for o in (data or {}).get("observations") or [] if o.get("value") not in (".", "")]

    key = "|".join(f"{k}={v}" for k, v in sorted(params.items()))
    ttl = _DAY if not year or year >= datetime.now(timezone.utc).year - 1 else 30 * _DAY
    obs = await cached("fred_obs", key, ttl, load) or []
    if not obs:
        return None
    o = obs[0]
    try:
        value = float(o["value"])
    except (KeyError, ValueError):
        return None
    family, scale = _family(c.units)
    date = str(o.get("date") or "")
    if kind == POINT_IN_TIME:
        period = Period(year=int(date[:4]) if date[:4].isdigit() else None, kind=POINT_IN_TIME,
                        basis=basis, record_date=date or None)
    elif kind == MONTHLY:
        period = Period(year=year, kind=MONTHLY, month=month, basis=basis)
    else:
        period = Period(year=year, kind=ANNUAL, basis=basis)
    source = await _source_name(sid)
    return Datapoint(
        quantity=Quantity(magnitude=value * scale, family=family, raw=str(o["value"])),
        metric=f"fred:{sid}",
        label=c.title,
        observed=period,
        # Only national series are offered (see dataset_tier), so this is honest.
        geography=Geography.national(),
        source="FRED",
        source_url=f"https://fred.stlouisfed.org/series/{sid}",
        series_id=sid,
        as_of=date or None,
        raw_value=str(o["value"]),
        extra={"original_source": source, "units": c.units,
               # FRED's own observation row: its date and value, as FRED lists them.
               "excerpt": make_excerpt([ex_row(date, o["value"], cited=True)], units=c.units,
                                       note=(f"Aggregated by FRED: {basis} of the {c.frequency} series."
                                             if basis and kind != POINT_IN_TIME else ""))},
    )
