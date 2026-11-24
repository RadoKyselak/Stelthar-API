"""Roll-call votes, from the two chambers' own files.

  Senate   senate.gov/legislative/LIS/roll_call_votes/...xml   one vote, every senator
           senate.gov/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml
           every vote of a session: question, result, tally, title (from 1989)
  House    clerk.house.gov/evs/{year}/roll{nnn}.xml            one vote, every member

No key. Congress.gov's bill actions link to these files; a nomination is
found in the Senate's session list by the nominee's name. A vote never
changes once taken, so it is kept for a week; the list of the session in
progress is kept for an hour.
"""
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import httpx

from config import logger
from evidence.cache import cached
from evidence.sources import SourceUnavailable

_HOUR = 3600
_WEEK = 7 * 24 * _HOUR
_UA = "Mozilla/5.0 (compatible; MiradorFactCheck/2; +https://factcheckgov.app)"
_SENATE_VOTE = re.compile(r"^https://www\.senate\.gov/legislative/LIS/roll_call_votes/vote(\d{3})(\d)/"
                          r"vote_\d{3}_\d_(\d{5})\.xml$")
_HOUSE_VOTE = re.compile(r"^https://clerk\.house\.gov/evs/(\d{4})/roll(\d{1,4})\.xml$")
FIRST_MENU_CONGRESS = 101

YEA, NAY, PRESENT, NOT_VOTING = "Yea", "Nay", "Present", "Not Voting"
_CAST = {"yea": YEA, "aye": YEA, "nay": NAY, "no": NAY, "present": PRESENT, "not voting": NOT_VOTING,
         "guilty": YEA, "not guilty": NAY}


def senate_vote_url(congress: int, session: int, number: int) -> str:
    return (f"https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/"
            f"vote_{congress}_{session}_{number:05d}.xml")


def page_url(xml_url: str) -> str:
    """The page a person reads, for the file a program reads."""
    m = _HOUSE_VOTE.match(xml_url)
    if m:
        return f"https://clerk.house.gov/Votes/{m.group(1)}{int(m.group(2))}"
    return re.sub(r"\.xml$", ".htm", xml_url)


async def _fetch(url: str, source: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0), follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": _UA})
    except httpx.HTTPError as e:
        raise SourceUnavailable(source, f"request failed: {type(e).__name__} {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise SourceUnavailable(source, f"HTTP {r.status_code}")
    return r.text


def _text(node, path: str) -> str:
    found = node.find(path) if node is not None else None
    return " ".join((found.text or "").split()) if found is not None else ""


def _int(s: str) -> int:
    m = re.search(r"\d+", s or "")
    return int(m.group(0)) if m else 0


def _cast(s: str) -> str:
    return _CAST.get((s or "").strip().lower(), (s or "").strip())


def parse_senate_vote(xml: str, url: str) -> Optional[Dict[str, Any]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    if root.tag != "roll_call_vote":
        return None
    when = None
    m = re.match(r"([A-Z][a-z]+ \d{1,2}, \d{4})", _text(root, "vote_date"))
    if m:
        try:
            when = datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
        except ValueError:
            when = None
    members = [{"last": _text(x, "last_name"), "first": _text(x, "first_name"), "party": _text(x, "party"),
                "state": _text(x, "state"), "vote": _cast(_text(x, "vote_cast"))}
               for x in root.findall("members/member")]
    return {"chamber": "Senate", "date": when, "question": _text(root, "vote_question_text"),
            "result": _text(root, "vote_result_text") or _text(root, "vote_result"),
            "title": _text(root, "vote_title"), "yea": _int(_text(root, "count/yeas")),
            "nay": _int(_text(root, "count/nays")),
            "tie_breaker": _text(root, "tie_breaker/tie_breaker_vote"),
            "number": _int(_text(root, "vote_number")), "members": members, "url": page_url(url)}


def parse_house_vote(xml: str, url: str) -> Optional[Dict[str, Any]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    meta = root.find("vote-metadata")
    if meta is None:
        return None
    when = None
    try:
        when = datetime.strptime(_text(meta, "action-date"), "%d-%b-%Y").date().isoformat()
    except ValueError:
        when = None
    members = []
    for rv in root.findall("vote-data/recorded-vote"):
        leg = rv.find("legislator")
        if leg is None:
            continue
        shown = " ".join((leg.text or "").split())
        members.append({"last": re.sub(r"\s*\([A-Z]{2}\)$", "", leg.get("unaccented-name") or shown),
                        "first": "", "party": leg.get("party") or "", "state": leg.get("state") or "",
                        "vote": _cast(_text(rv, "vote"))})
    totals = meta.find("vote-totals/totals-by-vote")
    return {"chamber": "House", "date": when,
            "question": " ".join(x for x in (_text(meta, "vote-question"), _text(meta, "legis-num")) if x),
            "result": _text(meta, "vote-result"), "title": _text(meta, "vote-desc"),
            "yea": _int(_text(totals, "yea-total")), "nay": _int(_text(totals, "nay-total")),
            "tie_breaker": "", "number": _int(_text(meta, "rollcall-num")), "members": members,
            "url": page_url(url)}


async def vote(url: str) -> Optional[Dict[str, Any]]:
    """One recorded vote with every member's vote, or None. Only the two
    chambers' own vote files are read: any other address is refused."""
    senate, house = _SENATE_VOTE.match(url or ""), _HOUSE_VOTE.match(url or "")
    if not (senate or house):
        return None

    async def load():
        xml = await _fetch(url, "Senate.gov" if senate else "Clerk of the House")
        if not xml:
            return None
        return parse_senate_vote(xml, url) if senate else parse_house_vote(xml, url)
    try:
        return await cached("rollcall", url, _WEEK, load)
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001 — an odd file is "not found", not a crash
        logger.exception("roll call %s failed", url)
        return None


def parse_senate_menu(xml: str) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    congress, session = _int(_text(root, "congress")), _int(_text(root, "session"))
    year = _int(_text(root, "congress_year"))
    out = []
    for v in root.findall("votes/vote"):
        when = None
        try:
            when = datetime.strptime(f"{_text(v, 'vote_date')}-{year}", "%d-%b-%Y").date().isoformat()
        except ValueError:
            when = None
        out.append({"congress": congress, "session": session, "number": _int(_text(v, "vote_number")),
                    "date": when, "issue": _text(v, "issue"),
                    "question": " ".join("".join(v.find("question").itertext()).split())
                    if v.find("question") is not None else "",
                    "result": _text(v, "result"), "yea": _int(_text(v, "vote_tally/yeas")),
                    "nay": _int(_text(v, "vote_tally/nays")), "title": _text(v, "title")})
    return out


def session_of(year: int) -> tuple:
    """(congress, session) sitting in a calendar year."""
    return (year - 1789) // 2 + 1, 1 if year % 2 else 2


async def senate_menu(congress: int, session: int) -> List[Dict[str, Any]]:
    """Every roll-call vote the Senate took in a session, newest first."""
    if congress < FIRST_MENU_CONGRESS or session not in (1, 2):
        return []
    url = f"https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml"
    current = session_of(date.today().year) == (congress, session)

    async def load():
        xml = await _fetch(url, "Senate.gov")
        return parse_senate_menu(xml) if xml else []
    try:
        return await cached("rollcall", url, _HOUR if current else _WEEK, load) or []
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("senate vote list %s/%s failed", congress, session)
        return []
