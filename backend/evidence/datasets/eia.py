"""The U.S. Energy Information Administration's series, found by search.

Fuel prices, spot prices and power generation are quoted constantly and FRED
carries only some of them: no state's gasoline price, no generation by fuel.
EIA publishes them all through one API (api.eia.gov/v2), which takes any
api.data.gov key.

Nothing here is a list of figures. The catalog is EIA's own: the series each
of a few routes offers, with EIA's descriptions, fetched from the API and
kept for a month. A search is word overlap against those descriptions, and a
value is read from EIA for the period asked:

  petroleum/pri/gnd   retail gasoline and diesel, U.S., regions, states, cities
  petroleum/pri/spt   spot prices: WTI, Brent, gasoline, heating oil, propane
  natural-gas/pri/fut Henry Hub spot and futures
  electricity         net generation by fuel, for the U.S. and each state

A series is addressed by the id EIA has used for a decade ("PET.RWTC",
"ELEC.GEN.NG-US-99"), which the API still answers under /v2/seriesid, with a
letter for how often it is published (D, W, M, A).
"""
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

import config
from config import logger
from evidence.cache import cached
from evidence.datapoint import ANNUAL, MONTHLY, POINT_IN_TIME, Datapoint, Geography, Period
from evidence.datasets import Candidate, tokens
from evidence.excerpt import make as make_excerpt, row as ex_row
from evidence.sources import SourceUnavailable
from evidence.units import USD, Quantity

_BASE = "https://api.eia.gov/v2"
_DAY = 24 * 3600
# Routes whose series EIA lists by id and description, and the prefix their
# ids carry.
_SERIES_ROUTES = (("petroleum/pri/gnd", "PET", "weekly, monthly, annual"),
                  ("petroleum/pri/spt", "PET", "daily, weekly, monthly, annual"),
                  ("natural-gas/pri/fut", "NG", "daily, weekly, monthly, annual"))
_GENERATION = "electricity/electric-power-operational-data"
_LETTER = {"daily": "D", "weekly": "W", "monthly": "M", "annual": "A"}


def _key() -> str:
    # EIA is on api.data.gov, whose shared demo key answers a few requests an
    # hour: enough for a first try, not for a service.
    return config.DATA_GOV_API_KEY or "DEMO_KEY"


def available() -> bool:
    return True


async def _get(path: str, params: Dict[str, str]) -> Any:
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"{_BASE}/{path}", params={**params, "api_key": _key()})
    except httpx.HTTPError as e:
        raise SourceUnavailable("EIA", f"request failed: {type(e).__name__}") from e
    if r.status_code in (400, 404):
        return None
    if r.status_code >= 400:
        raise SourceUnavailable("EIA", f"HTTP {r.status_code}")
    try:
        return (r.json() or {}).get("response")
    except ValueError:
        return None


async def _catalog() -> List[Dict[str, str]]:
    """Every series the routes above offer: id, EIA's description, how often."""
    async def load():
        out: List[Dict[str, str]] = []
        for route, prefix, freq in _SERIES_ROUTES:
            data = await _get(f"{route}/facet/series", {})
            for f in (data or {}).get("facets") or []:
                if f.get("id") and f.get("name"):
                    out.append({"id": f"{prefix}.{f['id']}", "title": str(f["name"]), "frequency": freq,
                                "route": route})
        fuels = (await _get(f"{_GENERATION}/facet/fueltypeid", {}) or {}).get("facets") or []
        places = (await _get(f"{_GENERATION}/facet/location", {}) or {}).get("facets") or []
        for fuel in fuels:
            for place in places:
                if fuel.get("id") and place.get("id") and fuel.get("name") and place.get("name"):
                    out.append({"id": f"ELEC.GEN.{fuel['id']}-{place['id']}-99",
                                "title": f"Net electricity generation, {fuel['name']}, {place['name']}, all sectors "
                                         f"(thousand megawatthours)",
                                "frequency": "monthly, annual", "route": _GENERATION})
        return out
    return await cached("eia_catalog", "v2", 30 * _DAY, load) or []


_UNITS = re.compile(r"\(([^()]*)\)\s*$")
# Words too common in EIA's descriptions to tell one series from another.
_COMMON = frozenset("price prices all the of in for per and u s us total average".split())


_NATION = re.compile(r"\bu\.?s\.?(?:a\.?)?\b|\bunited states\b|\bnational\b|\bnationwide\b", re.IGNORECASE)
# Words every description of a kind shares; they do not tell series apart.
_BOILERPLATE = tokens("net electricity generation all sectors thousand megawatthours total dollars per gallon "
                      "barrel million btu prices price formulations")


def _where(words: str):
    """(state name or "", whether the nation was asked for) in a search."""
    from claims.geography import resolve_geography
    g = resolve_geography(_NATION.sub(" ", words))
    state = g.name if g is not None and g.level == "state" else ""
    return state, (not state)


async def candidates(words: str, limit: int = 8) -> List[Candidate]:
    """EIA series whose own description matches a search, best first.

    The place decides before the words do: a state's series for a state, the
    U.S. series when the search names the country or no place at all. "Coal
    generation in the U.S." must not come back as refined coal in the Pacific
    region because that description happens to be shorter."""
    state, national = _where(words)
    asked = tokens(_NATION.sub(" ", words)) - _COMMON
    if state:
        asked -= tokens(state)
    if not asked:
        return []
    scored = []
    for s in await _catalog():
        title = s["title"]
        if state and state.lower() not in title.lower():
            continue
        if national and s["id"].startswith("ELEC.") and "U.S. Total" not in title:
            continue
        have = tokens(title)
        hit = len(asked & have)
        if not hit:
            continue
        extra = len(have - asked - _BOILERPLATE - (tokens(state) if state else set()))
        us = 1 if national and title.startswith("U.S.") else 0
        scored.append((hit / len(asked), us, -extra, s))
    scored.sort(key=lambda x: x[:3], reverse=True)
    out = []
    for share, _, _, s in scored[:limit]:
        if share < 0.5:
            break
        units = _UNITS.search(s["title"])
        out.append(Candidate(provider="eia", id=s["id"], title=s["title"],
                             units=units.group(1) if units else "", frequency=s["frequency"],
                             meta={"route": s["route"]}))
    return out


def _state(title: str) -> Geography:
    """The place an EIA description is for. "U.S." and regions are the
    nation's figure or nobody's state; a state's name makes it that state's."""
    from claims.geography import resolve_geography
    head = re.sub(r"\b(U\.S\.|East Coast|Midwest|Gulf Coast|Rocky Mountain|West Coast|New England|Central Atlantic|"
                  r"Lower Atlantic|PADD\s*\w+)\b", " ", title)
    g = resolve_geography(head)
    return g if g is not None and g.level == "state" else Geography.national()


async def fetch(c: Candidate, year: Optional[int], month: Optional[int],
                day: Optional[int] = None) -> Optional[Datapoint]:
    """One observation of an EIA series: the day's or the week's for a named
    day, the month's, the year's, or the newest."""
    offered = [f.strip() for f in c.frequency.split(",")]
    params: Dict[str, str] = {"sort[0][column]": "period", "sort[0][direction]": "desc", "length": "1"}
    if year and month and day:
        try:
            on = date(year, month, day)
        except ValueError:
            return None
        freq = "daily" if "daily" in offered else "weekly"
        if freq not in offered:
            return None
        # A weekly price is dated by its Monday: the week a day falls in is
        # the newest observation in the seven days up to it.
        start = on if freq == "daily" else on - timedelta(days=6)
        params.update(start=start.isoformat(), end=on.isoformat())
        kind, basis = POINT_IN_TIME, ("" if freq == "daily" else "weekly figure")
    elif year and month:
        freq = "monthly"
        params.update(start=f"{year}-{month:02d}", end=f"{year}-{month:02d}")
        kind, basis = MONTHLY, ""
    elif year:
        freq = "annual"
        params.update(start=str(year), end=str(year))
        kind, basis = ANNUAL, ""
    else:
        freq = offered[0]
        kind, basis = POINT_IN_TIME, "latest observation"
    if freq not in offered:
        return None
    sid = f"{c.id}.{_LETTER[freq]}"
    power = re.match(r"ELEC\.GEN\.([A-Z0-9]+)-([A-Z0-9]+)-99$", c.id)

    async def load():
        if power:
            # Generation is read from its own table, by fuel and place, for
            # all sectors. (The old series id answers too, with a figure
            # that leaves some generators out; the table is what EIA's
            # Electric Power Monthly prints.)
            data = await _get(f"{_GENERATION}/data/", {
                **params, "frequency": freq, "data[0]": "generation",
                "facets[fueltypeid][]": power.group(1), "facets[location][]": power.group(2),
                "facets[sectorid][]": "99"})
        else:
            # A price series, from the table it belongs to. (The old-style
            # address /seriesid ignores the dates asked for and answers with
            # the newest figure, which is a different week's price.)
            route = (c.meta or {}).get("route") or ""
            if not route:
                return []
            data = await _get(f"{route}/data/", {**params, "frequency": freq, "data[0]": "value",
                                                 "facets[series][]": c.id.split(".", 1)[1]})
        return (data or {}).get("data") or []

    key = sid + "|" + "|".join(f"{k}={v}" for k, v in sorted(params.items()))
    ttl = _DAY if not year or year >= datetime.now(timezone.utc).year - 1 else 30 * _DAY
    rows = await cached("eia_table", key, ttl, load) or []
    if not rows:
        return None
    row = rows[0]
    # The observation must be for the period asked. An answer for another
    # week is worse than none.
    got = str(row.get("period") or "")
    if year and not (params.get("start", "") <= got <= params.get("end", "9999")):
        return None
    raw = next((row[k] for k in ("value", "generation") if row.get(k) not in (None, "")), None)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    when = str(row.get("period") or "")
    units = str(row.get("units") or row.get("generation-units") or c.units)
    dollars = "$" in units or "dollar" in units.lower()
    if kind == POINT_IN_TIME:
        period = Period(year=int(when[:4]) if when[:4].isdigit() else None, kind=POINT_IN_TIME,
                        basis=basis, record_date=when or None)
    elif kind == MONTHLY:
        period = Period(year=year, kind=MONTHLY, month=month, basis=basis)
    else:
        period = Period(year=year, kind=ANNUAL, basis=basis)
    title = str(row.get("series-description") or c.title)
    if power and row.get("fuelTypeDescription") and row.get("stateDescription"):
        title = (f"Net electricity generation, {row['fuelTypeDescription']}, {row['stateDescription']}, "
                 f"all sectors")
    return Datapoint(
        quantity=Quantity(magnitude=value, family=USD if dollars else "unknown", raw=str(raw)),
        metric=f"eia:{c.id}", label=title, observed=period, geography=_state(title),
        source="EIA", source_url=f"https://www.eia.gov/opendata/browser/?frequency={freq}&seriesid={sid}",
        series_id=sid, as_of=when or None, raw_value=str(raw),
        extra={"units": units, "titled_place": title,
               "excerpt": make_excerpt([ex_row(when, str(raw), cited=True)], units=units)},
    )


def log_unavailable(e: Exception) -> None:
    logger.warning("EIA unavailable: %s", e)
