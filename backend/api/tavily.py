"""Tavily client — the research harness for anything the structured
government-data sources (BEA/BLS/Census/Treasury/USAspending) can't answer.

Replaced search.gov as the research backend: search.gov's affiliate
registration isn't self-service (no instant API key from a dashboard), which
was a real onboarding blocker. Tavily is purpose-built for exactly this
agent-search use case, has self-service signup with an immediate key, and a
generous free tier.

Unlike search.gov, Tavily searches the open web, not just federal sites — so
domain trust is scored here rather than guaranteed by the backend. Results on
.gov/.mil domains, or a short list of well-known nonpartisan/official sources,
rank above generic web results; nothing is hard-filtered out, since useful
material (e.g. CRS reports) is sometimes mirrored off government domains.

Requires a free API key: sign up at https://tavily.com/, and set
TAVILY_API_KEY.

NOTE: implemented against Tavily's documented REST shape as of this writing.
The exact endpoint/response field names should be smoke-tested against a live
key before relying on this in production — this hasn't been exercised against
the real service yet, and Tavily's API has evolved over time (in particular,
whether the API key belongs in the request body vs. an Authorization header).
"""
from typing import Dict, Any, List
from urllib.parse import urlparse
import re
import httpx

from config import TAVILY_API_KEY, logger
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS
from utils.content_fetch import fetch_and_extract
from utils.retry import async_retry
from utils.rate_limiter import get_quota_limiter

_tavily_limiter = get_quota_limiter("TAVILY", *RATE_LIMIT_QUOTAS.TAVILY)

_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_MAX_RESULTS = 5
_FETCH_TOP_N = 3  # fetch full content only for the top few — cost control

# Domains trusted enough to rank above generic web results. Not a hard filter —
# see module docstring.
_TRUSTED_DOMAIN_HINTS = (
    ".gov", ".mil", "sgp.fas.org",  # common CRS-report mirror
    "reuters.com", "apnews.com", "oecd.org", "imf.org", "worldbank.org",
)


def _is_trusted_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return any(hint in host for hint in _TRUSTED_DOMAIN_HINTS)


def _clean_text(value: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", cleaned).strip()


@async_retry(max_attempts=3, exceptions=(httpx.HTTPError, httpx.TimeoutException))
async def _search(keyword_query: str) -> List[Dict[str, Any]]:
    await _tavily_limiter.acquire()

    body = {
        "api_key": TAVILY_API_KEY,
        "query": keyword_query,
        "search_depth": "basic",
        "max_results": _MAX_RESULTS,
        "include_raw_content": True,  # Tavily fetches/extracts page text for us
    }

    async with httpx.AsyncClient(timeout=API_TIMEOUTS.TAVILY) as client:
        r = await client.post(_TAVILY_ENDPOINT, json=body)
        r.raise_for_status()
        data = r.json()

    raw_results = data.get("results", []) if isinstance(data, dict) else []
    ranked = []
    for item in raw_results:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        trust_bonus = 1 if _is_trusted_url(url) else 0
        score = float(item.get("score", 0.0)) + trust_bonus
        ranked.append((score, {
            "title": _clean_text(item.get("title", "N/A")),
            "url": url,
            "snippet": _clean_text(item.get("content", ""))[:300],
            "raw_content": item.get("raw_content"),
        }))

    ranked.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked[:_MAX_RESULTS]]


async def query_tavily(keyword_query: str) -> List[Dict[str, Any]]:
    """Search the web (gov/official sources ranked first) with fetched excerpts.

    Tavily's own `raw_content` is used when present — it already fetches and
    extracts page text server-side. If missing for a given result, falls back
    to this system's own content_fetch as a defense-in-depth second attempt.
    """
    if not keyword_query or not keyword_query.strip():
        return []

    if not TAVILY_API_KEY:
        return [{
            "error": "TAVILY_API_KEY not configured",
            "source": "TAVILY",
            "status": "failed",
        }]

    try:
        results = await _search(keyword_query)
    except httpx.HTTPStatusError as e:
        logger.error("Tavily HTTP error %s: %s", e.response.status_code, e.response.text[:200])
        return [{"error": f"Tavily API error: {e.response.status_code}", "source": "TAVILY", "status": "failed"}]
    except httpx.RequestError as e:
        logger.error("Tavily request error: %s", str(e))
        return [{"error": str(e), "source": "TAVILY", "status": "failed"}]
    except Exception as e:
        logger.exception("Unexpected error during Tavily query")
        return [{"error": f"Unexpected error processing Tavily data: {str(e)}", "source": "TAVILY", "status": "failed"}]

    if not results:
        return []

    for item in results[:_FETCH_TOP_N]:
        excerpt = item.pop("raw_content", None)
        if not excerpt:
            excerpt = await fetch_and_extract(item["url"])
        if excerpt:
            item["content_excerpt"] = excerpt[:4000]
        item["title"] = f"Web Research: {item['title']}"

    # Results beyond _FETCH_TOP_N still carry a raw_content field to drop.
    for item in results[_FETCH_TOP_N:]:
        item.pop("raw_content", None)
        item["title"] = f"Web Research: {item['title']}"

    return results
