"""The Federal Register: every executive order, rule and proposed rule the
federal government publishes, read from federalregister.gov/api/v1.

No key. Executive orders are complete from 1994 in full text; a president is
named by a slug ("donald-trump"), an agency likewise
("environmental-protection-agency"). A new order can take a few days to be
published, so a count is "published so far".
"""
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import logger
from evidence.cache import cached
from evidence.sources import SourceUnavailable

_BASE = "https://www.federalregister.gov/api/v1/documents.json"
_HOUR = 3600
_FIELDS = ("title", "type", "action", "abstract", "html_url", "publication_date", "effective_on",
           "signing_date", "executive_order_number", "president", "document_number")

RULE, PROPOSED = "RULE", "PRORULE"


async def _get(params: List[Tuple[str, str]]) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as c:
            r = await c.get(_BASE, params=params)
    except httpx.HTTPError as e:
        raise SourceUnavailable("Federal Register", f"request failed: {type(e).__name__} {e}") from e
    if r.status_code >= 400:
        raise SourceUnavailable("Federal Register", f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        return {}


async def _read(params: List[Tuple[str, str]], ttl: int = _HOUR) -> Dict[str, Any]:
    async def load():
        return await _get(params)
    try:
        return await cached("fedreg", repr(sorted(params)), ttl, load) or {}
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001 — an odd payload is "not found", not a crash
        logger.exception("Federal Register query failed")
        return {}


def _base(per_page: int) -> List[Tuple[str, str]]:
    return [("per_page", str(per_page))] + [("fields[]", f) for f in _FIELDS]


def _order_params(president: str = "", year: Optional[int] = None, since: str = "", until: str = "",
                  term: str = "") -> List[Tuple[str, str]]:
    p = [("conditions[presidential_document_type][]", "executive_order")]
    if president:
        p.append(("conditions[president][]", president))
    if year:
        p.append(("conditions[signing_date][year]", str(year)))
    if since:
        p.append(("conditions[signing_date][gte]", since))
    if until:
        p.append(("conditions[signing_date][lte]", until))
    if term:
        p.append(("conditions[term]", term))
    return p


async def order_count(president: str, year: Optional[int] = None, since: str = "", until: str = "") -> Optional[int]:
    """How many executive orders a president signed, as published so far.

    Distinct order numbers are counted, not documents: the Federal Register
    also files corrections and reprints under "executive order", which would
    overstate the count (294 documents for Obama's 276 orders)."""
    params = [("per_page", "1000"), ("fields[]", "executive_order_number")] + \
        _order_params(president, year, since, until)
    numbers = set()
    for page in range(1, 4):
        d = await _read(params + [("page", str(page))])
        if not isinstance(d.get("count"), int):
            return None
        numbers.update(str(x.get("executive_order_number")) for x in d.get("results") or []
                       if x.get("executive_order_number"))
        if page >= int(d.get("total_pages") or 1):
            break
    return len(numbers)


async def orders(term: str, president: str = "", year: Optional[int] = None,
                 limit: int = 8) -> List[Dict[str, Any]]:
    """Executive orders matching words, most relevant first."""
    d = await _read(_base(limit) + _order_params(president, year, term=term) + [("order", "relevance")])
    return list(d.get("results") or [])


async def order_numbered(number: int) -> Optional[Dict[str, Any]]:
    d = await _read(_base(10) + _order_params(term=str(number)), ttl=24 * _HOUR)
    for doc in d.get("results") or []:
        if str(doc.get("executive_order_number") or "") == str(number):
            return doc
    return None


async def rules(term: str, agency: str, kind: str = RULE, year: Optional[int] = None,
                limit: int = 8) -> List[Dict[str, Any]]:
    """An agency's final (or proposed) rules matching words, most relevant first."""
    p = _base(limit) + [("conditions[type][]", kind), ("conditions[agencies][]", agency),
                        ("conditions[term]", term), ("order", "relevance")]
    if year:
        p.append(("conditions[publication_date][year]", str(year)))
    d = await _read(p)
    return list(d.get("results") or [])
