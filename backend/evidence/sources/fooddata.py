"""USDA FoodData Central: what is in a food, per 100 grams and per household
portion ("1 medium banana", "1 cup"), from api.nal.usda.gov.

  /foods/search    foods by name: fdcId, description, dataType
  /food/{fdcId}    one food: foodNutrients (amount per 100 g) and foodPortions
                   (gramWeight of "1 medium", "1 cup", ...)

Datasets used, most useful first: Survey (FNDDS), foods as eaten; SR Legacy,
the long-standing reference; Foundation. Branded products are not used.

Uses DATA_GOV_API_KEY (a free api.data.gov key, 1,000 requests an hour).
Without it, DEMO_KEY allows about 10 an hour. Food data rarely changes, so
answers are cached for 30 days.
"""
from typing import Any, Dict, List, Optional

import httpx

import config
from config import logger
from evidence.cache import cached
from evidence.sources import SourceUnavailable

_MONTH = 30 * 24 * 3600
_BASE = "https://api.nal.usda.gov/fdc/v1"
DATASETS = "Survey (FNDDS),SR Legacy,Foundation"


def _key() -> str:
    return config.DATA_GOV_API_KEY or "DEMO_KEY"


async def _get(path: str, params: Dict[str, str]) -> Any:
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{_BASE}{path}", params={**params, "api_key": _key()})
    except httpx.HTTPError as e:
        raise SourceUnavailable("USDA", f"request failed: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code in (403, 429) or "OVER_RATE_LIMIT" in r.text[:200]:
        raise SourceUnavailable("USDA", "FoodData Central rate limit reached (set DATA_GOV_API_KEY)")
    if r.status_code >= 400:
        raise SourceUnavailable("USDA", f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        return None


async def search(query: str) -> List[Dict[str, Any]]:
    """[{fdcId, description, dataType}] for a food name."""
    async def load():
        d = await _get("/foods/search", {"query": query, "dataType": DATASETS, "pageSize": "25"})
        foods = (d or {}).get("foods") or []
        return {"foods": [{"fdcId": f.get("fdcId"), "description": f.get("description") or "",
                           "dataType": f.get("dataType") or ""} for f in foods if f.get("fdcId")]}
    try:
        got = await cached("fdc_search", query.lower(), _MONTH, load)
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("FoodData Central search failed for %r", query)
        return []
    return (got or {}).get("foods") or []


async def food(fdc_id: int) -> Optional[Dict[str, Any]]:
    """{fdcId, description, dataType, nutrients: {id: (amount per 100 g, unit, name)},
    portions: [(text, grams)]}"""
    async def load():
        d = await _get(f"/food/{int(fdc_id)}", {})
        if not isinstance(d, dict):
            return None
        nutrients = {}
        for n in d.get("foodNutrients") or []:
            meta = n.get("nutrient") or {}
            if meta.get("id") is None or n.get("amount") is None:
                continue
            nutrients[str(meta["id"])] = [float(n["amount"]), (meta.get("unitName") or "").lower(),
                                          meta.get("name") or ""]
        portions = []
        for p in d.get("foodPortions") or []:
            grams = p.get("gramWeight")
            if not grams:
                continue
            unit = ((p.get("measureUnit") or {}).get("name") or "")
            unit = "" if unit.lower() in ("undetermined", "") else unit
            text = (p.get("portionDescription") or " ".join(
                str(x) for x in (p.get("amount") or "", unit, p.get("modifier") or "") if x)).strip()
            amount = float(p.get("amount") or 1) or 1.0
            if text and text.lower() != "quantity not specified":
                portions.append([text, float(grams), amount])
        return {"fdcId": d.get("fdcId"), "description": d.get("description") or "",
                "dataType": d.get("dataType") or "", "nutrients": nutrients, "portions": portions}
    try:
        return await cached("fdc_food", str(fdc_id), _MONTH, load)
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("FoodData Central food %s failed", fdc_id)
        return None
