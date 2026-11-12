"""Congress.gov: the Library of Congress's record of every federal bill, read
from api.congress.gov (v3).

  bill/{congress}/{type}/{number}            title, sponsor, latest action, laws
  bill/{congress}/{type}/{number}/actions    every action, with roll-call votes
  bill/{congress}/{type}/{number}/titles     short and popular titles
  bill/{congress}/{type}/{number}/cosponsors

Types: hr, s, hjres, sjres, hconres, sconres, hres, sres. Bill numbers
restart each Congress, so a bill is always (congress, type, number).

Any api.data.gov key works: CONGRESS_API_KEY, else DATA_GOV_API_KEY (5,000
requests an hour). It is sent as a header, which keeps it out of URLs and
logs. Congress.gov has no title search; names are found elsewhere.
"""
from typing import Any, Dict, List, Optional

import httpx

import config
from config import logger
from evidence.cache import cached
from evidence.sources import SourceUnavailable

_HOUR = 3600
_TYPES = ("hr", "s", "hjres", "sjres", "hconres", "sconres", "hres", "sres")


def _key() -> str:
    return config.CONGRESS_API_KEY or config.DATA_GOV_API_KEY or "DEMO_KEY"


async def _get(path: str, **params) -> Optional[Dict[str, Any]]:
    """The JSON at `path`, or None when Congress.gov has no such record."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=5.0)) as c:
            r = await c.get(f"{config.CONGRESS_API_BASE}/{path}",
                            params={"format": "json", **params}, headers={"X-Api-Key": _key()})
    except httpx.HTTPError as e:
        raise SourceUnavailable("Congress.gov", f"request failed: {type(e).__name__} {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code == 429:
        raise SourceUnavailable("Congress.gov", "rate limit reached (set CONGRESS_API_KEY)")
    if r.status_code >= 400:
        raise SourceUnavailable("Congress.gov", f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        return None


async def _read(path: str, ttl: int, **params) -> Optional[Dict[str, Any]]:
    async def load():
        return await _get(path, **params)
    try:
        return await cached("congress", f"{path}|{sorted(params.items())}", ttl, load)
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001 — an odd payload is "not found", not a crash
        logger.exception("Congress.gov %s failed", path)
        return None


def _path(congress: int, kind: str, number: int) -> str:
    if kind not in _TYPES:
        raise ValueError(kind)
    return f"bill/{int(congress)}/{kind}/{int(number)}"


async def bill(congress: int, kind: str, number: int) -> Optional[Dict[str, Any]]:
    """The bill record, or None if that Congress has no such bill."""
    d = await _read(_path(congress, kind, number), 6 * _HOUR)
    return (d or {}).get("bill") or None


async def actions(congress: int, kind: str, number: int) -> List[Dict[str, Any]]:
    """Every action on the bill, newest first."""
    d = await _read(_path(congress, kind, number) + "/actions", 6 * _HOUR, limit=250)
    return list((d or {}).get("actions") or [])


async def titles(congress: int, kind: str, number: int) -> List[Dict[str, str]]:
    """Every title: {"titleType": "Popular Title" | "Short Title(s) as Enacted" | ..., "title": ...}."""
    d = await _read(_path(congress, kind, number) + "/titles", 7 * 24 * _HOUR, limit=250)
    return [{"titleType": t.get("titleType") or "", "title": t.get("title") or ""}
            for t in (d or {}).get("titles") or []]


async def cosponsors(congress: int, kind: str, number: int) -> List[Dict[str, Any]]:
    d = await _read(_path(congress, kind, number) + "/cosponsors", 6 * _HOUR, limit=250)
    return list((d or {}).get("cosponsors") or [])
