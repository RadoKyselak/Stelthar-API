"""USAspending — agency budgets, from an endpoint that honours the fiscal year.

The previous adapter called `/api/v2/references/toptier_agencies/` with
`params={"fiscal_year": year}`. That endpoint takes no such parameter and
returns only the currently-active fiscal year. Verified against the live API:
`fiscal_year=2015`, `2020`, `2025` and no parameter at all return byte-identical
numbers, every record carrying `active_fy: 2026`. The adapter then did

    fy_label = data.get("fiscal_year", year)

and since no top-level `fiscal_year` key exists, the fallback fired every time
and stamped current-year dollars with whatever year the claim asked for. The
temporal guard compared the claim's year against that copied value, saw a match,
and stayed silent. A true claim about FY2015 was contradicted at full confidence
by FY2026 data.

`/api/v2/agency/{toptier_code}/budgetary_resources/` is genuinely FY-aware — it
returns ten years of distinct figures — so the period can be read from the
response rather than assumed.

A definitional note that matters for accuracy: `agency_budgetary_resources`
includes prior-year carryover and is substantially larger than the discretionary
appropriation people mean by "the defense budget" ($1.52T vs ~$816B for DoD in
FY2023). Both figures are returned with precise labels rather than one being
silently presented as "the budget", because collapsing them is how a comparison
comes out technically-supported for the wrong reason.
"""
import asyncio
from typing import Dict, List, Optional, Tuple

import httpx

from config import USASPENDING_BASE_URL, logger
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS
from evidence.datapoint import FISCAL_YEAR, Datapoint, Geography, Period
from evidence.units import USD, Quantity, parse_number
from utils.rate_limiter import get_quota_limiter
from utils.urls import public_url

_limiter = get_quota_limiter("USASPENDING", *RATE_LIMIT_QUOTAS.USASPENDING)

# toptier_code lookup is stable; cache it for the process lifetime.
_agency_cache: Optional[List[Dict[str, str]]] = None
_agency_lock = asyncio.Lock()

# Common shorthands people write instead of the official agency name.
_ALIASES = {
    "dod": "department of defense",
    "pentagon": "department of defense",
    "the military": "department of defense",
    "defense department": "department of defense",
    "nasa": "national aeronautics and space administration",
    "hhs": "department of health and human services",
    "dhs": "department of homeland security",
    "doe": "department of energy",
    "ed": "department of education",
    "va": "department of veterans affairs",
    "usda": "department of agriculture",
    "doj": "department of justice",
    "dot": "department of transportation",
    "hud": "department of housing and urban development",
    "epa": "environmental protection agency",
    "irs": "internal revenue service",
    "state department": "department of state",
    "treasury department": "department of the treasury",
    "cdc": "centers for disease control and prevention",
    "nih": "national institutes of health",
}


async def _load_agencies() -> List[Dict[str, str]]:
    global _agency_cache
    if _agency_cache is not None:
        return _agency_cache
    async with _agency_lock:
        if _agency_cache is not None:
            return _agency_cache
        await _limiter.acquire()
        url = f"{USASPENDING_BASE_URL}/api/v2/references/toptier_agencies/"
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUTS.USASPENDING) as client:
                r = await client.get(url)
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            logger.warning("USAspending agency list failed: %s", e)
            return []
        _agency_cache = [
            {
                "name": a.get("agency_name", ""),
                "abbreviation": a.get("abbreviation") or "",
                "toptier_code": a.get("toptier_code") or "",
            }
            for a in payload.get("results", [])
            if a.get("toptier_code")
        ]
        return _agency_cache


async def resolve_agency(query: str) -> Optional[Tuple[str, str]]:
    """Map an agency name or shorthand to (official_name, toptier_code).

    Exact and alias matches only, then a containment check. Returns None rather
    than guessing — an unresolved agency routes the claim to grounded search,
    which is far better than answering about the wrong agency.
    """
    if not query or not query.strip():
        return None
    q = " ".join(query.strip().lower().split())
    q = _ALIASES.get(q, q)

    agencies = await _load_agencies()
    if not agencies:
        return None

    for a in agencies:
        if a["name"].lower() == q:
            return a["name"], a["toptier_code"]
    for a in agencies:
        if a["abbreviation"] and a["abbreviation"].lower() == q:
            return a["name"], a["toptier_code"]
    # Containment, longest official name first so "Department of Energy" is not
    # shadowed by a shorter partial match.
    for a in sorted(agencies, key=lambda x: -len(x["name"])):
        n = a["name"].lower()
        if q in n or n in q:
            return a["name"], a["toptier_code"]
    return None


async def fetch_agency_budget(
    agency_query: str,
    fiscal_year: Optional[int] = None,
) -> List[Datapoint]:
    """Budgetary resources and obligations for an agency, by fiscal year.

    Returns one datapoint per measure, each carrying the fiscal year the API
    actually reported. When `fiscal_year` is None every available year is
    returned, which is what makes a trend claim answerable.
    """
    resolved = await resolve_agency(agency_query)
    if not resolved:
        logger.info("USAspending: could not resolve agency %r", agency_query)
        return []
    official_name, code = resolved

    await _limiter.acquire()
    url = f"{USASPENDING_BASE_URL}/api/v2/agency/{code}/budgetary_resources/"
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUTS.USASPENDING) as client:
            r = await client.get(url)
            r.raise_for_status()
            payload = r.json()
            request_url = public_url(r.url)
    except httpx.HTTPError as e:
        logger.warning("USAspending budgetary_resources failed for %s: %s", official_name, e)
        return []

    rows = payload.get("agency_data_by_year", []) or []
    out: List[Datapoint] = []

    for row in rows:
        row_fy = row.get("fiscal_year")
        if row_fy is None:
            continue
        try:
            row_fy = int(row_fy)
        except (TypeError, ValueError):
            continue
        # Filter on the year the RESPONSE reported, never on the requested one.
        if fiscal_year is not None and row_fy != fiscal_year:
            continue

        for field, metric, label in (
            ("agency_budgetary_resources", "agency_budgetary_resources",
             f"{official_name} — total budgetary resources (includes prior-year carryover)"),
            ("agency_total_obligated", "agency_obligations",
             f"{official_name} — total obligations"),
        ):
            value = parse_number(row.get(field))
            if value is None:
                continue
            out.append(Datapoint(
                quantity=Quantity(magnitude=value, family=USD, raw=str(row.get(field))),
                metric=metric,
                label=label,
                observed=Period(
                    year=row_fy, kind=FISCAL_YEAR,
                    basis="federal fiscal year (Oct 1 - Sep 30)",
                ),
                geography=Geography.national(),
                source="USASPENDING",
                source_url=request_url,
                series_id=f"agency:{code}",
                raw_value=str(row.get(field)),
                extra={"agency": official_name, "toptier_code": code},
            ))

    if fiscal_year is not None and not out:
        logger.info("USAspending has no FY%s row for %s", fiscal_year, official_name)
    return out
