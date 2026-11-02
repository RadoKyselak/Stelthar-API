"""HUD income limits and Area Median Income (AMI), from the HUD USER API.

Every year HUD publishes, for each metro area and non-metro county, the
median family income ("AMI") and the income limits housing programs use:
extremely low (30%), very low (50%) and low (80%) income, for households of
one to eight people. Claims about "80% of AMI for a family of four" are about
these tables, not Census median household income.

  /fmr/listMetroAreas           metro areas: cbsa_code, area_name
  /fmr/listCounties/{ST}        counties: fips_code (10 digits), county_name
  /il/data/{entity}?year=YYYY   one area's limits: median_income, very_low
                                {il50_p1..p8}, extremely_low {il30_..}, low {il80_..}

Needs HUD_API_TOKEN (free). Without it nothing here is called.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

import config
from config import logger
from evidence.cache import cached
from evidence.sources import SourceUnavailable

_WEEK = 7 * 24 * 3600


def available() -> bool:
    return bool(config.HUD_API_TOKEN)


async def _get(path: str, params: Optional[Dict[str, str]] = None) -> Any:
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{config.HUD_API_BASE}{path}", params=params or {},
                            headers={"Authorization": f"Bearer {config.HUD_API_TOKEN}"})
    except httpx.HTTPError as e:
        raise SourceUnavailable("HUD", f"request failed: {e}") from e
    if r.status_code in (401, 403):
        raise SourceUnavailable("HUD", "API token missing or invalid")
    if r.status_code in (400, 404):
        return None
    if r.status_code >= 400:
        raise SourceUnavailable("HUD", f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        return None


def _rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("data", payload)
    return [x for x in payload if isinstance(x, dict)] if isinstance(payload, list) else []


async def metro_areas() -> List[Dict[str, Any]]:
    async def load():
        return _rows(await _get("/fmr/listMetroAreas"))
    return await cached("hud_metros", "all", _WEEK, load) or []


async def counties(state_usps: str) -> List[Dict[str, Any]]:
    async def load():
        return _rows(await _get(f"/fmr/listCounties/{state_usps.upper()}"))
    return await cached("hud_counties", state_usps.upper(), _WEEK, load) or []


def _norm(s: str) -> str:
    s = s.lower().replace("saint ", "st ").replace("st. ", "st ").replace("ft. ", "fort ")
    return " ".join(re.sub(r"[^a-z ]", " ", s).split())


async def find_area(place: str, state_usps: str = "") -> Optional[Tuple[str, str]]:
    """(HUD entity id, HUD area name) for a county ("Travis County, TX") or a
    city that names its metro area ("Austin, TX" -> Austin-Round Rock-San
    Marcos, TX MSA). None when it cannot be placed."""
    name = (place or "").strip()
    if not name:
        return None
    if ", " in name:
        head, _, tail = name.rpartition(", ")
        from claims.geography import USPS, _STATE_FIPS
        t = tail.strip().replace(".", "")
        if t.upper() in USPS:
            state_usps, name = t.upper(), head
        elif t.lower() in _STATE_FIPS:
            state_usps = next(k for k, v in USPS.items() if v == t.lower())
            name = head
    want = _norm(re.sub(r"^(the\s+)?(city|town) of\s+", "", name, flags=re.I))
    if re.search(r"\b(county|parish|borough)\b", want) and state_usps:
        base = re.sub(r"\b(county|parish|borough)\b", "", want).strip()
        for c in await counties(state_usps):
            cn = _norm(str(c.get("county_name") or ""))
            if re.sub(r"\b(county|parish|borough)\b", "", cn).strip() == base and c.get("fips_code"):
                return str(c["fips_code"]), str(c.get("county_name"))
        return None
    hits = []
    for m in await metro_areas():
        area = str(m.get("area_name") or "")
        cities, _, rest = area.partition(", ")
        parts = [_norm(p) for p in re.split(r"[-/]", cities)]
        states = re.findall(r"\b[A-Z]{2}\b", rest)
        if want in parts and (not state_usps or state_usps in states):
            hits.append((parts.index(want), str(m.get("cbsa_code")), area))
    if not hits:
        return None
    hits.sort()  # the metro the city leads ("Austin-Round Rock") over one that lists it
    if len(hits) > 1 and hits[0][0] == hits[1][0]:
        return None  # two metros by that name and nothing to choose by
    return hits[0][1], hits[0][2]


async def limits(entity_id: str, year: int) -> Optional[Dict[str, Any]]:
    """{area_name, year, median_income, l30, l50, l80: {size: dollars}} or None."""
    async def load():
        d = await _get(f"/il/data/{entity_id}", {"year": str(year)})
        d = d.get("data") if isinstance(d, dict) and "data" in d else d
        if not isinstance(d, dict) or not d.get("median_income"):
            return None
        out = {"area_name": d.get("area_name") or d.get("metro_name") or d.get("county_name") or "",
               "year": int(str(d.get("year") or year)[:4]),
               "median_income": float(d["median_income"])}
        for key, tier, pct in (("l30", "extremely_low", 30), ("l50", "very_low", 50), ("l80", "low", 80)):
            row = d.get(tier) or {}
            out[key] = {str(n): float(row[f"il{pct}_p{n}"]) for n in range(1, 9)
                        if row.get(f"il{pct}_p{n}") not in (None, "")}
        return out
    try:
        return await cached("hud_il", f"{entity_id}:{year}", _WEEK, load)
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001 — an odd payload is "not found", not a crash
        logger.exception("HUD income limits parse failed for %s %s", entity_id, year)
        return None
