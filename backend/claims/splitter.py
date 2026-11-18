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

A page that says when it was published lends one more thing: the year of a
month it names without one. A news release of 6 November 2026 that says "in
October" means October 2026. That reading is used only where it is safe (see
`_page_period`), is written into the claim like the rest, and is held to more
when the text nearby names some other year.

What is left out, on purpose:
  * "in Texas 4.1% and in California 5.3%": the second half names no
    statistic of its own, so the sentence stays whole and the engine declines
    it as it always has. Carrying a statistic across clauses is a guess.
  * A place named in one clause is never lent to another clause that has its
    own subject ("unemployment in Texas is 4% and inflation is 3%").
"""
import re
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional, Tuple

from claims.parser import (
    _BARE_MONTH_RE, _MONTH_YEAR_RE, _MONTHS, _PRESENT_TENSE_RE, _extract_fiscal_year, _extract_years,
    named_quarter, parse_claim,
)
from evidence.units import PERCENT, UNKNOWN, USD, parse_quantity

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
    # The reading leans on something the sentence does not say: its period
    # came from the page's date while the text around it names another year,
    # or it names no place while the text before it is about another country
    # or a state. Such a claim is listed only when the official figure bears
    # the reading out.
    tentative: bool = False


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


# A clause that opens with one of these depends on another clause for its meaning.
_SUBORDINATE = re.compile(r"(?:when|which|who|whose|where|as|after|before|since|because|although|though|if|"
                          r"that|unless|until|once|whereby)\b", re.I)
_YEARLIKE = re.compile(r"^(?:1\d{3}|20\d{2}|2100)$")
_SCALE = re.compile(r"\b(?:hundred|thousand|million|billion|trillion|bn|tn|mn)\b|\dk\b", re.I)


def _states_a_value(parsed, text: str) -> bool:
    """Whether the figure the parser took from a sentence is really a value of
    the measure it names.

    A person who types one claim means its number. A sentence picked out of an
    article may not: "the Panic of 1837 brought unprecedented unemployment"
    names a measure and holds a number, and checking it as "unemployment was
    1,837%" would stamp a verdict on something nobody said. So a sentence
    counts only when its figure is written as that measure is written: a
    percent sign for a rate, a dollar sign or a scale word for money, and
    never a bare year or an ordinal ("10th-highest").
    """
    value, spec = parsed.claimed_value, parsed.metric
    if value is None or spec is None:
        return False
    raw = (value.raw or "").strip()
    number = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
    if not number:
        return False
    digits = number.group(0)
    written = parse_quantity(raw)
    unit = written.family if written is not None else UNKNOWN
    if re.search(rf"(?<![\d.,$]){re.escape(digits)}(?:st|nd|rd|th)\b", text, re.I):
        return False                                    # an ordinal, not a value
    if unit == UNKNOWN and _YEARLIKE.match(digits) and not _SCALE.search(raw):
        return False                                    # a year
    if spec.family == PERCENT:
        return unit == PERCENT
    if spec.family == USD:
        return unit == USD or bool(_SCALE.search(raw))
    return unit in (UNKNOWN, spec.family)


_NEAR = 12      # words


def _figure_sits_with_its_measure(parsed, text: str) -> bool:
    """Whether the figure is written near the words that name its measure.

    "When the central bank started raising rates to combat an outbreak of
    inflation in March 2022, the average rate on a 1-year CD was stuck at a
    miserly 0.15%" names inflation and holds a percentage, and the percentage
    is the CD rate. Nothing in grammar a rule can read says so; distance does.
    A measure and its own figure sit within a few words of each other.
    """
    spec, value = parsed.metric, parsed.claimed_value
    low = text.lower()
    number = re.search(r"\d[\d,]*(?:\.\d+)?", (value.raw or "")) if value is not None else None
    if spec is None or number is None:
        return True
    named = [m.span() for phrase in spec.phrases
             for m in re.finditer(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", low)]
    if not named:
        return True     # picked by a rule of its own, which reads the measure and the figure together
    figures = [m.span() for m in re.finditer(rf"(?<![\d.,]){re.escape(number.group(0))}(?![\d])", low)]
    for a, b in named:
        for c, d in figures:
            between = low[b:c] if b <= c else low[d:a]
            if len(between.split()) <= _NEAR:
                return True
    return not figures


def _is_statistic(text: str) -> bool:
    try:
        parsed = parse_claim(text)
    except Exception:  # noqa: BLE001 — the parser never raises; stay safe if it ever does
        return False
    return _states_a_value(parsed, text) and _figure_sits_with_its_measure(parsed, text)


def _kind(text: str) -> Optional[str]:
    """What sort of claim a sentence is, or None when there is nothing to check."""
    if _is_statistic(text):
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
    return _is_statistic(clause)


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


_LAST_MONTH = re.compile(r"\blast month\b", re.I)
_LAST_YEAR = re.compile(r"\blast year\b", re.I)
_QUARTER_WORD = re.compile(r"\b(?:quarter|quarterly|q[1-4])\b", re.I)


def _long_date(when: date) -> str:
    return f"{when.day} {_MONTH_NAMES[when.month - 1]} {when.year}"


@dataclass(frozen=True)
class PagePeriod:
    """The period a sentence names without its year, dated by its page."""
    year: int
    month: Optional[int]
    quarter: Optional[int]
    words: str            # "October 2026", "Q3 2026", "2025"


def _page_period(sentence: str, as_of: Optional[date]) -> Optional[PagePeriod]:
    """The one period a sentence names without its year, read against the day
    its page was published; None when it names none, or more than one, or the
    reading would be a guess.

      "in October", page of 6 November 2026               -> October 2026
      "in December", page of 9 January 2026               -> December 2025
      "last month", page of 6 November 2026               -> October 2026
      "last year", page of 6 November 2026                -> 2025
      "in the third quarter", page of 29 October 2026     -> Q3 2026
      "from April through June", page of 30 July 2026     -> Q2 2026

    Not read: the month or quarter the page itself came out in. An agency has
    not yet published a period that is still running, so "in November" on a
    page of 6 November is last year's November or a forecast, and which is
    not said.
    """
    if as_of is None or _extract_years(sentence) or _extract_fiscal_year(sentence) is not None:
        return None
    last_month, last_year = bool(_LAST_MONTH.search(sentence)), bool(_LAST_YEAR.search(sentence))
    quarter, _, where = named_quarter(sentence)
    if quarter is not None:
        # Months inside "July through September" are the quarter's, not a month of their own.
        rest = sentence[:where[0]] + " " + sentence[where[1]:]
        if last_month or last_year or _BARE_MONTH_RE.search(rest) or named_quarter(rest)[0] is not None:
            return None
        ends = quarter * 3
        if ends >= as_of.month and ends - 2 <= as_of.month:
            return None                                  # the quarter still running
        year = as_of.year if ends < as_of.month else as_of.year - 1
        return PagePeriod(year, None, quarter, f"Q{quarter} {year}")
    if _QUARTER_WORD.search(sentence):
        return None
    months = {_MONTHS[m.lower()] for m in _BARE_MONTH_RE.findall(sentence)}
    if len(months) + last_month + last_year != 1:
        return None
    if last_year:
        return PagePeriod(as_of.year - 1, None, None, str(as_of.year - 1))
    if last_month:
        month = as_of.month - 1 or 12
        year = as_of.year if as_of.month > 1 else as_of.year - 1
    else:
        month = next(iter(months))
        if month == as_of.month:
            return None
        year = as_of.year if month < as_of.month else as_of.year - 1
    return PagePeriod(year, month, None, f"{_MONTH_NAMES[month - 1]} {year}")


def _dated_by_page(text: str, period: PagePeriod) -> str:
    """`text` with the period's year written in where it was left out."""
    if period.quarter is not None:
        _, _, where = named_quarter(text)
        if where is None:
            return text
        named = text[where[0]:where[1]]
        joiner = " " if _MONTH_YEAR_RE.search(f"{named} {period.year}") and "quarter" not in named.lower() \
            and "period" not in named.lower() else " of "
        return f"{text[:where[1]]}{joiner}{period.year}{text[where[1]:]}"
    if _LAST_YEAR.search(text):
        return _LAST_YEAR.sub(f"in {period.year}", text)
    if _LAST_MONTH.search(text):
        return _LAST_MONTH.sub(f"in {period.words}", text)
    return _BARE_MONTH_RE.sub(lambda m: f"{m.group(0)} {period.year}", text)


def _split_sentence(text: str, a: int, b: int, as_of: Optional[date] = None) -> List[ClaimSpan]:
    sentence = text[a:b]
    from_page = _page_period(sentence, as_of)
    page_note = (f"period ({from_page.words}) from the page's date ({_long_date(as_of)})"
                 if from_page else "")
    parts = _clauses(sentence, a)
    own = [(s, e) for s, e in parts if _is_own_claim(text[s:e])]
    # "..., when GDP fell 3.7% following the oil crisis" hangs on the rest of
    # its sentence: cut loose, it would be checked as a claim nobody made.
    if any(_SUBORDINATE.match(text[s:e]) for s, e in own):
        own = []
    if len(own) < 2:
        kind = _kind(sentence)
        if not kind:
            return []
        if from_page and kind == STATISTIC:
            return [ClaimSpan(_dated_by_page(sentence, from_page), sentence, a, b, kind, [page_note])]
        return [ClaimSpan(sentence, sentence, a, b, kind)]

    # What the sentence says once for every clause in it.
    period = _shared_period(sentence) or (f"in {from_page.words}" if from_page else "")
    present = bool(_PRESENT_TENSE_RE.search(sentence)) and not _extract_years(sentence)
    # (The country is what every national figure is for already, and "the U.S.
    # Bureau of Labor Statistics reported" names an agency, not a place.)
    places = {g.name for g in (parse_claim(text[s:e]).geography for s, e in parts
                               if (s, e) not in own) if g is not None and g.level != "nation"}
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
        if from_page and _page_period(clause, as_of) == from_page:
            # The clause names the month itself; only its year is missing.
            claim = _dated_by_page(claim, from_page)
            inherited.append(page_note)
        elif parsed.year is None and not parsed.present:
            if period and from_page:
                claim += f" {period}"
                inherited.append(page_note)
            elif period:
                claim += f" {period}"
                inherited.append(f"period ({period[3:]}) from the rest of the sentence")
            elif present:
                claim += " now"
                inherited.append("read as the latest figure, as the sentence is in the present tense")
        spans.append(ClaimSpan(claim, clause, s, e, STATISTIC, inherited))
    return spans


def _one_figure_one_period(claim: str) -> bool:
    """Whether a statistic is the kind one official figure settles: one value
    for one period (or the two points of "from X in A to Y in B"), with no
    qualifier the series cannot honour and no period named only by an event.
    Anything else the engine would decline, which on a page is only noise."""
    parsed = parse_claim(claim)
    if parsed.structured_blocker or parsed.has_unresolved_period:
        return False
    # A page does not say when it was written: "unemployment is 3.5%" in a
    # story from 2019 is not a claim about this month. Only a stated period
    # can be checked.
    if parsed.year is None:
        return False
    if parsed.is_trend_claim:
        return len(parsed.all_years or []) == 2 and len(parsed.all_values or []) == 2
    return True


# In text nobody picked: a sentence about what may happen, or what someone
# wants to happen. "About 6.3 million people could lose eligibility by fiscal
# year 2029 if the proposed change takes effect" holds a figure and a year,
# and nothing that has been counted.
_SPECULATIVE = re.compile(r"\b(?:would|could|might|should|proposed|proposal|proposes|plans? to|aims? to|"
                          r"seeks? to|hopes? to|(?:is|are) set to|if (?:the|it|they|congress|lawmakers))\b", re.I)


def _what_it_says(claim: str) -> tuple:
    """The measure, place, period and figure a statistic comes down to."""
    p = parse_claim(claim)
    place = p.geography.name if p.geography is not None and p.geography.level != "nation" else ""
    return (p.metric.metric if p.metric else claim, place,
            p.year, p.month, p.quarter, tuple(round(v.magnitude, 6) for v in (p.all_values or [])))


def _firm_record(text: str) -> bool:
    """Whether a sentence about a record (a bill, an agency action, a count of
    crimes or deaths) says something a record can settle: it names the bill
    or the agency, or it states a figure. "That is starting to lift the cost
    of food" touches a topic and claims nothing that can be looked up."""
    from services import regulatory
    return regulatory.topic(text) in ("bill", "fda", "ftc") or _has_figure(text)


def _around(text: str, a: int, b: int, before_only: bool = False) -> str:
    """The paragraph a sentence sits in and the two before it."""
    start = a
    for _ in range(3):
        start = text.rfind("\n", 0, max(start - 1, 0)) + 1
        if start == 0:
            break
    if before_only:
        return text[start:a]
    end = text.find("\n", b)
    return text[start:end if end != -1 else len(text)]


def _names_another_year(text: str, a: int, b: int, years: Tuple[int, ...]) -> bool:
    """Whether the paragraph a sentence sits in, or the two before it, names
    a year other than `years`. A story that turned to 2022 two paragraphs up
    and then says "peaked in June" does not mean this June."""
    return any(y not in years for y in _extract_years(_around(text, a, b)))


def _names_another_place(text: str, a: int, b: int, claim: str) -> bool:
    """Whether a sentence that names no place of its own follows text about
    somewhere else. "In 2020, the economy contracted 13.4%" in a paragraph on
    Peru is Peru's economy; read alone it is the United States'."""
    from claims.geography import resolve_geography
    if resolve_geography(claim) is not None:
        return False            # it says where, the country included
    near = resolve_geography(_around(text, a, b, before_only=True))
    return near is not None and near.level != "nation"


def split_claims(text: str, max_claims: int = 12,
                 kinds: Tuple[str, ...] = (STATISTIC, RECORD, OTHER),
                 strict: bool = False, as_of: Optional[date] = None) -> Tuple[List[ClaimSpan], bool]:
    """The checkable claims in `text`, in order, and whether more were found
    than `max_claims` allows. `kinds` narrows what is kept. `strict` (a page
    nobody picked the sentences of) also drops statistics that one official
    figure cannot settle, so the few checks a request gets go to claims that
    can end in a verdict. `as_of` is the day the text was published, when
    that is known: it dates a month named without its year."""
    text = text or ""
    found: List[ClaimSpan] = []
    said: set = set()
    for a, b in _sentences(text):
        for span in _split_sentence(text, a, b, as_of):
            if span.kind not in kinds:
                continue
            if strict and _SPECULATIVE.search(span.claim):
                continue        # what would, could or is proposed to happen is not on record yet
            if strict and span.kind == STATISTIC and not _one_figure_one_period(span.claim):
                continue
            if strict and span.kind == RECORD and not _firm_record(span.claim):
                continue
            if strict and span.kind == STATISTIC:
                # A story states its main figure in the headline and again in
                # its first line. One check answers both.
                same = _what_it_says(span.claim)
                if same in said:
                    continue
                said.add(same)
            unsure = False
            if as_of is not None and any("page's date" in note for note in span.inherited):
                period = _page_period(text[a:b], as_of)
                unsure = bool(period and _names_another_year(text, a, b, (period.year,)))
            if strict and span.kind == STATISTIC and not unsure:
                unsure = _names_another_place(text, a, b, span.claim)
            if unsure:
                span = ClaimSpan(span.claim, span.span, span.start, span.end, span.kind,
                                 span.inherited, tentative=True)
            found.append(span)
    return found[:max_claims], len(found) > max_claims
