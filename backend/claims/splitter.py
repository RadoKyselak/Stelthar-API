"""A passage, cut into the separate claims in it. No model call.

The engine checks one claim. A highlighted paragraph, a pasted speech or a
page of an article holds several, and answering the whole of it with one
verdict is wrong whenever its claims disagree: "inflation is 3%, unemployment
is 4% and wages are up 5%" came back as a single "Contradicted", though one
part was close and one was never checked.

Two cuts, both by rule:

  1. Sentences. A full stop after "U.S.", "Jan." or an initial is not an end.
  2. Clauses, only when a sentence holds two or more that each name their own
     statistic and their own figure. Otherwise the sentence stays whole, so a
     comparison ("from 5.2% in 2021 to 3.6% in 2023") is still one claim.

A clause cut from a sentence can lose what the sentence said once for all of
them: the year, the place, the tense. Those are put back as plain words at
the end of the clause ("... in 2023"), so the text that is checked is the text
that is shown, and nothing is settled out of sight. `inherited` says what was
added.

What is left out, on purpose:
  * "in Texas 4.1% and in California 5.3%": the second half names no
    statistic of its own, so the sentence stays whole and the engine declines
    it as it always has. Carrying a statistic across clauses is a guess.
  * A place named in one clause is never lent to another clause that has its
    own subject ("unemployment in Texas is 4% and inflation is 3%").
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from claims.parser import (
    _MONTH_YEAR_RE, _PRESENT_TENSE_RE, _extract_fiscal_year, _extract_years, parse_claim,
)

STATISTIC = "statistic"   # names a series the free data covers
RECORD = "record"         # a bill, an FDA or FTC action, a crime or health figure
OTHER = "other"           # states a figure, but no series here is named

# A full stop after one of these is part of the word, not the end of a sentence.
_ABBREVIATIONS = frozenset("""
jan feb mar apr jun jul aug sep sept oct nov dec
mr mrs ms dr prof sen rep gov lt gen col capt sgt pres st mt ft
no nos vs v approx est avg inc co corp ltd jr sr fig sec
u.s u.k u.n d.c e.g i.e a.m p.m
""".split())

_END = re.compile(r"[.!?]+[\"”')\]]*\s+|\n+")
_STARTS_SENTENCE = re.compile(r"[\"“'(\[]*[A-Z0-9$]")
_LAST_WORD = re.compile(r"([A-Za-z][A-Za-z.]*)\.$")

# Where one clause ends and the next begins.
_CLAUSE = re.compile(
    r"\s*;\s+|\s*,\s+(?:and|but|while|whereas|yet)\s+|\s+(?:and|but|while|whereas)\s+|\s*,\s+",
    re.IGNORECASE)

_YEAR_ONLY = re.compile(r"^(?:19|20)\d\d$")
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December")


@dataclass(frozen=True)
class ClaimSpan:
    """One checkable claim and where it sits in the text it came from."""
    claim: str            # what is sent to the engine
    span: str             # exactly as written
    start: int            # offsets of `span` in the original text
    end: int
    kind: str             # STATISTIC | RECORD | OTHER
    inherited: List[str] = field(default_factory=list)


def _sentences(text: str) -> List[Tuple[int, int]]:
    """(start, end) of each sentence, trailing space and punctuation left out."""
    out: List[Tuple[int, int]] = []
    start = 0
    for m in _END.finditer(text):
        if "\n" not in m.group(0):
            before = text[start:m.start() + 1]
            word = _LAST_WORD.search(before)
            token = word.group(1).lower() if word else ""
            if token in _ABBREVIATIONS or (len(token) == 1 and token.isalpha()):
                continue
            if not _STARTS_SENTENCE.match(text[m.end():]):
                continue
        out.append((start, m.start()))
        start = m.end()
    out.append((start, len(text)))
    trimmed = []
    for a, b in out:
        while a < b and text[a].isspace():
            a += 1
        while b > a and (text[b - 1].isspace() or text[b - 1] in ".!?"):
            b -= 1
        if b > a:
            trimmed.append((a, b))
    return trimmed


def _has_figure(text: str) -> bool:
    """A number that is not just a year."""
    for n in re.findall(r"\d[\d,.]*", text):
        if not _YEAR_ONLY.match(n.rstrip(".,")):
            return True
    return False


def _kind(text: str) -> Optional[str]:
    """What sort of claim a sentence is, or None when there is nothing to check."""
    try:
        parsed = parse_claim(text)
    except Exception:  # noqa: BLE001 — the parser never raises; stay safe if it ever does
        parsed = None
    if parsed is not None and parsed.metric is not None and parsed.claimed_value is not None:
        return STATISTIC
    from services import derived, regulatory
    if derived.reads_as_computed(text):
        return STATISTIC
    if regulatory.topic(text):
        return RECORD
    if _has_figure(text) and len(text.split()) >= 4:
        return OTHER
    return None


def _clauses(text: str, base: int) -> List[Tuple[int, int]]:
    """(start, end) in the original text of each clause of one sentence."""
    out, start = [], 0
    for m in _CLAUSE.finditer(text):
        if m.start() > start:
            out.append((base + start, base + m.start()))
        start = m.end()
    if start < len(text):
        out.append((base + start, base + len(text)))
    return out


def _is_own_claim(clause: str) -> bool:
    parsed = parse_claim(clause)
    return parsed.metric is not None and parsed.claimed_value is not None


def _shared_period(sentence: str) -> str:
    """The one period a sentence names, as words to add to a clause: "in June
    2022", "in fiscal year 2023", "in 2023". Empty when it names none or more
    than one."""
    fiscal = _extract_fiscal_year(sentence)
    years = _extract_years(sentence)
    if fiscal is not None and len(years) <= 1:
        return f"in fiscal year {fiscal}"
    if len(years) != 1:
        return ""
    months = _MONTH_YEAR_RE.findall(sentence)
    if len(months) == 1:
        from claims.parser import _MONTHS
        number = _MONTHS.get(months[0][0].lower())
        if number:
            return f"in {_MONTH_NAMES[number - 1]} {years[0]}"
    if months:
        return ""
    return f"in {years[0]}"


def _split_sentence(text: str, a: int, b: int) -> List[ClaimSpan]:
    sentence = text[a:b]
    parts = _clauses(sentence, a)
    own = [(s, e) for s, e in parts if _is_own_claim(text[s:e])]
    if len(own) < 2:
        kind = _kind(sentence)
        return [ClaimSpan(sentence, sentence, a, b, kind)] if kind else []

    # What the sentence says once for every clause in it.
    period = _shared_period(sentence)
    present = bool(_PRESENT_TENSE_RE.search(sentence)) and not _extract_years(sentence)
    places = {g.name for g in (parse_claim(text[s:e]).geography for s, e in parts
                               if (s, e) not in own) if g is not None}
    place = next(iter(places)) if len(places) == 1 else ""

    spans: List[ClaimSpan] = []
    for s, e in parts:
        clause = text[s:e]
        if (s, e) not in own:
            if _has_figure(clause) and len(clause.split()) >= 4:
                spans.append(ClaimSpan(clause, clause, s, e, OTHER))
            continue
        parsed = parse_claim(clause)
        claim, inherited = clause, []
        if parsed.geography is None and place:
            claim += f" in {place}"
            inherited.append(f"place ({place}) from the rest of the sentence")
        if parsed.year is None and not parsed.present:
            if period:
                claim += f" {period}"
                inherited.append(f"period ({period[3:]}) from the rest of the sentence")
            elif present:
                claim += " now"
                inherited.append("read as the latest figure, as the sentence is in the present tense")
        spans.append(ClaimSpan(claim, clause, s, e, STATISTIC, inherited))
    return spans


def split_claims(text: str, max_claims: int = 12,
                 kinds: Tuple[str, ...] = (STATISTIC, RECORD, OTHER)) -> Tuple[List[ClaimSpan], bool]:
    """The checkable claims in `text`, in order, and whether more were found
    than `max_claims` allows. `kinds` narrows what is kept: a page scan keeps
    only what the free data can settle."""
    text = text or ""
    found: List[ClaimSpan] = []
    for a, b in _sentences(text):
        for span in _split_sentence(text, a, b):
            if span.kind in kinds:
                found.append(span)
    return found[:max_claims], len(found) > max_claims
