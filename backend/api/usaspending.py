"""USAspending.gov API client (keyless).

Docs: https://api.usaspending.gov/

Answers the single most common class of claim this system used to fall through
on: "Agency X's budget/spending was $Y in FY ZZZZ". Previously these dropped to
Data.gov catalog metadata (stale, no numbers). USAspending gives the actual
first-party obligation and budget-authority figures.

Planner emits a `usaspending` block like:
    {"metric": "agency_budget", "agency": "Department of Defense", "year": "2023"}
    {"metric": "total_spending", "year": "2023"}
"""
import re
from typing import Dict, Any, List, Optional
import httpx

from config import USASPENDING_BASE_URL, logger
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS
from utils.parsing import parse_numeric_value
from utils.urls import public_url
from utils.retry import async_retry
from utils.rate_limiter import get_quota_limiter

_usaspending_limiter = get_quota_limiter("USASPENDING", *RATE_LIMIT_QUOTAS.USASPENDING)

# Common shorthands the planner/LLM may emit -> canonical agency substrings.
_AGENCY_ALIASES = {
    "dod": "department of defense",
    "pentagon": "department of defense",
    "defense": "department of defense",
    "military": "department of defense",
    "hhs": "department of health and human services",
    "education": "department of education",
    "doe energy": "department of energy",
    "nasa": "national aeronautics and space administration",
    "va": "department of veterans affairs",
    "dhs": "department of homeland security",
    "epa": "environmental protection agency",
    "usda": "department of agriculture",
    "treasury": "department of the treasury",
    "state department": "department of state",
    "doj": "department of justice",
    "hud": "department of housing and urban development",
    "dot": "department of transportation",
    "interior": "department of the interior",
    "labor": "department of labor",
    "nsf": "national science foundation",
    "sba": "small business administration",
    "ssa": "social security administration",
}


def _humanize_usd(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    a = abs(value)
    if a >= 1e12:
        return f"${value / 1e12:.2f} trillion"
    if a >= 1e9:
        return f"${value / 1e9:.2f} billion"
    if a >= 1e6:
        return f"${value / 1e6:.2f} million"
    return f"${value:,.0f}"


def _normalize_agency_query(agency: str) -> str:
    a = (agency or "").strip().lower()
    return _AGENCY_ALIASES.get(a, a)


def _score_agency_match(query: str, name: str) -> int:
    q_tokens = {t for t in re.findall(r"[a-z]+", query.lower()) if len(t) > 2}
    n_tokens = set(re.findall(r"[a-z]+", name.lower()))
    return len(q_tokens & n_tokens)


@async_retry(max_attempts=3, exceptions=(httpx.HTTPError, httpx.TimeoutException))
async def query_usaspending(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    metric = str(params.get("metric", "agency_budget")).strip().lower()
    year = str(params.get("year", "")).strip()
    agency = _normalize_agency_query(str(params.get("agency", "")))

    if not year.isdigit():
        return [{"error": "USAspending query requires a fiscal year", "source": "USASPENDING", "status": "failed"}]

    await _usaspending_limiter.acquire()

    url = f"{USASPENDING_BASE_URL}/api/v2/references/toptier_agencies/"
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUTS.USASPENDING) as client:
            r = await client.get(url, params={"fiscal_year": year})
            r.raise_for_status()
            data = r.json()
            request_url = public_url(r.url)
    except httpx.HTTPStatusError as e:
        logger.error("USAspending HTTP error %s: %s", e.response.status_code, e.response.text[:200])
        return [{"error": f"USAspending API error: {e.response.status_code}", "source": "USASPENDING", "status": "failed"}]
    except httpx.RequestError as e:
        logger.error("USAspending request error: %s", str(e))
        return [{"error": str(e), "source": "USASPENDING", "status": "failed"}]
    except Exception as e:
        logger.exception("Unexpected error during USAspending query")
        return [{"error": f"Unexpected error processing USAspending data: {str(e)}", "source": "USASPENDING", "status": "failed"}]

    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        logger.info("USAspending returned no agencies for FY%s", year)
        return []

    def _budget(rec: Dict[str, Any]) -> Optional[float]:
        for f in ("budget_authority_amount", "current_total_budget_authority_amount", "obligated_amount"):
            v = parse_numeric_value(rec.get(f))
            if v is not None:
                return v
        return None

    fy_label = data.get("fiscal_year", year)

    if metric == "total_spending":
        total = sum(b for b in (_budget(rec) for rec in results) if b is not None)
        snippet = (
            f"Total federal budget authority across {len(results)} agencies in FY{fy_label}: "
            f"{_humanize_usd(total)}."
        )
        return [{
            "title": f"USAspending: total federal budget authority FY{fy_label}",
            "url": request_url,
            "snippet": snippet,
            "data_value": total,
            "raw_data_value": f"{total:.0f}",
            "unit": "USD",
            "unit_multiplier": 1,
            "line_description": "Total federal budget authority",
            "year": str(fy_label),
            "source": "USASPENDING",
        }]

    # agency_budget: rank agencies by name overlap with the requested agency.
    if not agency:
        return [{"error": "USAspending agency_budget query requires an agency name", "source": "USASPENDING", "status": "failed"}]

    ranked = sorted(
        ((_score_agency_match(agency, rec.get("agency_name", "")), rec) for rec in results),
        key=lambda x: x[0],
        reverse=True,
    )
    best_score, best = ranked[0]
    if best_score == 0:
        logger.info("USAspending: no agency name matched '%s' in FY%s", agency, year)
        return []

    value = _budget(best)
    name = best.get("agency_name", "Unknown agency")
    pct = best.get("percentage_of_total_budget_authority")
    obligated = parse_numeric_value(best.get("obligated_amount"))
    snippet = (
        f"{name} FY{fy_label} budget authority: {_humanize_usd(value)}"
        f"{f' ({pct} of federal total)' if pct else ''}"
        f"{f'; obligated {_humanize_usd(obligated)}' if obligated is not None else ''}."
    )
    return [{
        "title": f"USAspending: {name} FY{fy_label}",
        "url": request_url,
        "snippet": snippet,
        "data_value": value,
        "raw_data_value": f"{value:.0f}" if value is not None else "N/A",
        "unit": "USD",
        "unit_multiplier": 1,
        "line_description": f"{name} budget authority",
        "year": str(fy_label),
        "source": "USASPENDING",
    }]
