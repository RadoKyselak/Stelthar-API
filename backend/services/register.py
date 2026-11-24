"""Claims about executive orders and federal rules, read from the Federal
Register, where every one of them is published.

  an order by number   "Executive Order 14067 was signed by Biden"
  an order by subject  "Trump signed an executive order withdrawing the United
                        States from the World Health Organization"
  how many             "Biden signed 162 executive orders"
                       "Trump signed more than 200 executive orders in 2025"
  a rule               "The EPA finalized a rule limiting PFAS in drinking water in 2024"
                       "The FDA banned Red Dye No. 3 in food"

An order or rule is matched by the words of its title, never guessed: when
the Federal Register lists nothing whose title carries the claim's subject,
nothing is said. A published rule can later be blocked by a court or undone
by another rule, and the Federal Register does not say so on the rule itself,
so a matched rule is reported as published, with that caveat.
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Tuple

from claims.verdict import CONTRADICTED, MEDIUM, SUPPORTED, Verdict
from config import logger
from evidence.excerpt import row as ex_row
from evidence.sources import SourceUnavailable, federalregister as fr
from evidence.units import UNKNOWN
from services.regulatory import NEGATION, fact, figure_check, verdict

PUBLISHED_ONLY = ("This confirms what the Federal Register published. A court or a later rule can block "
                  "or undo a rule after it is published.")
LAG = "The Federal Register can take a few days to publish a newly signed order."

# (surname, Federal Register name, shown, terms as (first day, last day))
_PRESIDENTS = (
    ("Bush", "george-w-bush", "George W. Bush", ((date(2001, 1, 20), date(2009, 1, 20)),)),
    ("Obama", "barack-obama", "Barack Obama", ((date(2009, 1, 20), date(2017, 1, 20)),)),
    ("Trump", "donald-trump", "Donald Trump", ((date(2017, 1, 20), date(2021, 1, 20)),
                                               (date(2025, 1, 20), date(2029, 1, 20)))),
    ("Biden", "joe-biden", "Joe Biden", ((date(2021, 1, 20), date(2025, 1, 20)),)),
)
_SURNAME = re.compile(r"\b(Bush|Obama|Trump|Biden)\b")
_EO = r"executive\s+orders?"
_EO_NUMBER = re.compile(r"\b(?:Executive\s+Order|E\.?O\.?)\s*(?:No\.?\s*|#\s*)?(\d{4,5})\b", re.I)
_EO_COUNT = re.compile(
    r"\b(?:signed|issued|has signed|has issued|put out)\s+(?:(?:only|just|more than|fewer than|less than|over|"
    r"under|about|around|nearly|almost|at least|a record)\s+)?(?P<n>\d[\d,]*)\s+" + _EO + r"\b", re.I)
_EO_TOPIC = re.compile(
    r"\b(?:signed|issued|has signed|has issued)\s+an?\s+" + _EO + r"\s+"
    r"(?:(?:that|which|to|on|about|for|regarding|concerning|aimed at|directing|ordering)\s+)?(?P<topic>.+)$", re.I)
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_TERM = re.compile(r"\b(first|second)\s+term\b", re.I)
_HUNDRED = re.compile(r"\bfirst\s+(?:100|hundred)\s+days\b", re.I)
_DAY_ONE = re.compile(r"\b(?:on\s+)?(?:his\s+)?(?:first day|day one)\b", re.I)

_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "from", "by", "with", "at", "as", "that",
         "which", "its", "his", "their", "all", "any", "new", "no", "into", "it", "is", "was", "be", "us",
         "federal", "national", "rule", "rules", "final", "regulation", "regulations", "standard", "standards",
         "requirement", "requirements", "order", "executive", "president", "administration", "government",
         "would", "will", "has", "have", "had", "been", "this", "these", "those", "than", "more", "most"}
_SAME = {"dye": ("dye", "color"), "ban": ("ban", "prohibit", "revoke", "repeal"), "banning": ("ban", "prohibit", "revoke"),
         "limits": ("limit", "standard", "regulation"), "limiting": ("limit", "standard", "regulation"),
         "america": ("america", "united"), "american": ("america", "united"), "u.s.": ("united",),
         "withdrew": ("withdraw",), "pulled": ("withdraw",), "pulling": ("withdraw",)}


def _president(text: str):
    m = _SURNAME.search(text or "")
    return next((p for p in _PRESIDENTS if m and p[0] == m.group(1)), None)


def _president_named(slug_or_name: str):
    s = (slug_or_name or "").lower()
    return next((p for p in _PRESIDENTS if p[1] == s or p[0].lower() in s), None)


def _stem(w: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[:-len(suffix)]
    return w


def _words(phrase: str) -> List[str]:
    """The content words of a subject, for matching against a title."""
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z.']*|\d+", phrase or ""):
        w = w.lower().strip(".'")
        if w and w not in _STOP and not re.fullmatch(r"(?:19|20)\d{2}", w) and w not in out:
            out.append(w)
    return out


def _has(word: str, hay: set) -> bool:
    options = _SAME.get(word, (word,))
    return any(_stem(o) in hay for o in options)


def _coverage(topic_words: List[str], doc: dict) -> Tuple[float, int]:
    """Share of the subject's words in the document, and how many are in its title."""
    title = {_stem(w) for w in re.findall(r"[a-z]+|\d+", (doc.get("title") or "").lower())}
    body = title | {_stem(w) for w in re.findall(r"[a-z]+|\d+", (doc.get("abstract") or "").lower())}
    if not topic_words:
        return 0.0, 0
    in_title = sum(_has(w, title) for w in topic_words)
    return sum(_has(w, body) for w in topic_words) / len(topic_words), in_title


def _matches(topic_words: List[str], doc: dict) -> bool:
    share, in_title = _coverage(topic_words, doc)
    return len(topic_words) >= 2 and in_title >= 2 and share >= 0.6


def _day(iso: Optional[str]) -> str:
    try:
        d = date.fromisoformat(iso or "")
    except ValueError:
        return "an unknown date"
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _signer(doc: dict) -> str:
    p = _president_named(((doc.get("president") or {}).get("identifier")) or "")
    return p[2] if p else ((doc.get("president") or {}).get("name") or "the President")


def _order_fact(doc: dict):
    n = doc.get("executive_order_number")
    signed = doc.get("signing_date") or ""
    official = f"Executive Order {n}, signed by {_signer(doc)} on {_day(signed)}"
    return fact("FEDERAL REGISTER", f"Executive Order {n}: {(doc.get('title') or '')[:120]}", official,
                doc.get("html_url") or "https://www.federalregister.gov/presidential-documents/executive-orders",
                year=int(signed[:4]) if signed[:4].isdigit() else None, record_date=signed,
                basis="Federal Register, presidential documents",
                rows=[ex_row(f"Executive Order {n}", doc.get("title") or "", cited=True),
                      ex_row("Signed", f"{_day(signed)}, by {_signer(doc)}"),
                      ex_row("Published", _day(doc.get("publication_date")))])


# ------------------------------------------------------------------ executive orders

def _when_wrong(text: str, signed: str) -> Optional[str]:
    years = [int(y) for y in _YEAR.findall(text)]
    if years and signed[:4].isdigit() and int(signed[:4]) not in years:
        return f"It was signed in {signed[:4]}, not {years[0]}."
    return None


async def _order_by_number(text: str, number: int) -> Optional[Verdict]:
    doc = await fr.order_numbered(number)
    if doc is None:
        return None
    signed = doc.get("signing_date") or ""
    claimed = _president(text)
    said_signed = re.search(r"\b(?:signed|issued)\b", text, re.I)
    has_year = bool(_YEAR.search(_EO_NUMBER.sub(" ", text)))
    if not ((claimed and said_signed) or has_year):
        return None     # nothing about the order's record is claimed
    dp = _order_fact(doc)
    line = f"Executive Order {number}, “{doc.get('title')}”, was signed by {_signer(doc)} on {_day(signed)}."
    good = True
    if claimed and claimed[2] != _signer(doc):
        good = False
    wrong = _when_wrong(_EO_NUMBER.sub(" ", text), signed)
    if wrong:
        good = False
        line += " " + wrong
    if said_signed and NEGATION.search(text[max(0, said_signed.start() - 22):said_signed.start()]):
        good = not good
    return verdict(SUPPORTED if good else CONTRADICTED, line, [dp])


def _window(text: str, pres) -> Tuple[Optional[int], str, str, str]:
    """(year, since, until, words) for the period a count is claimed over."""
    terms = pres[3]
    t = _TERM.search(text)
    if _HUNDRED.search(text):
        term = terms[1] if (t and t.group(1).lower() == "second" and len(terms) > 1) else \
            terms[-1] if (len(terms) > 1 and not t and date.today() >= terms[-1][0]) else terms[0]
        start = term[0]
        return None, start.isoformat(), (start + timedelta(days=99)).isoformat(), \
            f"in the first 100 days of the term that began {_day(start.isoformat())}"
    if t:
        i = 0 if t.group(1).lower() == "first" else 1
        if i >= len(terms):
            return None, "", "", ""
        a, b = terms[i]
        return None, a.isoformat(), (b - timedelta(days=1)).isoformat() if b <= date.today() else "", \
            f"in his {t.group(1).lower()} term"
    years = [int(y) for y in _YEAR.findall(text)]
    if years:
        return years[0], "", "", f"in {years[0]}"
    return None, "", "", ""


async def _order_count(text: str, parsed, m) -> Optional[Verdict]:
    pres = _president(text)
    if pres is None:
        return None
    year, since, until, words = _window(text, pres)
    if _TERM.search(text) and not words:
        return None
    n = await fr.order_count(pres[1], year=year, since=since, until=until)
    if n is None:
        return None
    if parsed is None:
        from claims.parser import parse_claim
        parsed = parse_claim(text)
    scope = f" {words}" if words else ""
    official = f"{n:,} executive orders"
    dp = fact("FEDERAL REGISTER", f"Executive orders signed by {pres[2]}{scope}", official,
              f"https://www.federalregister.gov/presidential-documents/executive-orders/{pres[1]}"
              + (f"/{year}" if year else ""),
              basis="Federal Register, presidential documents", magnitude=float(n), family=UNKNOWN,
              year=year or date.today().year)
    v = figure_check(parsed, m.group("n"), dp, text, UNKNOWN)
    if v is None:
        return None
    v.headline = f"The Federal Register lists {n:,} executive orders signed by {pres[2]}{scope}."
    in_office = any(a <= date.today() < b for a, b in pres[3])
    if not words and len(pres[3]) > 1:
        v.caveats.append(f"The claim names no term or year; this counts both of {pres[2]}'s terms.")
    if in_office and not (year and year < date.today().year) and not (until and until < date.today().isoformat()):
        v.caveats.append("He is in office, so the count is to date. " + LAG)
    return v


async def _order_by_topic(text: str, m) -> Optional[Verdict]:
    pres = _president(text)
    topic = re.sub(r"\s+(?:in|during|on)\s+(?:(?:January|February|March|April|May|June|July|August|September|"
                   r"October|November|December)\s+)?(?:\d{1,2},?\s+)?(?:19|20)\d{2}\b.*$", "", m.group("topic"))
    topic = re.sub(r"\b(?:on|during)\s+(?:his\s+)?(?:first day|day one)\b.*$", "", topic, flags=re.I)
    words = _words(topic)
    if len(words) < 2:
        return None
    years = [int(y) for y in _YEAR.findall(text)]
    term = " ".join(words)
    docs = await fr.orders(term, president=pres[1] if pres else "")
    hit = next((d for d in docs if _matches(words, d)), None)
    if hit is None and pres is not None:
        # Nothing under the president named: was it another president's order?
        others = [d for d in await fr.orders(term) if _matches(words, d) and _coverage(words, d)[0] >= 0.9]
        other = next((d for d in others if _signer(d) != pres[2]), None)
        if other is None:
            return None
        dp = _order_fact(other)
        line = (f"The Federal Register lists Executive Order {other.get('executive_order_number')}, "
                f"“{other.get('title')}”, as signed by {_signer(other)} on {_day(other.get('signing_date'))}. "
                f"It lists no order with this subject signed by {pres[2]}.")
        return verdict(CONTRADICTED, line, [dp], confidence=MEDIUM)
    if hit is None:
        return None
    signed = hit.get("signing_date") or ""
    dp = _order_fact(hit)
    line = (f"{_signer(hit)} signed Executive Order {hit.get('executive_order_number')}, “{hit.get('title')}”, "
            f"on {_day(signed)}.")
    good = True
    if years and signed[:4].isdigit() and int(signed[:4]) not in years:
        # Another order on the subject may be the one meant: only a year with
        # no matching order contradicts the claim.
        same_year = next((d for d in docs if _matches(words, d)
                          and (d.get("signing_date") or "")[:4] == str(years[0])), None)
        if same_year is not None:
            return await _order_by_topic_doc(same_year)
        good = False
        line += f" That was in {signed[:4]}, not {years[0]}."
    if good and _DAY_ONE.search(text) and pres is not None:
        starts = {a.isoformat() for a, _ in pres[3]}
        if signed not in starts:
            good = False
            line += " That was not his first day in office."
    return verdict(SUPPORTED if good else CONTRADICTED, line, [dp],
                   caveats=["Matched by the order's title; the claim's description of what it does is not checked."])


async def _order_by_topic_doc(doc: dict) -> Verdict:
    line = (f"{_signer(doc)} signed Executive Order {doc.get('executive_order_number')}, “{doc.get('title')}”, "
            f"on {_day(doc.get('signing_date'))}.")
    return verdict(SUPPORTED, line, [_order_fact(doc)],
                   caveats=["Matched by the order's title; the claim's description of what it does is not checked."])


# ------------------------------------------------------------------ rules

# How a claim names an agency -> the Federal Register's name for it.
_AGENCIES = (
    (r"EPA|Environmental Protection Agency", "environmental-protection-agency", "EPA"),
    (r"FDA|Food and Drug Administration", "food-and-drug-administration", "FDA"),
    (r"USDA|Department of Agriculture|Agriculture Department", "agriculture-department", "USDA"),
    (r"FCC|Federal Communications Commission", "federal-communications-commission", "FCC"),
    (r"SEC|Securities and Exchange Commission", "securities-and-exchange-commission", "SEC"),
    (r"OSHA|Occupational Safety and Health Administration", "occupational-safety-and-health-administration", "OSHA"),
    (r"Department of Labor|Labor Department|DOL", "labor-department", "the Labor Department"),
    (r"CFPB|Consumer Financial Protection Bureau", "consumer-financial-protection-bureau", "CFPB"),
    (r"NHTSA|National Highway Traffic Safety Administration", "national-highway-traffic-safety-administration", "NHTSA"),
    (r"FAA|Federal Aviation Administration", "federal-aviation-administration", "FAA"),
    (r"Department of Transportation|Transportation Department|DOT", "transportation-department",
     "the Transportation Department"),
    (r"Department of Education|Education Department", "education-department", "the Education Department"),
    (r"CMS|Centers for Medicare (?:and|&) Medicaid Services", "centers-for-medicare-medicaid-services", "CMS"),
    (r"HHS|Department of Health and Human Services", "health-and-human-services-department", "HHS"),
    (r"IRS|Internal Revenue Service", "internal-revenue-service", "IRS"),
    (r"DHS|Department of Homeland Security", "homeland-security-department", "DHS"),
    (r"Department of Energy|Energy Department|DOE", "energy-department", "the Energy Department"),
    (r"HUD|Department of Housing and Urban Development", "housing-and-urban-development-department", "HUD"),
    (r"ATF|Bureau of Alcohol, Tobacco, Firearms,? and Explosives", "alcohol-tobacco-firearms-and-explosives-bureau", "ATF"),
    (r"DEA|Drug Enforcement Administration", "drug-enforcement-administration", "DEA"),
    (r"CPSC|Consumer Product Safety Commission", "consumer-product-safety-commission", "CPSC"),
    (r"NLRB|National Labor Relations Board", "national-labor-relations-board", "NLRB"),
)
_AGENCY = re.compile(r"\b(?:the\s+)?(?P<agency>" + "|".join(a[0] for a in _AGENCIES) + r")\b")
_RULE_VERB = re.compile(
    r"\b(?:has\s+|have\s+|had\s+)?(?P<verb>finali[sz]ed|issued|published|adopted|announced|approved|proposed|"
    r"banned|prohibited|outlawed)\s+(?:(?:an?|the|its|new|final|a new|a final|a proposed)\s+)*"
    r"(?P<noun>rules?|regulations?|standards?|bans?|limits?|requirements?)?\s*"
    r"(?:(?:that|which|to|on|for|of|about|regarding|requiring|setting)\s+)?(?P<topic>.+)$", re.I)


@dataclass
class RuleRead:
    slug: str
    agency: str
    proposed: bool
    ban: bool
    words: List[str]
    year: Optional[int]


def read_rule(text: str) -> Optional[RuleRead]:
    t = " ".join((text or "").split()).rstrip(".")
    a = _AGENCY.search(t)
    if not a:
        return None
    slug, shown = next((s, n) for p, s, n in _AGENCIES if re.fullmatch(p, a.group("agency")))
    m = _RULE_VERB.search(t, a.end())
    if not m or m.start() - a.end() > 12:
        return None
    verb = m.group("verb").lower()
    ban = verb in ("banned", "prohibited", "outlawed")
    if not ban and not m.group("noun"):
        return None     # "the FDA approved Ozempic", "the EPA announced a plan": not a rule claim
    if re.search(r"\bwill\b|\bplans? to\b|\bwants? to\b|\bconsidering\b", t[:m.start()], re.I):
        return None
    if NEGATION.search(t[max(0, m.start() - 22):m.start()]):
        return None
    topic = re.sub(r"\s+(?:in|during)\s+(?:(?:January|February|March|April|May|June|July|August|September|"
                   r"October|November|December)\s+)?(?:19|20)\d{2}\b.*$", "", m.group("topic"))
    words = _words(topic)
    if len(words) < 2:
        return None
    years = [int(y) for y in _YEAR.findall(t)]
    return RuleRead(slug=slug, agency=shown, proposed=verb == "proposed", ban=ban, words=words,
                    year=years[0] if years else None)


async def _rule(text: str) -> Optional[Verdict]:
    r = read_rule(text)
    if r is None:
        return None
    kind = fr.PROPOSED if r.proposed else fr.RULE
    docs = [d for d in await fr.rules(" ".join(r.words), r.slug, kind) if _matches(r.words, d)]
    docs = [d for d in docs if "correction" not in (d.get("action") or "").lower()
            and "withdraw" not in (d.get("action") or "").lower()]
    if not docs:
        return None
    hit = docs[0]
    if r.year:
        hit = next((d for d in docs if (d.get("publication_date") or "")[:4] == str(r.year)), hit)
    pub = hit.get("publication_date") or ""
    what = "a proposed rule" if r.proposed else "a final rule"
    line = f"{r.agency} published {what}, “{hit.get('title')}”, in the Federal Register on {_day(pub)}"
    line += f" (effective {_day(hit.get('effective_on'))})." if hit.get("effective_on") and not r.proposed else "."
    good = True
    if r.year and pub[:4].isdigit() and int(pub[:4]) != r.year:
        good = False
        line += f" That was in {pub[:4]}, not {r.year}."
    dp = fact("FEDERAL REGISTER", (hit.get("title") or "")[:140], f"{what.capitalize()} published {_day(pub)}",
              hit.get("html_url") or "https://www.federalregister.gov", year=int(pub[:4]) if pub[:4].isdigit() else None,
              record_date=pub, basis="Federal Register",
              rows=[ex_row(hit.get("action") or what.capitalize(), hit.get("title") or "", cited=True),
                    ex_row("Published", _day(pub)),
                    ex_row("Effective", _day(hit.get("effective_on")) if hit.get("effective_on") else ""),
                    ex_row("Summary", (hit.get("abstract") or "")[:400])],
              note=PUBLISHED_ONLY)
    caveats = [PUBLISHED_ONLY]
    if r.ban:
        caveats.insert(0, "Matched by the rule's title; read its summary for exactly what it bans and when.")
    return verdict(SUPPORTED if good else CONTRADICTED, line, [dp], caveats=caveats,
                   confidence=MEDIUM if r.ban else "High")


# ------------------------------------------------------------------ entry

def topic(text: str) -> bool:
    """Whether a sentence is a claim this module reads. Fetches nothing."""
    t = text or ""
    if re.search(_EO, t, re.I) and (_EO_NUMBER.search(t) or _EO_COUNT.search(t) or _EO_TOPIC.search(t.rstrip("."))):
        return True
    return read_rule(t) is not None


async def check(text: str, parsed=None) -> Optional[Verdict]:
    t = " ".join((text or "").split())
    try:
        if re.search(_EO, t, re.I) or _EO_NUMBER.search(t):
            num = _EO_NUMBER.search(t)
            if num:
                return await _order_by_number(t, int(num.group(1)))
            count = _EO_COUNT.search(t)
            if count:
                return await _order_count(t, parsed, count)
            subject = _EO_TOPIC.search(t.rstrip("."))
            if subject:
                return await _order_by_topic(t, subject)
            return None
        return await _rule(t)
    except SourceUnavailable as e:
        logger.warning("Federal Register check skipped for %r: %s", text[:80], e)
    except Exception:  # noqa: BLE001 — never fail a request over this check
        logger.exception("Federal Register check failed for %r", text[:120])
    return None
