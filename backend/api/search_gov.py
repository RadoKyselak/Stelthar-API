"""search.gov client — the research harness for anything the structured
government-data sources (BEA/BLS/Census/Treasury/USAspending) can't answer.

search.gov (search.usa.gov) is a free, public search API built specifically to
search across U.S. federal government sites — no domain-whitelisting logic
needed on our side, and no per-call cost. It's the right default for a
research tool scoped to official sources.

Requires a free API key: sign up at https://search.gov/, register a site
("affiliate"), and set SEARCH_GOV_AFFILIATE + SEARCH_GOV_API_KEY.

NOTE: implemented against the documented v2 REST shape
(https://search.gov/developer/) as of this writing. The exact endpoint/response
field names should be smoke-tested against a live key before relying on this
in production — search APIs occasionally revise field names between versions,
and this hasn't been exercised against the real service yet.
"""
from typing import Dict, Any, List, Optional
import re
import httpx

from config import SEARCH_GOV_AFFILIATE, SEARCH_GOV_API_KEY, logger
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS
from utils.content_fetch import fetch_and_extract
from utils.retry import async_retry
from utils.rate_limiter import get_quota_limiter

_search_gov_limiter = get_quota_limiter("SEARCH_GOV", *RATE_LIMIT_QUOTAS.SEARCH_GOV)

_SEARCH_GOV_ENDPOINT = "https://search.gov/api/v2/search"
_MAX_RESULTS = 5
_FETCH_TOP_N = 3  # only fetch full content for the top few hits — cost control


def _clean_text(value: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", cleaned).strip()


@async_retry(max_attempts=3, exceptions=(httpx.HTTPError, httpx.TimeoutException))
async def _search(keyword_query: str) -> List[Dict[str, Any]]:
    await _search_gov_limiter.acquire()

    params = {
        "affiliate": SEARCH_GOV_AFFILIATE,
        "access_key": SEARCH_GOV_API_KEY,
        "query": keyword_query,
        "limit": _MAX_RESULTS,
    }

    async with httpx.AsyncClient(timeout=API_TIMEOUTS.SEARCH_GOV) as client:
        r = await client.get(_SEARCH_GOV_ENDPOINT, params=params)
        r.raise_for_status()
        data = r.json()

    results = (data.get("web", {}) or {}).get("results", []) if isinstance(data, dict) else []
    out = []
    for item in results[:_MAX_RESULTS]:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        out.append({
            "title": _clean_text(item.get("title", "N/A")),
            "url": url,
            "snippet": _clean_text(item.get("snippet", "") or item.get("description", "")),
        })
    return out


async def query_search_gov(keyword_query: str) -> List[Dict[str, Any]]:
    """Search official government sites and return results with fetched excerpts.

    Snippets alone are usually too thin to answer a research question, so the
    top few results are fetched and their actual page/report text is attached
    as `content_excerpt`. This is the expensive part, so it's capped to
    `_FETCH_TOP_N` results — everything beyond that returns snippet-only.
    """
    if not keyword_query or not keyword_query.strip():
        return []

    if not SEARCH_GOV_AFFILIATE or not SEARCH_GOV_API_KEY:
        return [{
            "error": "SEARCH_GOV_AFFILIATE/SEARCH_GOV_API_KEY not configured",
            "source": "SEARCH_GOV",
            "status": "failed",
        }]

    try:
        results = await _search(keyword_query)
    except httpx.HTTPStatusError as e:
        logger.error("search.gov HTTP error %s: %s", e.response.status_code, e.response.text[:200])
        return [{"error": f"search.gov API error: {e.response.status_code}", "source": "SEARCH_GOV", "status": "failed"}]
    except httpx.RequestError as e:
        logger.error("search.gov request error: %s", str(e))
        return [{"error": str(e), "source": "SEARCH_GOV", "status": "failed"}]
    except Exception as e:
        logger.exception("Unexpected error during search.gov query")
        return [{"error": f"Unexpected error processing search.gov data: {str(e)}", "source": "SEARCH_GOV", "status": "failed"}]

    if not results:
        return []

    for item in results[:_FETCH_TOP_N]:
        excerpt = await fetch_and_extract(item["url"])
        if excerpt:
            item["content_excerpt"] = excerpt
        item["title"] = f"Gov Research: {item['title']}"

    return results
