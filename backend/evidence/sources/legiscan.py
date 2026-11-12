"""LegiScan: bills in all 50 state legislatures (and Congress), read from
api.legiscan.com.

  getSessionList   a state's sessions, by year
  getMasterList    every bill in a session: number, title, status
  getBill          one bill: status, progress, history, sponsors, roll calls,
                   and `state_link`, the legislature's own page for it
  getSearch        bills by words in their title or text

LegiScan is not a government source: answers cite the legislature's own page
and credit LegiScan for the data, which it licenses as CC BY 4.0.

Needs LEGISCAN_API_KEY (free, 30,000 queries a month; cached calls count
against it too, so everything is cached here). Without a key `available()`
is False and state bills are left to search.
"""
from typing import Any, Dict, List, Optional

import httpx

import config
from config import logger
from evidence.cache import cached
from evidence.sources import SourceUnavailable

_HOUR = 3600

# Status codes LegiScan uses for a bill and its progress events.
INTRODUCED, ENGROSSED, ENROLLED, PASSED, VETOED, FAILED, OVERRIDE, CHAPTERED = 1, 2, 3, 4, 5, 6, 7, 8
ATTRIBUTION = "Bill data: LegiScan (CC BY 4.0)"


def available() -> bool:
    return bool(config.LEGISCAN_API_KEY)


async def _get(op: str, **params) -> Optional[Dict[str, Any]]:
    if not available():
        raise SourceUnavailable("LegiScan", "LEGISCAN_API_KEY is not set")
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            # LegiScan takes the key only as a query parameter; logs redact it.
            r = await c.get(config.LEGISCAN_BASE, params={"key": config.LEGISCAN_API_KEY, "op": op, **params})
    except httpx.HTTPError as e:
        raise SourceUnavailable("LegiScan", f"request failed: {e}") from e
    if r.status_code >= 400:
        raise SourceUnavailable("LegiScan", f"HTTP {r.status_code}")
    try:
        d = r.json()
    except ValueError:
        return None
    if (d or {}).get("status") != "OK":
        msg = ((d or {}).get("alert") or {}).get("message") or "error"
        if "not found" in msg.lower() or "unknown" in msg.lower():
            return None
        raise SourceUnavailable("LegiScan", msg)
    return d


async def _read(op: str, ttl: int, **params) -> Optional[Dict[str, Any]]:
    async def load():
        return await _get(op, **params)
    try:
        return await cached("legiscan", f"{op}|{sorted(params.items())}", ttl, load)
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001 — an odd payload is "not found", not a crash
        logger.exception("LegiScan %s failed", op)
        return None


def _rows(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    """LegiScan lists rows as an object keyed "0", "1", ... beside metadata."""
    return [v for k, v in (block or {}).items() if k.isdigit() and isinstance(v, dict)]


async def sessions(state: str) -> List[Dict[str, Any]]:
    d = await _read("getSessionList", 24 * _HOUR, state=state.upper())
    return list((d or {}).get("sessions") or [])


async def master_list(session_id: int) -> List[Dict[str, Any]]:
    d = await _read("getMasterList", 6 * _HOUR, id=int(session_id))
    return _rows((d or {}).get("masterlist") or {})


async def bill(bill_id: int) -> Optional[Dict[str, Any]]:
    d = await _read("getBill", 6 * _HOUR, id=int(bill_id))
    return (d or {}).get("bill") or None


async def search(state: str, query: str, year: int = 1) -> List[Dict[str, Any]]:
    """Bills matching `query`, best first. `year`: 1 all, 2 current session,
    3 recent, 4 prior, or an exact year."""
    d = await _read("getSearch", 24 * _HOUR, state=state.upper(), query=query, year=int(year))
    rows = _rows((d or {}).get("searchresult") or {})
    return sorted(rows, key=lambda r: -(r.get("relevance") or 0))
