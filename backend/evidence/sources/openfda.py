"""openFDA: the FDA's own records of what it approved, cleared, recalled and
lists as short, read from api.fda.gov.

  drug/drugsfda       Drugs@FDA: every NDA / ANDA (and some BLA) application,
                      its products, and each submission with its approval date
  drug/label          the prescribing label: indications, boxed warning, and
                      the application number (BLA biologics included)
  drug/shortages      the FDA drug shortage list, with status
  drug|food|device/enforcement   recall enforcement reports (2012 on)
  device/pma          premarket approvals (class III devices: "approved")
  device/registrationlisting     device brand names and their class

No key is needed (1,000 requests a day per address); OPENFDA_API_KEY raises
that to 120,000. Answers are cached for a day, "nothing found" included.
"""
from typing import Any, Dict, List, Optional

import httpx

import config
from config import logger
from evidence.cache import cached
from evidence.sources import SourceUnavailable

_DAY = 24 * 3600
_EMPTY = {"results": [], "total": 0}


def _quote(name: str) -> str:
    """A name as an openFDA phrase: quotes and query syntax removed."""
    return '"' + " ".join("".join(ch for ch in name if ch.isalnum() or ch in " -'.&/").split()) + '"'


async def _get(endpoint: str, search: str, limit: int = 25) -> Dict[str, Any]:
    params = {"search": search, "limit": str(limit)}
    if config.OPENFDA_API_KEY:
        params["api_key"] = config.OPENFDA_API_KEY
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{config.OPENFDA_BASE}/{endpoint}.json", params=params)
    except httpx.HTTPError as e:
        raise SourceUnavailable("FDA", f"request failed: {e}") from e
    if r.status_code == 404:
        return dict(_EMPTY)  # openFDA's "No matches found!"
    if r.status_code == 429:
        raise SourceUnavailable("FDA", "openFDA rate limit reached")
    if r.status_code >= 400:
        raise SourceUnavailable("FDA", f"HTTP {r.status_code}")
    try:
        d = r.json()
    except ValueError:
        return dict(_EMPTY)
    return {"results": d.get("results") or [],
            "total": ((d.get("meta") or {}).get("results") or {}).get("total", 0),
            "last_updated": (d.get("meta") or {}).get("last_updated")}


async def search(endpoint: str, query: str, limit: int = 25) -> Dict[str, Any]:
    """Cached; an empty answer is cached too (it is a real answer here)."""
    async def load():
        return await _get(endpoint, query, limit)
    try:
        return await cached("openfda", f"{endpoint}|{limit}|{query}", _DAY, load) or dict(_EMPTY)
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001 — an odd payload is "not found", not a crash
        logger.exception("openFDA %s failed for %s", endpoint, query[:80])
        return dict(_EMPTY)


# ------------------------------------------------------------------ drugs

async def applications(name: str) -> List[Dict[str, Any]]:
    """Drugs@FDA applications whose product carries this brand name or
    active ingredient."""
    q = _quote(name)
    d = await search("drug/drugsfda", f"products.brand_name:{q} OR products.active_ingredients.name:{q} "
                                      f"OR openfda.generic_name:{q}", 100)
    return d["results"]


async def labels(name: str, extra: str = "") -> List[Dict[str, Any]]:
    """Prescribing labels for a brand or generic name."""
    q = _quote(name)
    s = f"(openfda.brand_name:{q} OR openfda.generic_name:{q})"
    if extra:
        s += f" AND {extra}"
    return (await search("drug/label", s, 20))["results"]


async def shortages(generic: str) -> List[Dict[str, Any]]:
    q = _quote(generic)
    return (await search("drug/shortages", f"generic_name:{q} OR openfda.generic_name:{q}", 100))["results"]


async def recalls(name: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
    """Enforcement reports (food, drugs, devices) naming the product or firm."""
    q = _quote(name)
    s = f"(product_description:{q} OR recalling_firm:{q})"
    if year:
        s += f" AND recall_initiation_date:[{year}0101 TO {year}1231]"
    out: List[Dict[str, Any]] = []
    for kind in ("food", "drug", "device"):
        for r in (await search(f"{kind}/enforcement", s, 100))["results"]:
            out.append({**r, "_kind": kind})
    return out


# ------------------------------------------------------------------ devices

async def pma(name: str) -> List[Dict[str, Any]]:
    """Premarket approvals by trade name: original approvals, not supplements."""
    q = _quote(name)
    rows = (await search("device/pma", f"trade_name:{q}", 100))["results"]
    return [r for r in rows if not (r.get("supplement_number") or "").strip()] or rows


async def device_listings(name: str) -> List[Dict[str, Any]]:
    """Registered device brand names, with each product's device class."""
    q = _quote(name)
    return (await search("device/registrationlisting", f"proprietary_name:{q}", 25))["results"]
