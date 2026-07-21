"""Fetch and extract readable text from a URL (HTML or PDF).

Search APIs return a title and a thin snippet — often too little to actually
answer a research question. This module turns a search hit into real evidence
by fetching the page and extracting its main text, so the LLM synthesizing an
answer has an actual excerpt to read rather than a one-line teaser.

Cost control is the point of this module as much as extraction is:
  * an in-memory TTL cache so the same report isn't re-fetched every request
    (.gov reports and pages change rarely; a long TTL is safe and cheap)
  * a hard download-size cap so a large PDF can't blow up latency or memory
  * output is truncated before it ever reaches an LLM prompt
"""
import io
import re
import time
from typing import Optional, Dict
from urllib.parse import urlparse

import httpx

from config import logger

_FETCH_TIMEOUT_SECONDS = 12.0
_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024  # 8MB cap; government PDFs can be large
_MAX_EXTRACTED_CHARS = 4000
_CACHE_TTL_SECONDS = 24 * 3600  # reports/pages rarely change within a day
_CACHE_MAX_ENTRIES = 500

_cache: Dict[str, Dict] = {}


def _cache_get(url: str) -> Optional[str]:
    entry = _cache.get(url)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL_SECONDS:
        return entry["text"]
    return None


def _cache_set(url: str, text: Optional[str]) -> None:
    _cache[url] = {"ts": time.time(), "text": text}
    if len(_cache) > _CACHE_MAX_ENTRIES:
        oldest = sorted(_cache.items(), key=lambda kv: kv[1]["ts"])[: len(_cache) - _CACHE_MAX_ENTRIES]
        for k, _ in oldest:
            _cache.pop(k, None)


def _strip_html(html: str) -> str:
    # Drop script/style blocks entirely before stripping tags, or their
    # contents (often minified JS/CSS) pollute the extracted text.
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&lt;|&gt;|&#\d+;|&\w+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_pdf_text(data: bytes, max_chars: int) -> Optional[str]:
    try:
        import pypdf
    except ImportError:
        logger.warning("pypdf not installed; cannot extract PDF content. Add 'pypdf' to requirements.txt.")
        return None

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        parts = []
        total = 0
        for page in reader.pages:
            page_text = (page.extract_text() or "").strip()
            if page_text:
                parts.append(page_text)
                total += len(page_text)
            if total >= max_chars:
                break
        text = re.sub(r"\s+", " ", " ".join(parts)).strip()
        return text or None
    except Exception as e:
        logger.warning("PDF text extraction failed: %s", e)
        return None


async def fetch_and_extract(url: str, max_chars: int = _MAX_EXTRACTED_CHARS) -> Optional[str]:
    """Fetch a URL and return truncated, readable text, or None on failure.

    Never raises — a fetch failure should degrade to snippet-only evidence,
    not abort the whole research query.
    """
    if not url or not urlparse(url).scheme.startswith("http"):
        return None

    cached = _cache_get(url)
    if cached is not None:
        return cached[:max_chars]

    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()

                chunks = []
                total = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= _MAX_DOWNLOAD_BYTES:
                        logger.info("Truncating download at %d bytes for %s", total, url)
                        break
                data = b"".join(chunks)
    except httpx.HTTPStatusError as e:
        logger.info("Content fetch HTTP error %s for %s", e.response.status_code, url)
        _cache_set(url, None)
        return None
    except httpx.RequestError as e:
        logger.info("Content fetch request error for %s: %s", url, str(e))
        _cache_set(url, None)
        return None
    except Exception as e:
        logger.warning("Unexpected error fetching %s: %s", url, e)
        _cache_set(url, None)
        return None

    if "pdf" in content_type or url.lower().endswith(".pdf"):
        text = _extract_pdf_text(data, max_chars * 2)  # extract generously, truncate below
    else:
        try:
            text = _strip_html(data.decode("utf-8", errors="ignore"))
        except Exception as e:
            logger.warning("HTML decode/strip failed for %s: %s", url, e)
            text = None

    _cache_set(url, text)
    return text[:max_chars] if text else None
