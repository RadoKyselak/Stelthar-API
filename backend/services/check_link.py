"""Check the claims on a web page, given its address.

Someone holding a link (to a press release, a campaign page, a transcript)
should not have to open it, select the text and paste it. The page is fetched
here, its article text is picked out, and its claims are checked like any
passage.

Three things are decided in this file:

  * Free data only, always. A page is text nobody chose sentence by sentence,
    so it is checked the way the extension would scan one: only claims the
    free government data can settle, each needing a figure written as its
    measure is written (see claims/splitter.py), and never a model call.
  * Which addresses are fetched at all: utils/safe_fetch.py. Video and social
    sites are refused up front, since their pages hold no text to read.
  * What is said when it does not work. Many news sites refuse automated
    readers, and this service identifies itself honestly rather than pose as a
    browser. The answer then says so and names what does work: highlighting
    the text on the page.
"""
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from claims import article as article_reader
from services.check_text import PAGE, check_text
from utils.safe_fetch import FetchFailed, FetchRefused, Page, fetch_public_page

# Pages that are a player or a feed, not text.
_NOT_TEXT = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "facebook.com", "x.com", "twitter.com",
             "threads.net", "vimeo.com", "spotify.com", "twitch.tv")
_HINT = "Highlight the text on the page, right-click and choose Check with Project Mirador, or paste it here."

_CACHE: Dict[str, tuple] = {}     # address -> (when, Page): a recheck does not fetch again
_CACHE_SECONDS = 600
_CACHE_MAX = 64


def looks_like_link(text: str) -> bool:
    """A lone web address, as opposed to a claim that mentions one."""
    text = (text or "").strip()
    return text.lower().startswith(("http://", "https://")) and " " not in text and "\n" not in text


async def _page(url: str) -> Page:
    now = time.time()
    hit = _CACHE.get(url)
    if hit and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    page = await fetch_public_page(url)
    _CACHE[url] = (now, page)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    return page


async def check_link(url: str, client_ip: Optional[str] = None) -> Dict[str, Any]:
    """Every checkable claim on the page at `url`. Raises FetchRefused or
    FetchFailed with a message meant for the person who asked."""
    host = (urlsplit(url.strip()).hostname or "").lower()
    if any(host == site or host.endswith("." + site) for site in _NOT_TEXT):
        raise FetchRefused(f"Video and social media pages can't be read. {_HINT}")
    try:
        page = await _page(url.strip())
    except FetchFailed as e:
        raise FetchFailed(f"{e} {_HINT}")

    if page.content_type == "text/plain":
        blocks = [b.strip() for b in page.text.split("\n\n") if len(b.strip()) >= 40]
        found = article_reader.Article(title="", blocks=blocks[:400])
    else:
        found = article_reader.extract(page.text)
    if not found.blocks:
        raise FetchFailed(f"No readable text was found on that page. {_HINT}")

    result = await check_text(found.text, mode=PAGE, client_ip=client_ip)
    result["page"] = {"url": page.url, "host": page.host, "title": found.title,
                      "paragraphs": len(found.blocks)}
    return result
