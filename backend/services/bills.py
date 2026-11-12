"""Claims about a bill's record: whether it passed, became law, was signed,
vetoed or failed, the recorded vote, the sponsor, and when.

  federal   "The Senate passed H.R. 1 51-50"
            "Biden signed the Inflation Reduction Act in 2022"
            Congress.gov's record (the Library of Congress)
  state     "Texas passed SB 17 in 2023"   "Florida House Bill 1557 was vetoed"
            the legislature's record, read through LegiScan

Only the record is checked, never what the bill does: "H.R. 1 cut Medicaid"
is a claim about its text, and anything with a figure beyond a vote or a
date is left to the rest of the pipeline. A bill missing from the record is
never read as "no such bill": the claim may name it differently.

Bill numbers restart every Congress (and every state session), so a number
is read with the Congress or year the claim names, else the current one, and
the answer says which bill it read.
"""
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, List, Optional, Sequence, Tuple

from claims.verdict import CONTRADICTED, SUPPORTED, Verdict
from config import logger
from evidence.excerpt import row as ex_row
from evidence.sources import SourceUnavailable, congress, legiscan
from services.regulatory import NEGATION, fact, verdict

ONLY_RECORD = "This checks the bill's legislative record, not what the bill does."

# ------------------------------------------------------------------ reading

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_DATE = re.compile(r"\b(" + "|".join(m.title() for m in _MONTHS) + r"|"
                   + "|".join(m[:3].title() for m in _MONTHS) + r")\.?\s+(\d{1,2}),?\s+((?:19|20)\d{2})\b")
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_ORDINAL = re.compile(r"\b(\d{2,3})(?:st|nd|rd|th)\s+Congress\b", re.I)

# Federal bills: H.R. 1, S. 5, H.J.Res. 7, HR 22. Case matters: "S." after
# "U.S." is not a bill, hence the look-behind.
_FED = re.compile(
    r"(?<![.\w])(H\.\s?J\.\s?Res\.|S\.\s?J\.\s?Res\.|H\.\s?Con\.\s?Res\.|S\.\s?Con\.\s?Res\.|"
    r"H\.\s?Res\.|S\.\s?Res\.|HJRes|SJRes|HConRes|SConRes|HRes|SRes|H\.\s?R\.|HR|S\.)\s?(\d{1,5})\b")
_FED_KIND = {"hjres": "hjres", "sjres": "sjres", "hconres": "hconres", "sconres": "sconres",
             "hres": "hres", "sres": "sres", "hr": "hr", "s": "s"}
_FED_SHOW = {"hr": "H.R.", "s": "S.", "hjres": "H.J.Res.", "sjres": "S.J.Res.", "hconres": "H.Con.Res.",
             "sconres": "S.Con.Res.", "hres": "H.Res.", "sres": "S.Res."}
_FED_PAGE = {"hr": "house-bill", "s": "senate-bill", "hjres": "house-joint-resolution",
             "sjres": "senate-joint-resolution", "hconres": "house-concurrent-resolution",
             "sconres": "senate-concurrent-resolution", "hres": "house-resolution", "sres": "senate-resolution"}
_LEGISCAN_US = {"HB": "hr", "SB": "s", "HJR": "hjres", "SJR": "sjres", "HCR": "hconres",
                "SCR": "sconres", "HR": "hres", "SR": "sres"}

# State bills: SB 17, HB 1557, AB 5, SF 1, LD 1619, or spelled out.
_STATE_BILL = re.compile(
    r"\b(SB|HB|AB|SF|HF|LB|LD|SJR|HJR|AJR|SCR|HCR|ACR|SR|HR|A|S|H)\s?-?\s?(\d{1,5})\b")
_SPELLED = re.compile(
    r"\b(Senate|House|Assembly|Legislative)\s+(Bill|File|Document|Joint Resolution|Concurrent Resolution|"
    r"Resolution)\s+(?:No\.\s*)?(\d{1,5})\b")
_SPELLED_PREFIX = {("Senate", "Bill"): "SB", ("House", "Bill"): "HB", ("Assembly", "Bill"): "AB",
                   ("Senate", "File"): "SF", ("House", "File"): "HF", ("Legislative", "Bill"): "LB",
                   ("Legislative", "Document"): "LD", ("Senate", "Joint Resolution"): "SJR",
                   ("House", "Joint Resolution"): "HJR", ("Assembly", "Joint Resolution"): "AJR",
                   ("Senate", "Concurrent Resolution"): "SCR", ("House", "Concurrent Resolution"): "HCR",
                   ("Assembly", "Concurrent Resolution"): "ACR", ("Senate", "Resolution"): "SR",
                   ("House", "Resolution"): "HR"}

_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
_STATE_RE = re.compile(r"\b(" + "|".join(sorted(_STATES, key=len, reverse=True)) + r")\b(?!\s*,?\s*D\.?C\.?)")
_FEDERAL_CUE = re.compile(r"\b(Congress|congressional|U\.S\.|federal|President|White House|Obama|Trump|Biden|"
                          r"Bush)\b")

# Well-known federal acts by name (Congress.gov has no title search). Each is
# confirmed against the bill's own titles before it is used.
_ACTS: Tuple[Tuple[str, int, int, str, str], ...] = (
    (r"\b(?:Patient Protection and )?Affordable Care Act\b|\bObamacare\b", re.I, 111, "hr 3590",
     "Patient Protection and Affordable Care Act"),
    (r"\bTax Cuts and Jobs Act\b", re.I, 115, "hr 1", "Tax Cuts and Jobs Act"),
    (r"\bFirst Step Act\b", re.I, 115, "s 756", "First Step Act"),
    (r"\bCARES Act\b|\bCoronavirus Aid, Relief, and Economic Security Act\b", 0, 116, "hr 748",
     "Coronavirus Aid, Relief, and Economic Security Act"),
    (r"\bAmerican Rescue Plan(?: Act)?\b", re.I, 117, "hr 1319", "American Rescue Plan Act"),
    (r"\bInfrastructure Investment and Jobs Act\b|\bbipartisan infrastructure (?:law|bill)\b", re.I, 117,
     "hr 3684", "Infrastructure Investment and Jobs Act"),
    (r"\bInflation Reduction Act\b", re.I, 117, "hr 5376", "Inflation Reduction Act"),
    (r"\bCHIPS(?: and Science)? Act\b", 0, 117, "hr 4346", "CHIPS and Science Act"),
    (r"\bRespect for Marriage Act\b", re.I, 117, "hr 8404", "Respect for Marriage Act"),
    (r"\bBipartisan Safer Communities Act\b", re.I, 117, "s 2938", "Bipartisan Safer Communities Act"),
    (r"\bPACT Act\b|\bHonoring our Promise to Address Comprehensive Toxics Act\b", 0, 117, "s 3373",
     "Honoring our Promise to Address Comprehensive Toxics Act"),
    (r"\bFiscal Responsibility Act\b", re.I, 118, "hr 3746", "Fiscal Responsibility Act"),
    (r"\bLaken Riley Act\b", re.I, 119, "s 5", "Laken Riley Act"),
    (r"\b(?:One )?Big Beautiful Bill(?: Act)?\b", re.I, 119, "hr 1", "One Big Beautiful Bill Act"),
    (r"\bGENIUS Act\b", 0, 119, "s 1582", "GENIUS Act"),
    (r"\bTAKE IT DOWN Act\b|\bTake It Down Act\b", 0, 119, "s 146", "TAKE IT DOWN Act"),
    (r"\bEpstein Files Transparency Act\b", re.I, 119, "hr 4405", "Epstein Files Transparency Act"),
    (r"\bSAVE Act\b|\bSafeguard American Voter Eligibility Act\b", 0, 119, "hr 22",
     "Safeguard American Voter Eligibility Act"),
)
_ACT_NAME = re.compile(r"\b((?:[A-Z][\w'’.-]*\s+)(?:(?:[A-Z][\w'’.-]*|of|and|the|for|to|in|on|a)\s+){0,9}Act)"
                       r"(?:\s+of\s+((?:19|20)\d{2}))?\b")

_SIGN = re.compile(r"\bsign(?:ed|s|ing)?\b", re.I)
_LAW = re.compile(r"\b(?:bec(?:a|o)me[s]?\s+(?:a\s+|public\s+)?law|enacted|(?:is|are)\s+(?:now\s+)?(?:the\s+)?law|"
                  r"into\s+law|law\s+of\s+the\s+land)\b", re.I)
_VETO = re.compile(r"\bveto(?:ed|es|ing)?\b", re.I)
_OVERRIDE = re.compile(r"\boverr(?:ode|ide|idden|iding)\b", re.I)
_PASS = re.compile(r"\b(?:pass(?:ed|es|ing)?|approv(?:ed|es|e)|adopt(?:ed|s)?|clear(?:ed|s)?)\b", re.I)
_FAIL = re.compile(r"\b(?:fail(?:ed|s)?|defeated|rejected|voted\s+down|killed)\b", re.I)
_DIED = re.compile(r"\b(?:died|dies|dead|stalled)\b", re.I)
_NAME = r"((?:Sen\.|Senator|Rep\.|Representative|Assemblymember|Assemblyman|Assemblywoman|Del\.|Delegate)?\s*" \
        r"[A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+){0,2})"
_SPONSOR_AFTER = re.compile(r"\b(co-?sponsored|sponsored|introduced|authored|filed|written)\s+by\s+" + _NAME)
_SPONSOR_BEFORE = re.compile(_NAME + r"(?:['’]s)?\s+(co-?sponsored|sponsored|introduced|authored|filed|wrote)\b")
_TALLY = re.compile(r"\b(\d{1,3})\s*(?:-|–|—|to)\s*(\d{1,3})\b")
_SIGNER = re.compile(r"\b(Obama|Trump|Biden|Bush)\b")
_PRESIDENTS = ((date(2001, 1, 20), "Bush", "George W. Bush"), (date(2009, 1, 20), "Obama", "Barack Obama"),
               (date(2017, 1, 20), "Trump", "Donald Trump"), (date(2021, 1, 20), "Biden", "Joe Biden"),
               (date(2025, 1, 20), "Trump", "Donald Trump"))

LOWER, UPPER, BOTH, ANY = "lower", "upper", "both", "any"


@dataclass
class Ref:
    level: str                 # "federal" or "state"
    kind: str = ""             # federal: hr, s, hjres...
    number: int = 0
    state: str = ""            # postal code
    prefix: str = ""           # state: SB, HB, AB...
    name: str = ""             # an act's name, as the claim gives it
    canonical: str = ""        # the name confirmed against the bill's titles
    congress: Optional[int] = None
    congress_from: str = ""    # "named", "year", "act", "current"
    span: Tuple[int, int] = (0, 0)


@dataclass
class Read:
    ref: Ref
    law: Optional[bool] = None       # became law (True), did not (False)
    signed: bool = False
    veto: Optional[bool] = None
    override: bool = False
    passed: Tuple[str, ...] = ()
    passed_neg: bool = False
    failed: bool = False
    died: bool = False
    tally: Optional[Tuple[int, int]] = None
    sponsor: str = ""
    cosponsor: bool = False
    signer: str = ""
    year: Optional[int] = None
    day: Optional[date] = None


def _today() -> date:
    return date.today()


def congress_of(year: int) -> int:
    return (year - 1789) // 2 + 1


def _years_of(c: int) -> str:
    start = 1789 + 2 * (c - 1)
    return f"{start}–{start + 1}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower()


def _words(name: str) -> List[str]:
    stop = {"the", "of", "and", "act", "a", "to", "for", "in", "on", "bill"}
    return [w for w in re.findall(r"[a-z0-9]+", _fold(name)) if w not in stop and not re.fullmatch(r"\d{4}", w)]


def _names_match(name: str, titles: Sequence[str]) -> bool:
    want = _words(name)
    if not want:
        return False
    have = set(re.findall(r"[a-z0-9]+", _fold(" ".join(titles))))
    return all(w in have for w in want)


def _negated_before(text: str, at: int) -> bool:
    return bool(NEGATION.search(text[max(0, at - 22):at]))


def _state_named(text: str) -> str:
    for m in _STATE_RE.finditer(text):
        name = m.group(1)
        if name == "Washington" and re.search(r"\bWashington\s*,?\s*D\.?C\b|\bin Washington\b", text) \
                and not re.search(r"\bWashington (?:state|State|Legislature|lawmakers)\b|\bWashington['’]s\b", text):
            continue
        return _STATES[name]
    return ""


def _ref(text: str) -> Optional[Ref]:
    state = _state_named(text)
    fed_cue = bool(_FEDERAL_CUE.search(text))
    feds = list(_FED.finditer(text))
    for m in feds:
        token = re.sub(r"[.\s]", "", m.group(1)).lower()
        kind = _FED_KIND.get(token)
        if kind is None:
            continue
        dotted = "." in m.group(1)
        if kind == "hr" and dotted:
            return Ref("federal", kind=kind, number=int(m.group(2)), span=m.span())
        if state and not fed_cue:
            break  # "S. 5" or "HR 5" in a state's legislature
        if kind == "s" and not (dotted or fed_cue):
            continue
        return Ref("federal", kind=kind, number=int(m.group(2)), span=m.span())
    if state:
        m = _SPELLED.search(text)
        if m:
            prefix = _SPELLED_PREFIX.get((m.group(1), m.group(2)))
            if prefix:
                return Ref("state", state=state, prefix=prefix, number=int(m.group(3)), span=m.span())
        m = _STATE_BILL.search(text)
        if m:
            return Ref("state", state=state, prefix=m.group(1), number=int(m.group(2)), span=m.span())
    for pattern, flags, c, bill, canonical in _ACTS:
        m = re.search(pattern, text, flags)
        if m:
            kind, number = bill.split()
            return Ref("federal", kind=kind, number=int(number), name=m.group(0), canonical=canonical,
                       congress=c, congress_from="act", span=m.span())
    m = _ACT_NAME.search(text)
    if m and legiscan.available():
        name = re.sub(r"^(?:The|the)\s+", "", m.group(1))
        if len(_words(name)) >= 1:
            return Ref("state" if state and not fed_cue else "federal", state=state, name=name, canonical=name,
                       span=m.span())
    return None


def read(text: str) -> Optional[Read]:
    """What the claim says about a bill's record, or None."""
    t = " ".join((text or "").split())
    if not t:
        return None
    ref = _ref(t)
    if ref is None:
        return None
    r = Read(ref=ref)
    # Strip the bill's own name and number so "House Bill 1557" or "Act of
    # 2022" are not read as a chamber or a date.
    rest = t[:ref.span[0]] + " BILL " + t[ref.span[1]:]
    rest = re.sub(r"\bWhite House\b", "", rest)
    rest = _ACT_NAME.sub(" BILL ", rest)

    m = _ORDINAL.search(rest)
    if m and ref.level == "federal" and ref.congress is None:
        ref.congress, ref.congress_from = int(m.group(1)), "named"
    rest_no_cong = _ORDINAL.sub(" ", rest)
    d = _DATE.search(rest_no_cong)
    if d:
        mon = next(i for i, x in enumerate(_MONTHS) if x.startswith(d.group(1).lower()[:3])) + 1
        try:
            r.day = date(int(d.group(3)), mon, int(d.group(2)))
        except ValueError:
            r.day = None
    years = [int(y) for y in _YEAR.findall(rest_no_cong)]
    r.year = r.day.year if r.day else (years[0] if years else None)

    # Anything numeric left over (a dollar figure, a count) is a claim about
    # the bill's content, which this check does not cover.
    scrub = _DATE.sub(" ", rest_no_cong)
    scrub = _YEAR.sub(" ", scrub)
    tally = _TALLY.search(scrub)
    if tally:
        a, b = int(tally.group(1)), int(tally.group(2))
        if a <= 435 and b <= 435 and a + b >= 3:
            r.tally = (a, b)
        scrub = scrub[:tally.start()] + scrub[tally.end():]
    if re.search(r"\d", scrub):
        return None

    s = _SPONSOR_AFTER.search(rest) or _SPONSOR_BEFORE.search(rest)
    if s:
        name, verb = (s.group(2), s.group(1)) if s.re is _SPONSOR_AFTER else (s.group(1), s.group(2))
        name = re.sub(r"^(?:Sen\.|Senator|Rep\.|Representative|Assemblymember|Assemblyman|Assemblywoman|Del\.|"
                      r"Delegate)\s*", "", name).strip()
        if name and name.split()[0] not in ("The", "the", "It", "This", "Congress", "BILL", "House", "Senate"):
            r.sponsor = name
            r.cosponsor = verb.lower().startswith("co")

    law_m = _LAW.search(rest)
    sign_m = _SIGN.search(rest)
    if _OVERRIDE.search(rest):
        r.override = True
        r.law = True
    if law_m:
        r.law = not _negated_before(rest, law_m.start())
    if sign_m:
        r.signed = True
        neg = _negated_before(rest, sign_m.start())
        if r.law is None or neg:
            r.law = not neg
    veto_m = _VETO.search(rest)
    if veto_m and not r.override:
        r.veto = not _negated_before(rest, veto_m.start())
    if (r.signed or r.veto is not None) and ref.level == "federal":
        sm = _SIGNER.search(rest)
        if sm:
            r.signer = sm.group(1)

    pass_rest = re.sub(r"\bpass(?:ed|es)?\s+into\s+law\b", " ", rest, flags=re.I)
    pass_m = _PASS.search(pass_rest)
    if pass_m:
        r.passed_neg = _negated_before(pass_rest, pass_m.start())
        r.passed = _chambers(pass_rest, ref.level)
    if _FAIL.search(rest):
        r.failed = True
    elif _DIED.search(rest):
        r.died = True

    if not (r.law is not None or r.veto is not None or r.passed or r.failed or r.died or r.sponsor
            or r.tally):
        return None
    return r


def _chambers(text: str, level: str) -> Tuple[str, ...]:
    out = []
    if re.search(r"\bboth (?:chambers|houses)\b", text, re.I):
        return (BOTH,)
    if re.search(r"\bCongress\b", text) and level == "federal":
        return (BOTH,)
    if re.search(r"\b(?:the )?(?:state )?(?:legislature|General Assembly|lawmakers)\b", text, re.I) \
            and level == "state" and not re.search(r"\b(House|Senate|Assembly)\b", text):
        return (BOTH,)
    if re.search(r"\b(House|Assembly)\b", text):
        out.append(LOWER)
    if re.search(r"\bSenate\b", text):
        out.append(UPPER)
    return tuple(out) or (ANY,)


# ------------------------------------------------------------------ records

@dataclass
class Event:
    what: str            # introduced, passed_lower, passed_upper, failed, signed, law, veto, override, unsigned
    day: Optional[date]
    text: str = ""
    tally: Optional[Tuple[int, int]] = None


@dataclass
class Record:
    label: str                       # "H.R. 1 (119th Congress)"
    title: str
    url: str
    source: str
    events: List[Event]
    law: bool
    law_name: str = ""
    latest: Tuple[Optional[date], str] = (None, "")
    sponsors: List[Tuple[str, str]] = field(default_factory=list)    # (shown, last name)
    cosponsors: Optional[Callable] = None
    over: bool = False
    ended: str = ""                  # "the 117th Congress ended" / "the 2023 session ended"
    lower: str = "House"
    executive: str = "the President"
    federal: bool = True
    tallies: List[Tuple[str, Event]] = field(default_factory=list)  # (chamber, vote)
    notes: List[str] = field(default_factory=list)

    def first(self, *what) -> Optional[Event]:
        hits = [e for e in self.events if e.what in what]
        return min(hits, key=lambda e: e.day or date.max) if hits else None

    def last(self, *what) -> Optional[Event]:
        hits = [e for e in self.events if e.what in what]
        return max(hits, key=lambda e: e.day or date.min) if hits else None


def _iso(s: str) -> Optional[date]:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def show_date(d: Optional[date]) -> str:
    return f"{_MONTHS[d.month - 1].title()} {d.day}, {d.year}" if d else "an unknown date"


def _tally_in(text: str) -> Optional[Tuple[int, int]]:
    m = re.search(r"(\d{1,3})\s*-\s*(\d{1,3})", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _federal_record(ref: Ref, bill: dict, acts: List[dict], cospons: Callable) -> Record:
    c, kind, n = ref.congress, ref.kind, ref.number
    events: List[Event] = []
    seen = set()

    def add(what, a, tally=None):
        d = _iso(a.get("actionDate"))
        key = (what, d, tally)
        if key not in seen:
            seen.add(key)
            events.append(Event(what, d, a.get("text") or "", tally))

    tallies = []
    for a in acts:
        t = a.get("text") or ""
        code = str(a.get("actionCode") or "")
        typ = a.get("type") or ""
        if code == "8000" or t.startswith("Passed/agreed to in House"):
            add("passed_lower", a, _tally_in(t))
        elif code == "17000" or t.startswith("Passed/agreed to in Senate"):
            add("passed_upper", a, _tally_in(t))
        elif code in ("9000", "18000") or t.startswith("Failed of passage/not agreed to in"):
            add("failed", a, _tally_in(t))
        elif typ == "ResolvingDifferences" and re.search(r"\bAgreed to\b|\bagreed to\b", t):
            if re.search(r"\bthe House agree\b|\bHouse agreed\b", t):
                add("passed_lower", a, _tally_in(t))
            elif re.search(r"\bthe Senate agree\b|\bSenate agreed\b", t):
                add("passed_upper", a, _tally_in(t))
        if t.startswith("Signed by President"):
            add("signed", a)
        elif t.startswith("Became Public Law") or t.startswith("Became Private Law"):
            add("law", a)
        elif re.search(r"\bVetoed by President\b|\bPocket Vetoed\b", t):
            add("veto", a)
        elif re.search(r"\bover(?: the President's)? veto\b|\bveto overrid", t, re.I):
            add("override", a)
    intro = _iso(bill.get("introducedDate"))
    if intro:
        events.append(Event("introduced", intro, "Introduced"))
    for e in events:
        if e.tally and e.what in ("passed_lower", "passed_upper", "failed"):
            chamber = LOWER if e.what == "passed_lower" else UPPER if e.what == "passed_upper" else \
                (LOWER if "House" in e.text else UPPER)
            tallies.append((chamber, e))
    laws = bill.get("laws") or []
    latest = bill.get("latestAction") or {}
    show = f"{_FED_SHOW[kind]} {n}"
    title = bill.get("title") or ""
    over = c < congress_of(_today().year)
    return Record(
        label=f"{show} ({_ordinal(c)} Congress)", title=title,
        url=f"https://www.congress.gov/bill/{_ordinal(c)}-congress/{_FED_PAGE[kind]}/{n}",
        source="CONGRESS", events=events, law=bool(laws),
        law_name=(f"{laws[0].get('type') or 'Public Law'} {laws[0].get('number')}" if laws else ""),
        latest=(_iso(latest.get("actionDate")), latest.get("text") or ""),
        sponsors=[(s.get("fullName") or "", _fold(s.get("lastName") or "")) for s in bill.get("sponsors") or []],
        cosponsors=cospons, over=over, ended=f"the {_ordinal(c)} Congress ended", tallies=tallies)


_SIGNED_STATE = re.compile(r"\b(signed by (?:the )?gov|approved by (?:the )?gov|governor signed|signed by governor|"
                           r"gov\w*\.? signed|chaptered)", re.I)
_UNSIGNED_STATE = re.compile(r"\bwithout (?:the )?(?:governor'?s )?signature\b|\bbecame law without\b", re.I)


def _state_record(ref: Ref, b: dict) -> Record:
    st = ref.state
    state_name = next(k for k, v in _STATES.items() if v == st)
    origin_upper = str(b.get("body") or "").upper().startswith("S")
    events: List[Event] = []
    for p in b.get("progress") or []:
        d, ev = _iso(p.get("date")), p.get("event")
        if ev == legiscan.INTRODUCED:
            events.append(Event("introduced", d, "Introduced"))
        elif ev == legiscan.ENGROSSED:
            events.append(Event("passed_upper" if origin_upper else "passed_lower", d,
                                "Passed the chamber where it was introduced"))
        elif ev == legiscan.ENROLLED:
            events.append(Event("passed_lower" if origin_upper else "passed_upper", d, "Passed both chambers"))
        elif ev in (legiscan.PASSED, legiscan.CHAPTERED):
            events.append(Event("law", d, "Became law"))
        elif ev == legiscan.VETOED:
            events.append(Event("veto", d, "Vetoed"))
        elif ev == legiscan.FAILED:
            events.append(Event("failed", d, "Failed"))
        elif ev == legiscan.OVERRIDE:
            events.append(Event("override", d, "Veto overridden"))
    for h in b.get("history") or []:
        a = h.get("action") or ""
        if _UNSIGNED_STATE.search(a):
            events.append(Event("unsigned", _iso(h.get("date")), a))
        elif _SIGNED_STATE.search(a):
            events.append(Event("signed", _iso(h.get("date")), a))
    tallies = []
    for v in b.get("votes") or []:
        desc = v.get("desc") or ""
        if re.search(r"committee", desc, re.I):
            continue
        chamber = UPPER if str(v.get("chamber") or "").upper().startswith("S") else LOWER
        tallies.append((chamber, Event("vote", _iso(v.get("date")), desc,
                                       (int(v.get("yea") or 0), int(v.get("nay") or 0)))))
    status = int(b.get("status") or 0)
    law = status == legiscan.PASSED or any(e.what == "law" for e in events)
    hist = b.get("history") or []
    latest = (_iso(hist[-1].get("date")), hist[-1].get("action") or "") if hist else \
        (_iso(b.get("status_date")), "")
    session = b.get("session") or {}
    end = int(session.get("year_end") or 0)
    over = bool(session.get("sine_die")) or (end and end < _today().year)
    sponsors = [(s.get("name") or "", _fold(s.get("last_name") or ""), int(s.get("sponsor_type_id") or 0))
                for s in b.get("sponsors") or []]
    primary = [(n, ln) for n, ln, t in sponsors if t in (0, 1)] or [(n, ln) for n, ln, _ in sponsors[:1]]
    co = [(n, ln) for n, ln, t in sponsors if t not in (0, 1)]

    async def cospons():
        return [{"fullName": n, "lastName": ln} for n, ln in co]

    lower = "Assembly" if st in ("CA", "NV", "NY", "WI", "NJ") else "House"
    year_txt = session.get("session_title") or (str(session.get("year_start")) if session else "")
    return Record(
        label=f"{state_name} {b.get('bill_number') or ref.prefix + str(ref.number)}"
              + (f" ({year_txt})" if year_txt else ""),
        title=b.get("title") or "", url=b.get("state_link") or b.get("url") or "",
        source=f"{state_name} Legislature", events=events, law=law,
        law_name="", latest=latest, sponsors=primary, cosponsors=cospons, over=bool(over),
        ended="the session ended", lower=lower, executive="the governor", federal=False, tallies=tallies,
        notes=[legiscan.ATTRIBUTION])


# ------------------------------------------------------------------ fetching

async def _federal(ref: Ref) -> Tuple[Optional[Record], List[str]]:
    caveats = []
    if ref.congress is None and ref.name and legiscan.available():
        rows = await legiscan.search("US", f'"{ref.name}"', 1)
        for row in rows[:3]:
            m = re.match(r"([A-Z]+)(\d+)$", str(row.get("bill_number") or ""))
            year = re.search(r"/(\d{4})$", str(row.get("url") or ""))
            if m and m.group(1) in _LEGISCAN_US and year:
                ref.kind, ref.number = _LEGISCAN_US[m.group(1)], int(m.group(2))
                ref.congress, ref.congress_from = congress_of(int(year.group(1))), "act"
                break
        if ref.congress is None:
            return None, []
    if ref.congress is None:
        ref.congress, ref.congress_from = congress_of(_today().year), "current"
    bill = await congress.bill(ref.congress, ref.kind, ref.number)
    if not bill:
        return None, []
    titles = await congress.titles(ref.congress, ref.kind, ref.number)
    if ref.canonical:
        if not _names_match(ref.canonical, [t["title"] for t in titles] + [bill.get("title") or ""]):
            logger.warning("bill name %r does not match %s %s %s", ref.canonical, ref.congress, ref.kind, ref.number)
            return None, []
    acts = await congress.actions(ref.congress, ref.kind, ref.number)
    c, kind, n = ref.congress, ref.kind, ref.number

    async def cospons():
        return await congress.cosponsors(c, kind, n)

    rec = _federal_record(ref, bill, acts, cospons)
    rec.title = _short_title(titles) or rec.title
    show = f"{_FED_SHOW[kind]} {n}"
    if ref.congress_from == "current":
        caveats.append(f"The claim names no Congress or year; this is {show} of the {_ordinal(c)} Congress "
                       f"({_years_of(c)}). Bill numbers restart every Congress.")
    elif ref.congress_from == "year":
        caveats.append(f"Bill numbers restart every Congress; this is {show} of the {_ordinal(c)} Congress "
                       f"({_years_of(c)}), the one in session in the year the claim names.")
    return rec, caveats


def _short_title(titles: List[dict]) -> str:
    """The name people use: the popular title, else the short title."""
    for prefix in ("Popular Title", "Short Title(s) as Enacted", "Short Titles as Enacted",
                   "Short Title(s) as Passed", "Short Title"):
        hit = next((t["title"] for t in titles if t.get("titleType", "").startswith(prefix)
                    and not t.get("titleType", "").endswith("portions of this bill")), "")
        if hit:
            return re.sub(r"\s+of\s+(?:19|20)\d{2}$", "", hit.strip().rstrip("."))
    return ""


def _norm_number(s: str) -> Tuple[str, int]:
    m = re.match(r"([A-Z]+)0*(\d+)$", re.sub(r"[^A-Z0-9]", "", (s or "").upper()))
    return (m.group(1), int(m.group(2))) if m else ("", -1)


async def _state(ref: Ref, year: Optional[int]) -> Tuple[Optional[Record], List[str]]:
    if not legiscan.available():
        return None, []
    caveats = []
    if ref.name:
        rows = await legiscan.search(ref.state, f'"{ref.name}"', year or 1)
        for row in rows[:3]:
            b = await legiscan.bill(row.get("bill_id"))
            if b and _names_match(ref.name, [b.get("title") or "", b.get("description") or ""]):
                return _state_record(ref, b), caveats
        return None, []
    sessions = await legiscan.sessions(ref.state)
    if year:
        cands = [s for s in sessions if int(s.get("year_start") or 0) <= year <= int(s.get("year_end") or 0)]
    else:
        latest = max((int(s.get("year_end") or 0) for s in sessions), default=0)
        cands = [s for s in sessions if int(s.get("year_end") or 0) >= latest - 1]
    cands.sort(key=lambda s: (int(s.get("special") or 0), -int(s.get("year_end") or 0)))
    want = (ref.prefix.upper(), ref.number)
    for s in cands[:4]:
        for row in await legiscan.master_list(s.get("session_id")):
            if _norm_number(row.get("number") or "") == want:
                b = await legiscan.bill(row.get("bill_id"))
                if not b:
                    continue
                if not year:
                    caveats.append(f"The claim names no year; this is {ref.prefix} {ref.number} of the "
                                   f"{s.get('session_title') or s.get('session_name') or 'latest'} session. "
                                   "Bill numbers restart every session.")
                return _state_record(ref, b), caveats
    return None, []


# ------------------------------------------------------------------ judging

def _president(d: Optional[date]) -> Tuple[str, str]:
    if d is None:
        return "", ""
    best = ("", "")
    for start, short, full in _PRESIDENTS:
        if d >= start:
            best = (short, full)
    return best


def _when_ok(r: Read, e: Optional[Event]) -> Optional[str]:
    """A mismatch between the claim's date and the event's, as a sentence."""
    if e is None or e.day is None:
        return None
    if r.day and r.day != e.day:
        return f"That happened on {show_date(e.day)}, not {show_date(r.day)}."
    if r.year and not r.day and r.year != e.day.year:
        return f"That happened on {show_date(e.day)}, not in {r.year}."
    return None


async def judge(r: Read, rec: Record) -> Optional[Tuple[bool, List[str], Optional[Event]]]:
    ok: List[str] = []
    bad: List[str] = []
    cited: Optional[Event] = None
    exe = rec.executive
    signed = rec.first("signed")
    law_ev = rec.first("law")
    veto = rec.last("veto")
    override = rec.first("override")
    unsigned = rec.first("unsigned")
    dated: Optional[Event] = None

    def status_now() -> str:
        if rec.over:
            return f"it did not become law before {rec.ended}"
        d, t = rec.latest
        return f"its latest action ({show_date(d)}) was: {t.rstrip('.')}" if t else "it has not become law"

    if r.law is not None:
        if r.law:
            if not rec.law:
                tail = f" {exe.capitalize()} vetoed it on {show_date(veto.day)}." if veto else ""
                bad.append(f"{rec.label} has not become law: {status_now()}.{tail}")
            elif r.signed and not signed and (override or unsigned):
                how = "over the veto" if override else f"without {exe}'s signature"
                ev = override or unsigned
                bad.append(f"{rec.label} became law {how} on {show_date(ev.day)}; it was not signed.")
            else:
                ev = signed or law_ev
                dated = cited = ev
                name = f" as {rec.law_name}" if rec.law_name else ""
                if signed:
                    ok.append(f"{rec.label} was signed into law{name} on {show_date(signed.day)}.")
                else:
                    ok.append(f"{rec.label} became law{name}" + (f" on {show_date(ev.day)}." if ev else "."))
                if r.signer and rec.federal and ev:
                    short, full = _president(ev.day)
                    if short and short != r.signer:
                        bad.append(f"It was signed by {full}, not {r.signer}.")
        else:
            if rec.law:
                ev = signed or law_ev
                cited = ev
                bad.append(f"{rec.label} did become law" + (f" on {show_date(ev.day)}." if ev else "."))
            else:
                ok.append(f"{rec.label} has not become law: {status_now()}.")

    if r.veto is not None:
        if r.veto:
            if veto:
                dated = dated or veto
                cited = cited or veto
                ok.append(f"{exe.capitalize()} vetoed {rec.label} on {show_date(veto.day)}.")
                if override:
                    ok.append(f"The veto was overridden on {show_date(override.day)}.")
                if r.signer and rec.federal:
                    short, full = _president(veto.day)
                    if short and short != r.signer:
                        bad.append(f"The veto was {full}'s, not {r.signer}'s.")
            else:
                more = f" It became law on {show_date((signed or law_ev).day)}." if rec.law and (signed or law_ev) \
                    else ""
                bad.append(f"The record of {rec.label} shows no veto.{more}")
        else:
            if veto:
                cited = cited or veto
                bad.append(f"{exe.capitalize()} did veto {rec.label}, on {show_date(veto.day)}.")
            else:
                ok.append(f"The record of {rec.label} shows no veto.")

    if r.passed and not r.law:
        want = r.passed
        need = [LOWER, UPPER] if BOTH in want else [c for c in want if c in (LOWER, UPPER)]
        names = {LOWER: rec.lower, UPPER: "Senate"}
        if ANY in want:
            ev = rec.first("passed_lower", "passed_upper")
            if rec.law and not ev:
                ev = law_ev
            if r.passed_neg:
                (bad if ev else ok).append(
                    f"{rec.label} passed on {show_date(ev.day)}." if ev else f"{rec.label} has not passed a floor vote.")
            elif ev:
                dated = dated or ev
                cited = cited or ev
                ok.append(f"{rec.label} passed on {show_date(ev.day)}.")
            else:
                bad.append(f"{rec.label} has not passed a floor vote: {status_now()}.")
        for ch in need:
            ev = rec.first("passed_lower" if ch == LOWER else "passed_upper")
            where = names[ch]
            if r.passed_neg:
                if ev:
                    bad.append(f"{rec.label} passed the {where} on {show_date(ev.day)}.")
                else:
                    ok.append(f"{rec.label} has not passed the {where}.")
            elif ev:
                dated = dated or ev
                cited = cited or ev
                vote = f" ({ev.tally[0]}–{ev.tally[1]})" if ev.tally else ""
                ok.append(f"{rec.label} passed the {where} on {show_date(ev.day)}{vote}.")
            else:
                bad.append(f"{rec.label} has not passed the {where}: {status_now()}.")

    if r.failed and r.law is None and not r.passed:
        fail = rec.first("failed")
        if fail:
            cited = cited or fail
            dated = dated or fail
            ok.append(f"{rec.label} failed on {show_date(fail.day)}.")
        elif rec.law:
            ev = signed or law_ev
            bad.append(f"{rec.label} did not fail: it became law" + (f" on {show_date(ev.day)}." if ev else "."))
        elif rec.over:
            ok.append(f"{rec.label} did not become law before {rec.ended}.")
    elif r.died and r.law is None and not r.passed:
        if rec.law:
            ev = signed or law_ev
            bad.append(f"{rec.label} became law" + (f" on {show_date(ev.day)}." if ev else "."))
        elif rec.over:
            ok.append(f"{rec.label} did not become law before {rec.ended}.")

    if r.tally:
        a, b = r.tally
        votes = sorted(rec.tallies, key=lambda cv: cv[1].day or date.min)
        if r.passed and ANY not in r.passed and BOTH not in r.passed:
            votes = [(c, e) for c, e in votes if c in r.passed]
        if votes:
            match = next((e for _, e in votes if e.tally == (a, b) or (r.failed and e.tally == (b, a))), None)
            if match:
                cited = cited or match
                ok.append(f"The recorded vote on {show_date(match.day)} was {match.tally[0]}–{match.tally[1]}.")
            else:
                shown = "; ".join(f"{e.tally[0]}–{e.tally[1]} ({rec.lower if c == LOWER else 'Senate'}, "
                                  f"{show_date(e.day)})" for c, e in votes[:3])
                bad.append(f"The recorded {'vote was' if len(votes) == 1 else 'votes were'} {shown}, "
                           f"not {a}–{b}.")

    if r.sponsor:
        last = _fold(r.sponsor.split()[-1])
        primary = [s for s in rec.sponsors if s[1] == last]
        shown = ", ".join(s[0] for s in rec.sponsors) or "not listed"
        if r.cosponsor:
            co = [c for c in (await rec.cosponsors() if rec.cosponsors else [])
                  if _fold(c.get("lastName") or "") == last]
            if co or primary:
                ok.append(f"{(co[0].get('fullName') if co else primary[0][0])} "
                          f"{'cosponsored' if co else 'sponsored'} {rec.label}.")
            else:
                bad.append(f"{r.sponsor} is not listed among the sponsors of {rec.label}; the sponsor was {shown}.")
        elif primary:
            ok.append(f"{primary[0][0]} sponsored {rec.label}.")
        else:
            co = [c for c in (await rec.cosponsors() if rec.cosponsors else [])
                  if _fold(c.get("lastName") or "") == last]
            if co:
                bad.append(f"{co[0].get('fullName')} was a cosponsor of {rec.label}; the sponsor was {shown}.")
            else:
                bad.append(f"The sponsor of {rec.label} was {shown}, not {r.sponsor}.")

    if not ok and not bad:
        return None
    if ok and not bad:
        miss = _when_ok(r, dated)
        if miss:  # what happened, then when: "...signed on August 16, 2022. That happened ..., not in 2023."
            return False, ok[:2] + [miss], cited
    return (not bad, (bad if bad else ok), cited)


def _official(rec: Record) -> str:
    signed, law_ev, veto = rec.first("signed"), rec.first("law"), rec.last("veto")
    if rec.law:
        ev = signed or law_ev
        name = rec.law_name or "law"
        return f"Became {name}" + (f" on {show_date(ev.day)}" if ev else "")
    if veto:
        return f"Vetoed {show_date(veto.day)}"
    if rec.over:
        return f"Did not become law ({rec.ended})"
    d, t = rec.latest
    return f"Latest action {show_date(d)}: {t[:140]}" if t else "Pending"


def _rows(rec: Record, cited: Optional[Event]):
    labels = {"introduced": "Introduced", "passed_lower": f"Passed {rec.lower}", "passed_upper": "Passed Senate",
              "failed": "Failed", "signed": f"Signed by {rec.executive}", "law": "Became law",
              "veto": f"Vetoed by {rec.executive}", "override": "Veto overridden",
              "unsigned": "Became law without signature"}
    out = []
    for e in sorted(rec.events, key=lambda e: e.day or date.min):
        if e.what not in labels:
            continue
        val = show_date(e.day) + (f" ({e.tally[0]}–{e.tally[1]})" if e.tally else "")
        if e.what == "law" and rec.law_name:
            val += f", {rec.law_name}"
        out.append(ex_row(labels[e.what], val, cited=(e is cited)))
    out = out[-8:]
    if cited is not None and cited.what == "vote" and cited.tally:
        out.append(ex_row(f"Recorded vote: {cited.text[:60]}".rstrip(": "),
                          f"{show_date(cited.day)} ({cited.tally[0]}\u2013{cited.tally[1]})", cited=True))
    if out and not any(x.get("cited") for x in out):
        out[-1]["cited"] = True  # the latest step stands for the record
    elif not out:
        out = [ex_row(rec.label, _official(rec), cited=True)]
    return out


async def check(text: str, parsed=None) -> Optional[Verdict]:
    r = read(text)
    if r is None:
        return None
    try:
        ref = r.ref
        if ref.level == "federal":
            if ref.congress is None and ref.congress_from == "" and r.year and not ref.name:
                ref.congress, ref.congress_from = congress_of(r.year), "year"
            rec, caveats = await _federal(ref)
        else:
            rec, caveats = await _state(ref, r.year)
        if rec is None:
            return None
        judged = await judge(r, rec)
        if judged is None:
            return None
        good, lines, cited = judged
        dp = fact(rec.source, f"{rec.label}: {rec.title[:120]}" if rec.title else rec.label,
                  _official(rec), rec.url,
                  year=(cited.day.year if cited and cited.day else None),
                  record_date=(cited.day.isoformat() if cited and cited.day else ""),
                  basis="Congress.gov bill record" if rec.federal else "state legislature bill record",
                  rows=_rows(rec, cited),
                  note=" ".join([ONLY_RECORD] + rec.notes))
        return verdict(SUPPORTED if good else CONTRADICTED, " ".join(lines[:3]), [dp],
                       caveats=caveats + [ONLY_RECORD] + rec.notes)
    except SourceUnavailable as e:
        logger.warning("bill check skipped for %r: %s", text[:80], e)
    except Exception:  # noqa: BLE001 — never fail a request over this check
        logger.exception("bill check failed for %r", text[:120])
    return None
