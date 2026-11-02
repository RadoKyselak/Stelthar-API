"""World Bank open data: World Development Indicators for every country,
read from api.worldbank.org/v2 (no key, no account).

  /country?format=json&per_page=400
        every economy the World Bank covers: ISO3 id, ISO2 code, name,
        capital city, region ("Aggregates" for groups like "World" or
        "Euro area", which are skipped here)
  /country/{iso3}/indicator/{code}?format=json&per_page=100
        one indicator's annual series for one country, newest first:
        [{indicator: {id, value}, date: "2023", value: 3.071, decimal: 1}, ...]

Every answer is a two-element array, [meta, rows]: meta holds paging and
"lastupdated"; rows is null when nothing is published. An error is a
one-element array, [{"message": [{"id", "key", "value"}]}], served with
HTTP 200.

The country list changes a few times a decade, so it is cached for 30 days;
an indicator series for a day (the World Bank revises WDI a few times a year).
"""
import asyncio
from typing import Any, Dict, List, Optional

import httpx

from config import logger
from evidence.cache import cached
from evidence.sources import SourceUnavailable

_BASE = "https://api.worldbank.org/v2"
_DAY = 24 * 3600
_MONTH = 30 * _DAY
NAME = "World Bank"


async def _get(path: str, params: Dict[str, str]) -> Any:
    """GET {_BASE}{path} as JSON. Raises SourceUnavailable on an outage or a
    refusal; returns None for a body that is not JSON."""
    query = {"format": "json", **(params or {})}
    r = None
    waits = (1.0,)
    for attempt in range(len(waits) + 1):
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
                r = await c.get(f"{_BASE}{path}", params=query)
        except httpx.HTTPError as e:
            if attempt == len(waits):
                raise SourceUnavailable(NAME, f"request failed: {e}") from e
            await asyncio.sleep(waits[attempt])
            continue
        if r.status_code >= 500 and attempt < len(waits):
            await asyncio.sleep(waits[attempt])
            continue
        break
    if r.status_code == 429:
        raise SourceUnavailable(NAME, "rate limit reached")
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise SourceUnavailable(NAME, f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        return None


def _rows(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """The data half of a [meta, rows] answer; [] when nothing is published,
    None for an error message or a shape this does not know."""
    if not isinstance(payload, list) or not payload:
        return None
    if len(payload) == 1 and isinstance(payload[0], dict) and payload[0].get("message"):
        msg = payload[0]["message"]
        first = msg[0] if isinstance(msg, list) and msg else {}
        logger.info("World Bank API message: %s", first.get("value") or first)
        return None
    if len(payload) < 2:
        return None
    return payload[1] if isinstance(payload[1], list) else []


def _meta(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


async def countries() -> List[Dict[str, str]]:
    """[{iso3, iso2, name, capital, region}] for every economy, aggregates
    ("World", "Euro area", "High income", ...) left out."""
    async def load():
        d = await _get("/country", {"per_page": "400"})
        rows = _rows(d) or []
        out = []
        for c in rows:
            region = ((c.get("region") or {}).get("value") or "").strip()
            if not c.get("id") or region == "Aggregates":
                continue
            out.append({"iso3": c["id"], "iso2": c.get("iso2Code") or "",
                        "name": (c.get("name") or "").strip(),
                        "capital": (c.get("capitalCity") or "").strip(), "region": region})
        return out
    try:
        return await cached("worldbank_countries", "all", _MONTH, load) or []
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001 — an odd payload is "no list", not a crash
        logger.exception("World Bank country list failed")
        return []


async def series(iso3: str, code: str) -> Optional[Dict[str, Any]]:
    """{code, name, last_updated, country, observations: [{year, value, decimal}]}
    for one country and indicator, newest year first. Years the World Bank
    lists without a value are kept (value None), so a caller can tell "not
    published yet" from "not found". None when the API knows no such series."""
    iso3, code = iso3.upper(), code.upper()

    async def load():
        d = await _get(f"/country/{iso3}/indicator/{code}", {"per_page": "100"})
        rows = _rows(d)
        if not rows:
            return None
        obs = []
        name, country = "", ""
        for r in rows:
            try:
                year = int(str(r.get("date") or "")[:4])
            except ValueError:
                continue
            name = name or ((r.get("indicator") or {}).get("value") or "")
            country = country or ((r.get("country") or {}).get("value") or "")
            v = r.get("value")
            obs.append({"year": year, "value": float(v) if v is not None else None,
                        "decimal": int(r.get("decimal") or 0)})
        obs.sort(key=lambda o: -o["year"])
        return {"code": code, "name": name, "country": country,
                "last_updated": _meta(d).get("lastupdated") or "", "observations": obs}
    try:
        return await cached("worldbank_series", f"{iso3}|{code}", _DAY, load)
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("World Bank %s %s failed", iso3, code)
        return None


async def indicator(iso3: str, code: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
    """Observations of one indicator: [{year, value, decimal, indicator,
    last_updated, country}], newest first and values only. With `year`, that
    year's observation alone (or [] when it is not published)."""
    s = await series(iso3, code)
    if not s:
        return []
    out = [{**o, "indicator": s["name"], "last_updated": s["last_updated"], "country": s["country"]}
           for o in s["observations"] if o["value"] is not None]
    if year is not None:
        out = [o for o in out if o["year"] == int(year)]
    return out
