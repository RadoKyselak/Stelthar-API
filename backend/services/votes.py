"""Claims about how Congress voted, read from the chambers' own roll calls.

  a member     "Senator Joe Manchin voted for the Inflation Reduction Act"
  a party      "Every Republican senator voted against the Inflation Reduction Act"
               "Only three House Democrats voted for H.R. 1"
  a nominee    "The Senate confirmed Ketanji Brown Jackson 53-47"
               "Senator Susan Collins voted to confirm Brett Kavanaugh"
  a Congress   "The 118th Congress enacted fewer than 300 laws"

The vote read for a bill is the last one a chamber took on passing it (the
final version, when a bill went back and forth), found through Congress.gov's
record of the bill; each senator's or representative's vote is in the
Senate's or the House Clerk's file for that roll call. A nomination is found
by the nominee's name in the Senate's list of votes for a session.

Only a recorded vote is read. A bill passed by voice vote or unanimous
consent has no roll call, and no claim about who voted for it is settled
here. A name missing from a roll call is never read as a "no" vote.
"""
import asyncio
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from claims.verdict import CONTRADICTED, MISLEADING, MOSTLY_ACCURATE, SUPPORTED, Verdict
from config import logger
from evidence.excerpt import row as ex_row
from evidence.sources import SourceUnavailable, congress, rollcall
from evidence.units import UNKNOWN
from services.regulatory import fact, figure_check, verdict

ONLY_ROLL_CALL = "This reads the recorded vote, not the reasons for it or what the measure does."

FOR, AGAINST, NOT_FOR, ABSENT = "for", "against", "not_for", "absent"
ALL, NONE, COUNT = "all", "none", "count"
UPPER, LOWER = "Senate", "House"

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a single": 1, "single": 1}
_NUM = r"(?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_TITLE = r"(?:Sen\.|Senator|Rep\.|Representative|Congressman|Congresswoman|Leader|Speaker)"
_PERSON = r"[A-Z][\w'’.-]*(?:\s+(?:[A-Z][\w'’.-]*|de|del|la|van|von)){0,3}"
_FOR = r"(?:for|in favou?r of|yes on|yea on|aye on|to pass|to approve|to advance|to confirm|to support)"
_AGAINST = r"(?:against|no on|nay on|to block|to reject|to kill|to oppose|down)"
_VOTED = re.compile(
    r"\b(?P<neg>did(?:n['’]t| not) vote|never voted|voted|votes|voting)\s+"
    r"(?P<dir>" + _FOR + "|" + _AGAINST + r")\b")
_MISSED = re.compile(r"\b(?:did(?:n['’]t| not) vote|missed the vote|skipped the vote|was absent|abstained)\b")
_MEMBER = re.compile(r"(?P<title>" + _TITLE + r")?\s*(?P<name>" + _PERSON + r")(?:\s*\([^)]{1,12}\))?\s+"
                     r"(?:(?:has|had|also|then|later|ultimately|still|even)\s+){0,2}$")
_PARTY_WORD = r"(?P<party>Republicans?|Democrats?|Democratic|GOP|independents?)"
_ROLE = r"(?:senators?|representatives?|House members?|members(?: of Congress| of the House)?|lawmakers|" \
        r"congress(?:men|women|man|woman)|legislators)"
_QUANT = re.compile(
    r"\b(?P<q>every(?: single)?|all(?: of the)?(?:\s+\d{1,3})?|each|no|zero|none of the|not a single|not one|"
    r"(?:only|just|exactly)\s+" + _NUM + r"|a single|" + _NUM + r")\s+"
    r"(?:(?P<ch1>House|Senate)\s+)?" + _PARTY_WORD + r"(?:\s+(?:in the\s+)?(?P<role>" + _ROLE + r"))?"
    r"(?:\s+in\s+the\s+(?P<ch2>House|Senate))?\b", re.I)
_QUANT_AFTER = re.compile(
    r"\b(?:(?P<ch1>House|Senate)\s+)?" + _PARTY_WORD + r"(?:\s+in\s+the\s+(?P<ch2>House|Senate))?\s+"
    r"(?P<q>all|unanimously)\b", re.I)
_CHAMBER_WORD = re.compile(r"\b(?:in|by|through)\s+the\s+(House|Senate)\b|\b(House|Senate)\s+(?:vote|floor)\b")
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_TALLY = re.compile(r"\b(\d{1,3})\s*(?:-|–|—|to)\s*(\d{1,3})\b")
_NOT_NAMES = {"the", "it", "he", "she", "they", "this", "that", "who", "congress", "senate", "house", "bill",
              "republicans", "democrats", "lawmakers", "nobody", "everyone", "senators", "members", "president",
              "republican", "democrat", "most", "many", "some", "several", "both", "all", "and", "but", "yet",
              "then", "in", "on", "when", "while", "after", "before", "although", "white"}

_CONFIRM_SENATE = re.compile(
    r"\bSenate\s+(?:has\s+|had\s+)?(?:\w+ly\s+)?(?P<verb>confirmed|rejected|voted to confirm|voted to reject|"
    r"voted down|voted against confirming)\s+(?:the nomination of\s+|President \w+['’]s nominee,?\s+)?"
    r"(?:(?:Judge|Justice|Dr\.|Gen\.|General|Gov\.|Governor|Sen\.|Senator|Rep\.|Secretary|Director)\s+)?"
    r"(?P<name>" + _PERSON + r")")
_CONFIRM_PASSIVE = re.compile(
    r"(?P<name>" + _PERSON + r")(?:['’]s nomination)?\s+(?:was|has been|had been|got)\s+(?:\w+ly\s+)?"
    r"(?P<verb>confirmed|rejected)\b")
_CONFIRM_MEMBER = re.compile(
    r"\b(?:to confirm|against confirming|for confirming|(?:for|against)(?: the)?(?: nomination| confirmation)? of|"
    r"for|against)\s+(?:(?:Judge|Justice|Dr\.|Gen\.|General|Secretary|Director)\s+)?(?P<name>" + _PERSON + r")"
    r"(?:['’]s\s+(?:confirmation|nomination))?")
_NOMINEE_CUE = re.compile(r"\bconfirm\w*|\bnominat\w+|\bnominee\b", re.I)
_CONGRESS_CUE = re.compile(r"\bSenat\w+|\bHouse\b|\bCongress\w*|\bRep\.|\bSen\.|\bRepresentative\b|\blawmakers?\b|"
                           r"\bbill\b|\bAct\b|\bamendment\b|\bresolution\b|\bnominat\w+|\bconfirm\w*")
_SCAN_SESSIONS = 24   # twelve years back, when the claim names no year
_SCAN_BATCH = 6

_LAWS = re.compile(
    r"\b(?:the\s+)?(?P<c>\d{2,3})(?:st|nd|rd|th)\s+Congress\s+(?:has\s+|had\s+)?(?:only\s+|just\s+)?"
    r"(?:passed|enacted|produced|made)\s+(?:(?:only|just|more than|fewer than|less than|over|under|about|"
    r"around|nearly|almost|at least)\s+)?(?P<n>\d[\d,]*)\s+(?:new\s+|public\s+)?(?:laws|bills into law)\b", re.I)


# ------------------------------------------------------------------ reading

def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower()


def _tokens(name: str) -> List[str]:
    return [t for t in re.findall(r"[a-z]+", _fold(name).replace("’", "'").replace("'", ""))
            if t not in ("jr", "sr", "ii", "iii", "iv")]


def _number(q: str) -> Optional[int]:
    m = re.search(r"\d{1,3}", q)
    if m:
        return int(m.group(0))
    for word, n in _NUM_WORDS.items():
        if re.search(r"\b" + word + r"\b", q, re.I):
            return n
    return None


def _party(word: str) -> str:
    w = word.lower()
    return "R" if w.startswith(("republican", "gop")) else "D" if w.startswith("democrat") else "I"


@dataclass
class Read:
    kind: str                         # "member", "party", "confirm", "laws"
    direction: str = ""               # FOR, AGAINST, NOT_FOR, ABSENT
    name: str = ""                    # the member
    chamber: str = ""                 # UPPER, LOWER or "" (not said)
    party: str = ""                   # R, D, I
    quantity: str = ""                # ALL, NONE, COUNT
    number: Optional[int] = None
    exact: bool = False               # "only three": no more than that
    nominee: str = ""
    role_words: Tuple[str, ...] = ()
    confirmed: bool = True            # the claim says the Senate confirmed (False: rejected)
    tally: Optional[Tuple[int, int]] = None
    year: Optional[int] = None
    congress: Optional[int] = None
    figure: str = ""


def _direction(m) -> str:
    neg = not m.group("neg").lower().startswith(("voted", "votes", "voting"))
    against = bool(re.match(_AGAINST, m.group("dir"), re.I))
    if neg:
        return FOR if against else NOT_FOR   # "did not vote against" is read as a vote for
    return AGAINST if against else FOR


def _chamber_of(title: str, text: str) -> str:
    t = (title or "").lower()
    if t.startswith("sen"):
        return UPPER
    if t.startswith(("rep", "congress", "speaker")):
        return LOWER
    return ""


def _member_before(text: str, at: int) -> Optional[Tuple[str, str]]:
    m = _MEMBER.search(text[:at])
    if not m:
        return None
    name = m.group("name").strip()
    words = name.split()
    while words and _fold(words[0]) in _NOT_NAMES:
        words = words[1:]
    if not words or _fold(words[-1]) in _NOT_NAMES or words[-1] == "BILL":
        return None
    return " ".join(words), _chamber_of(m.group("title") or "", text)


def _party_read(text: str, at: int) -> Optional[Read]:
    before = text[:at]
    m = None
    for m in _QUANT.finditer(before):
        pass
    after = None
    for after in _QUANT_AFTER.finditer(before):
        pass
    hit = m if m and at - m.end() <= 40 else None
    if hit is None and after is not None and at - after.end() <= 6:
        hit = after
    if hit is None:
        return None
    q = hit.group("q").lower()
    chamber = hit.group("ch1") or hit.group("ch2") or ""
    role = (hit.groupdict().get("role") or "").lower()
    if not chamber and role.startswith("senator"):
        chamber = UPPER
    elif not chamber and (role.startswith(("representative", "house", "congress")) or "of the house" in role):
        chamber = LOWER
    r = Read(kind="party", party=_party(hit.group("party")), chamber=chamber.title())
    if q.startswith(("every", "all", "each", "unanimously")):
        r.quantity = ALL
        r.number = _number(q) if re.search(r"\d", q) else None
    elif q in ("no", "zero", "none of the", "not a single", "not one"):
        r.quantity = NONE
    else:
        n = _number(q)
        if n is None:
            return None
        r.quantity, r.number = COUNT, n
        r.exact = True
    return r


def read(text: str, bill_span: Optional[Tuple[int, int]] = None) -> Optional[Read]:
    """What the claim says about a vote, or None."""
    t = " ".join((text or "").split())
    if not t:
        return None
    laws = _LAWS.search(t)
    if laws:
        return Read(kind="laws", congress=int(laws.group("c")), figure=laws.group("n"))
    years = [int(y) for y in _YEAR.findall(t)]
    year = years[0] if years else None

    rest = t
    if bill_span:
        rest = t[:bill_span[0]] + "BILL" + t[bill_span[1]:]
    voted = _VOTED.search(rest)
    if voted:
        r = _party_read(rest, voted.start())
        if r is None:
            who = _member_before(rest, voted.start())
            if who:
                r = Read(kind="member", name=who[0], chamber=who[1])
        if r is not None:
            r.direction, r.year = _direction(voted), year
            if not r.chamber:
                c = _CHAMBER_WORD.search(rest)
                if c:
                    r.chamber = c.group(1) or c.group(2)
            if bill_span is None:
                if not _NOMINEE_CUE.search(t):
                    return None
                nm = _CONFIRM_MEMBER.search(rest, voted.start())
                if not nm:
                    return None
                r.nominee, r.chamber = nm.group("name"), UPPER
            return r
    missed = _MISSED.search(rest)
    if missed and bill_span:
        who = _member_before(rest, missed.start())
        if who:
            return Read(kind="member", name=who[0], chamber=who[1], direction=ABSENT, year=year)

    if bill_span is None:
        m = _CONFIRM_SENATE.search(t) or _CONFIRM_PASSIVE.search(t)
        if m and len(_tokens(m.group("name"))) >= 2:
            verb = m.group("verb").lower()
            r = Read(kind="confirm", nominee=m.group("name"), chamber=UPPER, year=year,
                     confirmed=not verb.startswith(("rejected", "voted to reject", "voted down", "voted against")))
            tail = t[m.end():]
            role = re.search(r"\b(?:as|to be|to the|for)\s+(?:the\s+|an?\s+)?([A-Za-z][\w .'’-]{2,80})", tail)
            if role:
                r.role_words = tuple(w for w in _tokens(role.group(1))
                                     if w not in ("the", "of", "and", "for", "united", "states", "in", "by", "a"))[:6]
            scrub = _YEAR.sub(" ", t)
            tally = _TALLY.search(scrub)
            if tally:
                a, b = int(tally.group(1)), int(tally.group(2))
                if a <= 101 and b <= 101 and a + b >= 3:
                    r.tally = (a, b)
            return r
    return None


# ------------------------------------------------------------------ records

def _passage_votes(acts: Sequence[dict]) -> Dict[str, str]:
    """The roll call each chamber last took on passing the bill: chamber -> file."""
    best: Dict[str, Tuple[str, str]] = {}
    for a in acts:
        votes = a.get("recordedVotes") or []
        if not votes:
            continue
        t = a.get("text") or ""
        code = str(a.get("actionCode") or "")
        passage = (code in ("8000", "17000", "9000", "18000")
                   or re.match(r"(?:Passed/agreed to in|Failed of passage/not agreed to in|On passage|"
                               r"Passed (?:House|Senate))", t)
                   or (re.search(r"\bagree to the (?:Senate|House) amendments?\b|\bconference report\b", t, re.I)
                       and re.search(r"\bAgreed to\b|\bagreed to\b|\bFailed\b", t))
                   or re.search(r"\bconcurred in the (?:House|Senate) amendments?\b", t))
        if not passage or re.search(r"\bmotion to (?:recommit|commit|table|proceed|reconsider)\b|\bcloture\b", t, re.I):
            continue
        day = a.get("actionDate") or ""
        for v in votes:
            ch = UPPER if str(v.get("chamber") or "").lower().startswith("s") else LOWER
            url = v.get("url") or ""
            if url and (ch not in best or day >= best[ch][0]):
                best[ch] = (day, url)
    return {ch: url for ch, (_, url) in best.items()}


def _find_member(vote: dict, name: str) -> List[dict]:
    want = _tokens(name)
    if not want:
        return []
    last = want[-1]
    hits = [m for m in vote["members"] if _tokens(m["last"])[-1:] == [last]
            or " ".join(_tokens(m["last"])) == " ".join(want[-len(_tokens(m["last"])):])]
    if len(hits) > 1 and len(want) > 1:
        first = [m for m in hits if m["first"] and _tokens(m["first"])[:1] == want[:1]]
        if first:
            hits = first
    return hits


def _show_member(m: dict, chamber: str, asked: str) -> str:
    title = "Sen." if chamber == UPPER else "Rep."
    full = f"{m['first']} {m['last']}".strip() if m["first"] else (asked if len(asked.split()) > 1 else m["last"])
    return f"{title} {full} ({m['party']}-{m['state']})"


def _show_day(iso: Optional[str]) -> str:
    try:
        d = date.fromisoformat(iso or "")
    except ValueError:
        return "an unknown date"
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _tally(v: dict) -> str:
    extra = " and the Vice President broke the tie" if v.get("tie_breaker") and v["yea"] == v["nay"] else ""
    return f"{v['yea']}–{v['nay']}{extra}"


def _outcome(v: dict) -> str:
    """"Bill Passed, 51–50" from a chamber's own wording of the result."""
    result = re.sub(r"\s*\(.*$", "", v["result"] or "").strip() or "result not stated"
    return f"{result}, {_tally(v)}"


def _counts(v: dict, party: str) -> Dict[str, int]:
    out = {rollcall.YEA: 0, rollcall.NAY: 0, rollcall.PRESENT: 0, rollcall.NOT_VOTING: 0}
    for m in v["members"]:
        if (m["party"] or "").upper().startswith(party):
            out[m["vote"]] = out.get(m["vote"], 0) + 1
    return out


def _party_name(p: str, n: int = 2) -> str:
    return {"R": "Republican", "D": "Democrat", "I": "independent"}[p] + ("" if n == 1 else "s")


def _vote_rows(v: dict, cited: str):
    rows = [ex_row(f"{v['chamber']} roll call {v['number']}, {_show_day(v['date'])}", cited, cited=True),
            ex_row("Question", v["question"] or v["title"]),
            ex_row("Result", f"{v['result']} ({v['yea']} yea, {v['nay']} nay)")]
    for p in ("R", "D", "I"):
        c = _counts(v, p)
        if sum(c.values()):
            rows.append(ex_row(_party_name(p), f"{c[rollcall.YEA]} yea, {c[rollcall.NAY]} nay, "
                                               f"{c[rollcall.NOT_VOTING] + c[rollcall.PRESENT]} not voting or present"))
    return rows


def _vote_fact(v: dict, label: str, official: str) -> Any:
    return fact("SENATE" if v["chamber"] == UPPER else "HOUSE CLERK", label, official, v["url"],
                year=int((v["date"] or "0")[:4] or 0) or None, record_date=v["date"] or "",
                basis=f"{v['chamber']} roll call vote", rows=_vote_rows(v, official), note=ONLY_ROLL_CALL)


# ------------------------------------------------------------------ judging

def _judge_member(r: Read, votes: List[dict], subject: str) -> Optional[Verdict]:
    found = []
    for v in votes:
        hits = _find_member(v, r.name)
        if len(hits) == 1:
            found.append((v, hits[0]))
        elif len(hits) > 1 and (not r.chamber or v["chamber"] == r.chamber):
            return None     # two members share the name; the claim does not say which
    # "Senator Sanders voted against the Patriot Act": he was in the House
    # then. The chamber the claim's title names wins; the other is a fallback.
    named = [f for f in found if f[0]["chamber"] == r.chamber]
    found = named or found
    if len(found) != 1:
        return None
    v, m = found[0]
    who = _show_member(m, v["chamber"], r.name)
    cast = m["vote"]
    did = {rollcall.YEA: "voted Yea (for)", rollcall.NAY: "voted Nay (against)",
           rollcall.PRESENT: "voted Present", rollcall.NOT_VOTING: "did not vote"}.get(cast, f"is recorded as {cast}")
    good = {FOR: cast == rollcall.YEA, AGAINST: cast == rollcall.NAY, NOT_FOR: cast != rollcall.YEA,
            ABSENT: cast in (rollcall.NOT_VOTING, rollcall.PRESENT)}[r.direction]
    line = f"{who} {did} on {subject}, in the {v['chamber']} roll call of {_show_day(v['date'])} ({_outcome(v)})."
    caveats = [ONLY_ROLL_CALL]
    if r.year and v["date"] and int(v["date"][:4]) != r.year:
        good = False
        line += f" The vote was in {v['date'][:4]}, not {r.year}."
    dp = _vote_fact(v, f"{who} on {subject}", cast)
    return verdict(SUPPORTED if good else CONTRADICTED, line, [dp], caveats=caveats, claimed=r.direction)


def _judge_party(r: Read, votes: List[dict], subject: str) -> Optional[Verdict]:
    lines, outcomes, dps, sides = [], [], [], []
    for v in votes:
        c = _counts(v, r.party)
        yea, nay = c[rollcall.YEA], c[rollcall.NAY]
        away = c[rollcall.NOT_VOTING] + c[rollcall.PRESENT]
        if yea + nay + away == 0:
            continue
        side = yea if r.direction == FOR else nay
        other = nay if r.direction == FOR else yea
        if r.quantity == ALL:
            ok = other == 0 and side > 0
        elif r.quantity == NONE:
            ok = side == 0
        else:
            ok = side == r.number
        outcomes.append(ok)
        sides.append(side)
        away_note = f", and {away} did not vote" if away else ""
        lines.append(f"In the {v['chamber']} roll call of {_show_day(v['date'])} on {subject}, "
                     f"{yea} {_party_name(r.party, yea)} voted for and {nay} against{away_note}.")
        dps.append(_vote_fact(v, f"{_party_name(r.party)} on {subject}", f"{yea} for, {nay} against"))
    if not outcomes:
        return None
    if r.quantity == COUNT and not r.chamber and len(outcomes) > 1:
        good = any(outcomes)
    else:
        good = all(outcomes)
    caveats = [ONLY_ROLL_CALL]
    if not r.chamber and len(outcomes) > 1:
        caveats.append("The claim names no chamber; both chambers' votes are shown.")
    if good and r.quantity == ALL and r.number is not None and len(sides) == 1 and sides[0] != r.number:
        caveats.append(f"The claim says all {r.number}; {sides[0]} are recorded on that side. Members who did "
                       f"not vote, and independents who sit with a party, are counted separately.")
        return verdict(MOSTLY_ACCURATE, " ".join(lines), dps, caveats=caveats)
    if good is False and any(outcomes) and r.quantity != COUNT:
        return verdict(MISLEADING, " ".join(lines), dps, caveats=caveats)
    return verdict(SUPPORTED if good else CONTRADICTED, " ".join(lines), dps, caveats=caveats)


async def _bill_votes(text: str) -> Optional[Tuple[Read, List[dict], str]]:
    from services import bills
    t = " ".join((text or "").split())
    ref = bills._ref(t)
    if ref is None or ref.level != "federal":
        return None
    r = read(t, ref.span)
    if r is None or r.kind not in ("member", "party"):
        return None
    if ref.congress is None and r.year and not ref.name:
        ref.congress, ref.congress_from = bills.congress_of(r.year), "year"
    rec, _ = await bills._federal(ref)
    if rec is None:
        return None
    acts = await congress.actions(ref.congress, ref.kind, ref.number)
    files = _passage_votes(acts)
    wanted = [r.chamber] if r.chamber and r.kind == "party" else [UPPER, LOWER]
    votes = [v for v in await asyncio.gather(*(rollcall.vote(files[ch]) for ch in wanted if ch in files)) if v]
    if not votes:
        return None
    subject = f"{rec.label}, the {rec.title}" if rec.title and len(rec.title) <= 90 else rec.label
    # A vote in a year the bill did not name is the bill's own date; drop it
    # as a claim about when, since the year was used to pick the Congress.
    if ref.congress_from == "year":
        r.year = None
    return r, votes, subject


# ------------------------------------------------------------------ nominations

def _same_given(a: str, b: str) -> bool:
    """Pete and Peter, Tom and Thomas: one given name is the start of the other."""
    return a == b or (min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)))


def _nominee_votes(menu: Sequence[dict], nominee: str) -> List[dict]:
    want = _tokens(nominee)
    out = []
    for v in menu:
        q = v["question"].lower()
        if "nomination" not in q or "cloture" in q or "motion" in q:
            continue
        # The title opens with the nominee: "Confirmation: Peter Hegseth, of Tennessee, to be..."
        named = _tokens(re.sub(r"^(?:Confirmation:?|Nomination\s*-)\s*", "", v["title"]).split(",")[0])
        if len(want) >= 2 and named and want[-1] in named \
                and all(any(_same_given(w, n) for n in named) for w in want[:-1]):
            out.append(v)
    return out


async def _confirmation(r: Read) -> Optional[Tuple[dict, int]]:
    """The Senate's vote on a nominee, and how many such votes there were."""
    this_year = date.today().year
    if r.year:
        sessions = [rollcall.session_of(r.year)]
    else:
        sessions = [rollcall.session_of(y) for y in range(this_year, this_year - _SCAN_SESSIONS, -1)]
    sessions = [s for s in sessions if s[0] >= rollcall.FIRST_MENU_CONGRESS]
    for i in range(0, len(sessions), _SCAN_BATCH):
        menus = await asyncio.gather(*(rollcall.senate_menu(c, s) for c, s in sessions[i:i + _SCAN_BATCH]))
        hits = [v for menu in menus for v in _nominee_votes(menu, r.nominee)]
        if hits:
            def fit(v):
                return (sum(w in _tokens(v["title"]) for w in r.role_words), v["date"] or "")
            best = max(hits, key=fit)
            return best, len(hits)
    return None


def _post(title: str) -> str:
    t = re.sub(r"^(?:Confirmation:?|Nomination\s*-)\s*", "", title or "").strip()
    return t[:1].upper() + t[1:] if t else "the nomination"


async def _check_confirm(r: Read) -> Optional[Verdict]:
    found = await _confirmation(r)
    if found is None:
        return None
    menu_vote, several = found
    url = rollcall.senate_vote_url(menu_vote["congress"], menu_vote["session"], menu_vote["number"])
    v = await rollcall.vote(url)
    if v is None:
        return None
    confirmed = "confirmed" in v["result"].lower() and "not" not in v["result"].lower()
    subject = _post(menu_vote["title"])
    caveats = [ONLY_ROLL_CALL]
    if several > 1:
        caveats.append(f"The Senate held {several} confirmation votes matching this name; this is the "
                       f"{'one matching the post named' if r.role_words else 'most recent'}.")
    if r.kind == "member":
        return _judge_member(r, [v], f"the nomination of {subject}")
    yea = v["yea"] + (1 if v.get("tie_breaker", "").lower().startswith("yea") and v["yea"] == v["nay"] else 0)
    line = (f"The Senate {'confirmed' if confirmed else 'did not confirm'} {subject} on "
            f"{_show_day(v['date'])}, by a vote of {_tally(v)}.")
    good = confirmed == r.confirmed
    if good and r.tally and r.tally not in ((v["yea"], v["nay"]), (yea, v["nay"])):
        good = False
        line += f" The recorded vote was {v['yea']}–{v['nay']}, not {r.tally[0]}–{r.tally[1]}."
    if good and r.year and v["date"] and int(v["date"][:4]) != r.year:
        good = False
    dp = _vote_fact(v, subject[:140], f"{'Confirmed' if confirmed else 'Not confirmed'}, {v['yea']}–{v['nay']}")
    return verdict(SUPPORTED if good else CONTRADICTED, line, [dp], caveats=caveats,
                   claimed=(f"{r.tally[0]}–{r.tally[1]}" if r.tally else ""))


# ------------------------------------------------------------------ laws a Congress enacted

async def _check_laws(r: Read, text: str, parsed) -> Optional[Verdict]:
    from services import bills
    n = await congress.law_count(r.congress)
    if n is None:
        return None
    if parsed is None:
        from claims.parser import parse_claim
        parsed = parse_claim(text)
    label = f"Public laws enacted by the {bills._ordinal(r.congress)} Congress ({bills._years_of(r.congress)})"
    dp = fact("CONGRESS", label, f"{n:,} public laws", f"https://www.congress.gov/public-laws/{bills._ordinal(r.congress)}-congress",
              basis="Congress.gov list of public laws", magnitude=float(n), family=UNKNOWN,
              year=1789 + 2 * (r.congress - 1) + 1)
    v = figure_check(parsed, r.figure, dp, text, UNKNOWN)
    if v is None:
        return None
    v.headline = f"The {bills._ordinal(r.congress)} Congress ({bills._years_of(r.congress)}) enacted {n:,} public laws."
    if r.congress >= bills.congress_of(date.today().year):
        v.caveats.append("This Congress is still in session, so the count is to date.")
    return v


# ------------------------------------------------------------------ entry

def topic(text: str) -> bool:
    """Whether a sentence is a claim about a recorded vote (for choosing which
    sentence of a passage to check). Reads the text only; fetches nothing."""
    from services import bills
    t = " ".join((text or "").split())
    ref = bills._ref(t)
    if read(t, ref.span if ref is not None and ref.level == "federal" else None) is not None:
        return True
    # A vote on a measure this module cannot name ("voted against the border
    # bill") is still a claim about a roll call, for a search to settle.
    voted = _VOTED.search(t)
    if not voted or not _CONGRESS_CUE.search(t):
        return False        # "Americans voted for Trump" is an election, not a roll call
    return bool(_party_read(t, voted.start()) or _member_before(t, voted.start()))


async def check(text: str, parsed=None) -> Optional[Verdict]:
    try:
        t = " ".join((text or "").split())
        loose = read(t)
        if loose is not None and loose.kind == "laws":
            return await _check_laws(loose, t, parsed)
        got = await _bill_votes(t)
        if got is not None:
            r, votes, subject = got
            return _judge_member(r, votes, subject) if r.kind == "member" else _judge_party(r, votes, subject)
        if loose is not None and (loose.kind == "confirm" or loose.nominee):
            return await _check_confirm(loose)
    except SourceUnavailable as e:
        logger.warning("vote check skipped for %r: %s", text[:80], e)
    except Exception:  # noqa: BLE001 — never fail a request over this check
        logger.exception("vote check failed for %r", text[:120])
    return None
