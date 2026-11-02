"""Published fact-checks of similar claims, from Google's Fact Check Tools API.

Fact-checkers (PolitiFact, FactCheck.org, Snopes, AP, Reuters, USA Today and
others) tag their articles with ClaimReview markup; Google indexes it and
serves it here. Free, with a key; no model call.

They are shown beside the check, with each outlet's own rating, date and link.
When a claim goes to web search they are also handed to the model as leads,
and a published rating that agrees with the answer counts toward its
confidence (one that disagrees is named and caps it). They never decide a
verdict alone: the search matches on wording, so a result can be about another
year, person or number.
"""
import asyncio
import contextvars
import re
from typing import Any, Dict, List, Optional

import httpx

import config
from config import logger
from evidence.cache import cached

_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
_DAY = 24 * 3600
_WAIT = 2.5          # seconds to wait after the check itself has finished
MAX_SHOWN = 3

_STOP = frozenset("""
a an the and or but of in on at to for from by with as is was were be been are it its this that
these those than then there their they he she his her we our you your i not no so if into over
under about after before during per percent million billion trillion thousand year years
""".split())
_WORD_RE = re.compile(r"[a-z][a-z'-]+|\d[\d,.]*")


def available() -> bool:
    return bool(getattr(config, "GOOGLE_FACTCHECK_API_KEY", None))


def _words(text: str) -> set:
    out = set()
    for w in _WORD_RE.findall((text or "").lower()):
        w = w.strip(".,'-")
        if w[:1].isdigit() or (len(w) > 2 and w not in _STOP):
            out.add(w.replace(",", ""))
    return out


_YEAR_RE = re.compile(r"(?:19|20)\d\d")


def _keywords(text: str, n: int = 8) -> str:
    seen = []
    for w in _WORD_RE.findall((text or "").lower()):
        w = w.strip(".,'-")
        if len(w) > 2 and w not in _STOP and w not in seen and not w[0].isdigit():
            seen.append(w)
    return " ".join(seen[:n])


async def _query(q: str) -> List[Dict[str, Any]]:
    from evidence.ai_search import normalise
    params = {"query": q[:200], "languageCode": "en", "pageSize": "10",
              "key": config.GOOGLE_FACTCHECK_API_KEY}

    async def load():
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                r = await client.get(_URL, params=params)
        except httpx.HTTPError as e:
            logger.warning("Fact-check search failed: %s", type(e).__name__)
            return None
        if r.status_code != 200:
            # The URL carries the key: log the status only.
            logger.warning("Fact-check search returned %s", r.status_code)
            return None
        try:
            return (r.json() or {}).get("claims") or []
        except ValueError:
            return None

    return await cached("factcheck", normalise(q), _DAY, load) or []


def _https(u: Any) -> str:
    u = str(u or "").strip()
    return u if u.startswith("https://") and len(u) < 600 else ""


def _clip(s: Any, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def pick(claim: str, found: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """The few reviews whose claim is worded most like this one."""
    mine = _words(claim)
    if not mine:
        return []
    my_numbers = {w for w in mine if w[0].isdigit()}
    scored = []
    for c in found:
        theirs = _words(c.get("text", ""))
        shared = mine & theirs
        overlap = len(shared) / max(1, min(len(mine), len(theirs)))
        if len(shared) < 2 or overlap < 0.34:
            continue
        same = my_numbers & theirs
        figures = {n for n in same if not _YEAR_RE.fullmatch(n)}
        score = overlap + (0.3 if figures else 0.0) + (0.1 if same - figures else 0.0)
        for rv in c.get("claimReview") or []:
            url = _https(rv.get("url"))
            rating = _clip(rv.get("textualRating"), 60)
            pub = rv.get("publisher") or {}
            who = _clip(pub.get("name") or pub.get("site"), 60)
            if not url or not rating or not who:
                continue
            scored.append((score, str(rv.get("reviewDate") or "")[:10], {
                "publisher": who,
                "rating": rating,
                "title": _clip(rv.get("title") or c.get("text"), 160),
                "claim": _clip(c.get("text"), 200),
                "claimant": _clip(c.get("claimant"), 80),
                "date": str(rv.get("reviewDate") or "")[:10],
                "url": url,
            }))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    out, urls = [], set()
    for _, _, item in scored:
        if item["url"] in urls:
            continue
        urls.add(item["url"])
        out.append(item)
        if len(out) == MAX_SHOWN:
            break
    return out


async def search(claim: str) -> List[Dict[str, str]]:
    """Never raises: related fact-checks are extra, not part of the check."""
    if not available() or not (claim or "").strip():
        return []
    try:
        found = await _query(claim)
        got = pick(claim, found)
        if not got:
            kw = _keywords(claim)
            if kw and kw != claim.lower():
                got = pick(claim, await _query(kw))
        return got
    except Exception:  # noqa: BLE001
        logger.exception("Fact-check search error")
        return []


def start(claim: str) -> Optional["asyncio.Task"]:
    """Begin the search alongside the check."""
    return asyncio.create_task(search(claim)) if available() else None


async def finish(task: Optional["asyncio.Task"]) -> List[Dict[str, str]]:
    if task is None:
        return []
    try:
        return await asyncio.wait_for(task, timeout=_WAIT)
    except Exception:  # noqa: BLE001  (timeout or error: the check goes out without them)
        task.cancel()
        return []


# The search for the claim being checked, started by the endpoint so the web
# search step can read its results without a parameter threaded through.
CURRENT: "contextvars.ContextVar[Optional[asyncio.Task]]" = contextvars.ContextVar(
    "fact_checks_current", default=None)


async def peek(task: Optional["asyncio.Task"], timeout: float = 3.0) -> List[Dict[str, str]]:
    """The results if they arrive within `timeout`, without cancelling the
    search (the endpoint still waits for it to show them)."""
    if task is None:
        return []
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        return list(task.result() or []) if task in done else []
    except Exception:  # noqa: BLE001
        return []


_FALSE = re.compile(r"\b(false|pants on fire|incorrect|wrong|fake|fabricated|not true|untrue|"
                    r"baseless|no evidence|debunked|four pinocchios|hoax|scam|bogus|inaccurate)\b", re.I)
_MIXED = re.compile(r"\b(misleading|missing context|half[- ]true|mixture|mixed|partly|partially|"
                    r"needs context|not the whole story|exaggerat|distort|out of context|"
                    r"cherry|spin|two pinocchios|three pinocchios|lacks context|unproven)\w*", re.I)
_MOSTLY = re.compile(r"\bmostly true\b|\blargely (true|accurate|correct)\b", re.I)
_TRUE = re.compile(r"\b(true|correct|accurate|right|confirmed|geppetto checkmark)\b", re.I)


def stance(rating: str) -> Optional[str]:
    """A publisher's rating on the checker's scale, or None when it doesn't map:
    SUPPORTED, MOSTLY_ACCURATE, MISLEADING or CONTRADICTED."""
    r = (rating or "").strip()
    if not r:
        return None
    if re.search(r"\bmostly false\b", r, re.I):
        return "CONTRADICTED"
    if _MOSTLY.search(r):
        return "MOSTLY_ACCURATE"
    if _MIXED.search(r):
        return "MISLEADING"
    if _FALSE.search(r):
        return "CONTRADICTED"
    if _TRUE.search(r):
        return "SUPPORTED"
    return None


def as_leads(found: List[Dict[str, str]]) -> str:
    """Published fact-checks as a hint for the web search step."""
    lines = []
    for f in found[:MAX_SHOWN]:
        when = f" ({f['date']})" if f.get("date") else ""
        lines.append(f"{f['publisher']}{when} rated a similar claim \u201c{f['rating']}\u201d: "
                     f"\u201c{f.get('claim') or f.get('title')}\u201d {f['url']}")
    if not lines:
        return ""
    return ("Published fact-checks of similar claims (they may concern another year, person or "
            "figure, so check that they match before relying on one): " + " | ".join(lines))
