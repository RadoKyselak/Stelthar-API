from typing import Dict, List, Any
from urllib.parse import urlparse, quote_plus
import html
import re
import httpx
from config import logger
from config.constants import API_TIMEOUTS, RATE_LIMITS_PER_SECOND
from utils.retry import async_retry
from utils.rate_limiter import get_rate_limiter

_live_web_limiter = get_rate_limiter("LIVE_WEB", RATE_LIMITS_PER_SECOND.DATA_GOV)
_LIVE_WEB_LIMIT = 7

# Lightweight trust gate for fallback web results.
_TRUSTED_DOMAIN_HINTS = (
    ".gov",
    ".edu",
    "reuters.com",
    "apnews.com",
    "worldbank.org",
    "imf.org",
    "oecd.org",
)


def _clean_text(value: str) -> str:
    cleaned = html.unescape(value or "")
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_trusted_url(url: str) -> bool:
    if not url:
        return False
    host = (urlparse(url).netloc or "").lower()
    return any(hint in host for hint in _TRUSTED_DOMAIN_HINTS)


def _keyword_overlap_score(query: str, title: str, snippet: str) -> int:
    query_tokens = {t for t in re.findall(r"[a-zA-Z0-9]+", query.lower()) if len(t) > 2}
    haystack_tokens = set(re.findall(r"[a-zA-Z0-9]+", f"{title} {snippet}".lower()))
    return len(query_tokens & haystack_tokens)




async def _fallback_trusted_search(keyword_query: str, timeout: float) -> List[Dict[str, str]]:
    """Fallback web search when LIVE_WEB_SEARCH_URL is not configured.
    Uses DuckDuckGo HTML endpoint and keeps only trusted-domain links.
    """
    encoded = quote_plus(keyword_query)
    url = f"https://duckduckgo.com/html/?q={encoded}"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            html_doc = response.text

        matches = re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html_doc, flags=re.IGNORECASE)
        ranked = []
        for href, raw_title in matches:
            title = _clean_text(raw_title)
            if not href.startswith("http"):
                continue
            if not _is_trusted_url(href):
                continue
            score = _keyword_overlap_score(keyword_query, title, "") + 3
            ranked.append((score, {"title": f"Live Web: {title}", "url": href, "snippet": "Trusted web result"}))

        ranked.sort(key=lambda x: x[0], reverse=True)
        seen = set()
        out = []
        for _, item in ranked:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            out.append(item)
            if len(out) >= 5:
                break
        return out
    except Exception as e:
        logger.error("Fallback trusted search failed: %s", str(e))
        return []

def _extract_results(data: Any) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("results"), list):
        return data["results"]
    if isinstance(data.get("items"), list):
        return data["items"]
    if isinstance(data.get("data"), dict) and isinstance(data["data"].get("results"), list):
        return data["data"]["results"]
    return []


@async_retry(max_attempts=3, exceptions=(httpx.HTTPError, httpx.TimeoutException))
async def query_live_web(keyword_query: str, base_url: str | None = None) -> List[Dict[str, str]]:
    if not keyword_query or not keyword_query.strip():
        return []

    service_url = (base_url or "").strip()
    if not service_url:
        fallback = await _fallback_trusted_search(keyword_query, API_TIMEOUTS.DATA_GOV)
        if fallback:
            return fallback
        return [{"error": "LIVE_WEB search unavailable (no endpoint configured and fallback failed)", "source": "LIVE_WEB", "status": "failed"}]

    await _live_web_limiter.acquire()

    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUTS.DATA_GOV) as client:
            response = await client.get(service_url, params={"q": keyword_query, "limit": _LIVE_WEB_LIMIT})
            response.raise_for_status()
            data = response.json()
            entries = _extract_results(data)

            ranked = []
            for entry in entries:
                title = _clean_text(str(entry.get("title") or entry.get("name") or "N/A"))
                url = (entry.get("url") or entry.get("link") or "").strip()
                snippet = _clean_text(str(entry.get("snippet") or entry.get("description") or ""))

                if not url:
                    continue
                trust_bonus = 3 if _is_trusted_url(url) else 0
                score = _keyword_overlap_score(keyword_query, title, snippet) + trust_bonus
                ranked.append((score, {
                    "title": f"Live Web: {title}",
                    "url": url,
                    "snippet": snippet[:300],
                }))

            ranked.sort(key=lambda x: x[0], reverse=True)
            seen = set()
            out = []
            for _, item in ranked:
                if item["url"] in seen:
                    continue
                seen.add(item["url"])
                out.append(item)
                if len(out) >= 5:
                    break

            return out

    except httpx.HTTPStatusError as e:
        logger.error("Live Web API HTTP error %s: %s", e.response.status_code, e.response.text)
        return [{"error": f"Live Web API error: {e.response.status_code}", "source": "LIVE_WEB", "status": "failed"}]
    except httpx.RequestError as e:
        logger.error("Live Web API request error: %s", str(e))
        return [{"error": str(e), "source": "LIVE_WEB", "status": "failed"}]
    except Exception as e:
        logger.exception("Unexpected error during Live Web query")
        return [{"error": f"Unexpected error processing Live Web data: {str(e)}", "source": "LIVE_WEB", "status": "failed"}]
