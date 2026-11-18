"""Deterministic claim parsing — no LLM, no network, no cost.

Every Gemini call is scarce: the free tier allows a few hundred requests a day
and the old pipeline spent four to eight of them per claim on internal
deliberation (plan, critique, re-plan, synthesise, embed) before answering
anything. Parsing the common claim shapes here costs nothing and leaves the
model budget for the claims that genuinely need it.

The shapes this handles are the ones people actually write:

    "The unemployment rate in 2024 was 3.7%"
    "Inflation hit 9% in June 2022"
    "The national debt is over $33 trillion"
    "Median household income in Texas was about $73,000 in 2022"
    "GDP grew 2.5% last year"

Anything it cannot parse returns `metric=None`, which routes the claim to
grounded search. Guessing is not a goal — a confident wrong parse is worse than
a clean miss, because a miss still gets answered by the next tier.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from claims.geography import resolve_geography
from claims.metrics import BY_METRIC, VARIANTS, MetricSpec, match_metric, match_metrics
from evidence.datapoint import Geography
from evidence.units import Quantity, parse_quantity
import evidence.units as _units

# Comparators, longest first so "at least" beats "least".
_COMPARATORS = [
    ("greater than", (" more than ", " greater than ", " over ", " above ",
                      " exceeded ", " exceeds ", " higher than ", " north of ",
                      " passed ", " surpassed ", " topped ", " crossed ")),
    ("at least", (" at least ", " no less than ", " or more",)),
    ("less than", (" less than ", " under ", " below ", " fewer than ",
                   " lower than ", " beneath ")),
    ("at most", (" at most ", " no more than ", " or less",)),
]

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_FY_RE = re.compile(r"\b(?:fy|fiscal\s+year)\s*(\d{4})\b|\bfy(\d{2})\b", re.IGNORECASE)
_MONTH_YEAR_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{4})\b", re.IGNORECASE
)
# A month name after a preposition, capitalised ("in May", never "may").
_BARE_MONTH_RE = re.compile(
    r"\b(?:in|during|for|by|through|of)\s+(?:early\s+|late\s+|mid-?\s*)?"
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\b(?!\s*,?\s*(?:of\s+)?\d{4})")
# Bill designators and other numerals that look like years but are not.
_NON_YEAR_CONTEXT_RE = re.compile(
    r"(?:h\.\s?r\.|h\.\s?res\.|s\.\s?res\.|s\.|bill|section|§|act of)\s*\d{0,4}$",
    re.IGNORECASE,
)

_JOBS_CHANGE_RE = re.compile(
    r"\b(added|adds|add|created|creates|create|gained|gains|lost|lose|loses|shed|sheds|cut|"
    r"eliminated)\s+(?:(?:a|an)\s+(?:[a-z-]+\s+){0,2}|about\s+|nearly\s+|roughly\s+|around\s+|almost\s+|"
    r"more than\s+|over\s+|just over\s+|just under\s+|under\s+|some\s+)?[\d,.]+\s*"
    r"(?:million|thousand|k|m)?\s+(?:new\s+|net\s+)?jobs\b",
    re.IGNORECASE,
)

# The jobs report's own wording: "Total nonfarm payroll employment rose by
# 165,000 in October". The change in payrolls, as "added 165,000 jobs" is.
_PAYROLLS_CHANGE_RE = re.compile(
    r"\b(?:nonfarm\s+)?payroll(?:s\b|\s+employment\b|\s+jobs\b)\s+(?:[a-z]+\s+){0,2}?"
    r"(rose|increased|grew|climbed|expanded|edged up|were up|was up|fell|declined|dropped|decreased|"
    r"contracted|shrank|edged down|were down|was down)\s+by\s+"
    r"(?:about\s+|nearly\s+|roughly\s+|around\s+|almost\s+|more than\s+|over\s+|just over\s+|just under\s+)?"
    r"[\d,.]+\s*(?:million|thousand|k)?\b",
    re.IGNORECASE,
)
# Private payrolls are a part of the total, and no series here is that part.
_PRIVATE_PAYROLLS_RE = re.compile(r"\bprivate(?:[- ]sector)?\s+(?:nonfarm\s+)?payroll", re.IGNORECASE)
_PAYROLLS_FELL = frozenset(("fell", "declined", "dropped", "decreased", "contracted", "shrank", "edged down",
                            "were down", "was down"))

# "The number of people" is the population only when nothing narrows it. "The
# number of people marginally attached to the labor force" (1.6 million) is
# not a claim that 1.6 million people live in the country.
_PEOPLE_COUNT_RE = re.compile(
    r"\b(?i:number of people)\b(?!\s*$|\s*[,.;:]|\s+(?i:is|was|are|were|has|have|had|reached|hit|grew|rose|fell|"
    r"topped|passed|exceeds|exceeded|stands|stood|living|residing|who\s+live|who\s+reside)\b"
    r"|\s+(?i:in)\s+(?:(?i:the\s+)?(?i:u\.?s\.?|united states|america|country|nation|world|state|city)\b|[A-Z]))")
_POPULATION_WORD_RE = re.compile(r"\bpopulation\b|\bresidents\b|\bhow many people live\b", re.IGNORECASE)

# A figure counted in something no series here is counted in: "34.4 hours",
# "12 cents", "25 basis points". Naming a measure in the same sentence does not
# make such a figure a value of it.
_FOREIGN_UNIT = (r"hours?|minutes?|weeks?|days?|months?|years?|cents?|(?:basis|percentage)\s+points?|points?|times|"
                 r"miles?|gallons?|barrels?|tons?|pounds?|degrees?|seats?|votes?|acres?|square\s+(?:feet|miles)")


def _in_a_foreign_unit(raw: str, claimed: Optional[Quantity]) -> bool:
    if claimed is None or claimed.family != "unknown":
        return False
    number = re.search(r"\d[\d,]*(?:\.\d+)?", claimed.raw or "")
    if not number:
        return False
    return bool(re.search(rf"(?<![\d.,$]){re.escape(number.group(0))}\s*-?\s*(?:{_FOREIGN_UNIT})\b", raw, re.IGNORECASE))


_PRICES_ROSE_RE = re.compile(
    r"^\s*(?:u\.?s\.?\s+|american\s+|overall\s+|consumer\s+)?prices\s+"
    r"(?:rose|increased|went up|were up|are up|climbed|jumped|grew|fell|declined|dropped)\b",
    re.IGNORECASE,
)

# Change-rate series where a fall is a negative value.
_SIGNED_CHANGE_METRICS = frozenset({
    "real_gdp_growth", "inflation_rate", "core_inflation_rate", "producer_price_inflation",
})
_FELL_BY_RE = re.compile(
    r"\b(?:fell|fall|falls|shrank|shrink|shrinks|contracted|contract|contracts|declined|"
    r"declines|dropped|drops|decreased|decreases|went down)\s+(?:by\s+)?"
    # "fell at an annual rate of 0.6 percent" is as much a fall as "fell 0.6 percent".
    r"(?:at\s+an?\s+(?:seasonally\s+adjusted\s+)?(?:annual|annualized|annualised)\s+rate\s+of\s+)?"
    r"(?:about\s+|nearly\s+|roughly\s+|around\s+|almost\s+)?\d",
    re.IGNORECASE,
)

_PRESENT_TENSE_RE = re.compile(
    r"\b(is|are|currently|right now|today|now|as of today|at present)\b", re.IGNORECASE
)

# Temporal references that name a period without naming a year. Silently
# treating these as "latest" is how a true two-point claim about the debt got
# Contradicted: "$27.8 trillion when Biden took office" was compared against
# today's figure. If we cannot resolve the period, we must not answer as if we
# had.
_RELATIVE_PERIOD_RE = re.compile(
    r"\b(when\s+\w+\s+took office|took office|under (president|governor|the )?\w+|"
    r"since\s+\w+\s+(took office|became)|during (his|her|their|the)\s+\w+|"
    r"at the start of|by the end of|before the pandemic|since the pandemic)\b",
    re.IGNORECASE,
)


# A year that names an event, not the period a figure is for: "GDP fell 3.7%
# following the 1973 oil crisis" is not a claim about calendar 1973 (when GDP
# grew 5.6%), and reading it as one stamps "Contradicted" on a true sentence.
_EVENT_NOUNS = (r"crisis|crises|recession|crash|panic|pandemic|election|tax cuts?|stimulus|shutdown|war|attacks?|"
                r"bailout|embargo|downturn|collapse|bubble|depression|act|law|reforms?|strike|hurricane|earthquake|"
                # "According to a 2024 study": when it was written, not what it measured.
                r"study|studies|report|survey|poll|paper|analysis|review|audit|book|article|interview|speech|ruling")
_EVENT_YEAR_RE = re.compile(
    rf"\b(?:the|a|an|one)\s+((?:19|20)\d\d)(?:[-\u2013]\d{{2,4}})?\s+(?:[A-Za-z-]+\s+){{0,2}}(?:{_EVENT_NOUNS})\b"
    r"|\b(?:following|in the wake of|in the aftermath of|as a result of)\s+(?:the\s+)?(?:[A-Za-z-]+\s+){0,3}?"
    r"((?:19|20)\d\d)\b",
    re.IGNORECASE)


def _event_years(text: str) -> List[int]:
    """Years used as the name of an event rather than as a period."""
    out: List[int] = []
    for m in _EVENT_YEAR_RE.finditer(text):
        y = int(m.group(1) or m.group(2))
        if y not in out:
            out.append(y)
    return out


# ---------------------------------------------------------------------------
# Qualifiers the structured series cannot honour.
#
# Every structured series is a single total: all persons, all households, the
# whole year, current dollars. A claim about a subgroup, a quarter, a per-capita
# figure or an inflation-adjusted figure asserts something about a DIFFERENT
# number. Comparing it to the total produced confident wrong verdicts on true
# claims ("the child poverty rate was 13.7%" -> Contradicted vs 12.5% all ages;
# "GDP grew 4.9% in Q3 2023" -> Contradicted vs 2.9% annual). These claims are
# routed to grounded search instead, which can find the right series.
# ---------------------------------------------------------------------------
_NUM_UNIT_AHEAD = r"(?!\s*(?:million|billion|trillion|thousand|percent|%|k\b|bn\b|tn\b))"

_SUBGROUP_RE = re.compile(
    r"\b(?:black|white(?!\s+house)|hispanic|latin[oax]s?|latine|asian|african[- ]americans?|"
    r"native americans?|american indians?|indigenous|pacific islanders?|multiracial|"
    r"women|men|female|male|mothers|fathers|single (?:mothers|parents|women|men)|"
    r"children|child|kids|teens?|teenagers?|teenage|youth|young(?:er)?|prime[- ]age|working[- ]age|"
    r"college[- ](?:educated|graduates?)|graduates|dropouts|long[- ]term|full[- ]time|part[- ]time|"
    r"seniors|elderly|older (?:americans|adults|workers)|retirees|veterans|"
    r"immigrants?|foreign[- ]born|native[- ]born|non[- ]citizens?|"
    r"rural|urban|disabled|people with disabilities|households headed by|"
    r"married couples?|families with|respondents|transgender|lgbtq?\+?|gay|lesbian|renters|homeowners|"
    r"students|undocumented|(?:one|two|three|four|five|six|seven|\d)(?:-or-more)?-person households?)\b"
    r"|\b(?:aged?|ages)\s+\d{1,2}\b"
    # "over 65" is an age; "over 4.1%" is a threshold, so a decimal is not an age.
    r"|\b(?:under|over|above|below)\s+(?:the\s+age\s+of\s+)?\d{1,2}(?![.,]\d)\b" + _NUM_UNIT_AHEAD +
    r"|\b\d{1,2}\s*(?:to|-|through)\s*\d{1,2}[- ]year[- ]olds?\b"
    r"|\b\d{1,2}\s+(?:years?\s+)?(?:and|or)\s+(?:older|over)\b",
    re.IGNORECASE,
)

# The universe a metric is already defined over is not a subgroup of it.
_UNIVERSE_OK = {
    "bachelors_or_higher": re.compile(r"\b25\s+(?:years?\s+)?(?:and|or)\s+(?:older|over)\b|"
                                      r"\b(?:aged?|ages|over)\s+25\b", re.IGNORECASE),
    "unemployment_rate": re.compile(r"\b16\s+(?:years?\s+)?(?:and|or)\s+(?:older|over)\b", re.IGNORECASE),
    "labor_force_participation": re.compile(r"\b16\s+(?:years?\s+)?(?:and|or)\s+(?:older|over)\b", re.IGNORECASE),
}

_QUARTER_RE = re.compile(
    r"\bq[1-4]\b|\b(?:first|second|third|fourth|1st|2nd|3rd|4th)[- ]quarter\b|"
    r"\bquarterly\b|\bannuali[sz]ed\b|\bannual (?:rate|pace|clip)\b|\bsaar\b",
    re.IGNORECASE,
)
_PER_CAPITA_RE = re.compile(r"\bper\s+(?:capita|person|head|resident|worker|household)\b", re.IGNORECASE)
_REAL_DOLLARS_RE = re.compile(
    r"\breal\b(?!\s+estate)|\bchained\b|\binflation[- ]adjusted\b|\bconstant[- ]dollars?\b",
    re.IGNORECASE,
)
_NOMINAL_RE = re.compile(r"\bnominal\b|\bcurrent[- ]dollars?\b", re.IGNORECASE)


_SUPERLATIVE_RE = re.compile(
    r"\b(?:the\s+|a\s+|an\s+)?(?:(?:lowest|highest|biggest|largest|smallest|fastest|slowest|"
    r"steepest|sharpest)\s+(?:level\s+|rate\s+|reading\s+)?(?:in|since)\s+(?:over\s+|more than\s+|"
    r"nearly\s+|almost\s+|about\s+|roughly\s+)?\d{1,3}\s+years|"
    r"(?:lowest|highest|biggest|largest|smallest|fastest|slowest|steepest|sharpest)"
    r"\s+(?:level\s+|rate\s+|reading\s+)?since\s+(?:19|20)\d\d|"
    r"\d{1,3}[- ]year\s+(?:low|high|record|peak))\b",
    re.IGNORECASE,
)
_SPAN_RE = re.compile(r"\b\d{1,3}-(?:year|month|week|day)\b", re.IGNORECASE)

_AGE_BOUND_RE = re.compile(
    r"\b\d{1,2}\s*(?:to|-|through)\s*\d{1,2}[- ]?(?:year[- ]olds?|years?(?:\s+old)?)\b"
    r"|\b\d{1,2}\s+(?:years?\s+)?(?:and|or)\s+(?:older|over|above)\b"
    r"|\b(?:aged?|ages)\s+\d{1,2}\s*\+?(?:\s*(?:to|-)\s*\d{1,2})?"
    r"|\b\d{1,2}\s*\+\s*(?=\s*(?:year|adult|population|people))",
    re.IGNORECASE,
)


# Quarters as people write them: "Q2 2026", "2026 Q2", "2026Q2",
# "the second quarter of 2026".
_ORDINAL_Q = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
              "fourth": 4, "4th": 4}
_QUARTER_YEAR_RE = re.compile(
    r"\bq([1-4])\s*(?:of\s+)?['’]?(\d{4})\b"
    r"|\b(\d{4})\s*[-:]?\s*q([1-4])\b"
    r"|\b(first|second|third|fourth|1st|2nd|3rd|4th)[- ]quarter(?:\s+of)?\s+(\d{4})\b",
    re.IGNORECASE,
)
# A quarter named by its months: "from July through September 2026", "the
# July-September quarter". The news wires write it this way more often than
# "the third quarter".
_QUARTER_BY_MONTHS_RE = re.compile(
    r"\b(January|April|July|October|Jan|Apr|Jul|Oct)\.?\s*(?:-|\u2013|\u2014|through\b|thru\b|to\b)\s*"
    r"(March|June|September|December|Mar|Jun|Sept?|Dec)\b\.?"
    r"(?:\s+(?:quarter|period))?(?:\s+of)?(?:\s+((?:19|20)\d\d)\b)?",
    re.IGNORECASE,
)
_QUARTER_OF_MONTHS = {(1, 3): 1, (4, 6): 2, (7, 9): 3, (10, 12): 4}
_ORDINAL_QUARTER_RE = re.compile(r"\b(first|second|third|fourth|1st|2nd|3rd|4th)[- ]quarter\b", re.IGNORECASE)


def named_quarter(text: str):
    """(quarter, year or None, (start, end) of the words) for the quarter a
    claim names, with or without its year; (None, None, None) when it names
    none. "July through September" is the third quarter; "July through
    October" is not a quarter."""
    text = text or ""
    for m in _QUARTER_BY_MONTHS_RE.finditer(text):
        q = _QUARTER_OF_MONTHS.get((_MONTHS[m.group(1).lower()], _MONTHS[m.group(2).lower()]))
        if q:
            return q, (int(m.group(3)) if m.group(3) else None), m.span()
    q, year = _extract_quarter(text)
    if q is not None:
        return q, year, None
    m = _ORDINAL_QUARTER_RE.search(text)
    if m:
        return _ORDINAL_Q[m.group(1).lower()], None, m.span()
    return None, None, None


_QUARTER_TOKEN_RE = re.compile(r"\bq[1-4]\b|\b(\d{4})q[1-4]\b", re.IGNORECASE)

# Metrics that have a quarterly series wired up, and the phrasing that goes
# with quarterly figures ("at an annual rate") is then expected, not a blocker.
_QUARTERLY_METRICS = {"real_gdp_growth"}


def _extract_quarter(text: str):
    """(quarter, year) for an explicit quarter, else (None, None)."""
    m = _QUARTER_YEAR_RE.search(text or "")
    if not m:
        for mm in _QUARTER_BY_MONTHS_RE.finditer(text or ""):
            q = _QUARTER_OF_MONTHS.get((_MONTHS[mm.group(1).lower()], _MONTHS[mm.group(2).lower()]))
            if q and mm.group(3):
                return q, int(mm.group(3))
        return None, None
    if m.group(1):
        return int(m.group(1)), int(m.group(2))
    if m.group(3):
        return int(m.group(4)), int(m.group(3))
    return _ORDINAL_Q[m.group(5).lower()], int(m.group(6))


def _select_variant(text: str, spec: Optional[MetricSpec]) -> Optional[MetricSpec]:
    """Switch "unemployment rate" to "unemployment rate, Black" when the claim
    says so. Exactly one variant must match; two ("Black women") is a
    subgroup no series covers, and the blocker below declines it."""
    if spec is None:
        return None
    hits = [v for v in VARIANTS.get(spec.metric, [])
            if v.qualifier and re.search(v.qualifier, text, re.IGNORECASE)]
    return hits[0] if len(hits) == 1 else spec


# Things that are not the federal debt, though they say "debt".
_NOT_THE_DEBT_RE = re.compile(
    r"\binterest\s+(?:payments?\s+|costs?\s+|expenses?\s+|outlays?\s+|spending\s+)?"
    r"(?:on|for)\s+(?:the\s+|our\s+)?(?:national\s+|federal\s+|public\s+)?debt\b"
    r"|\b(?:debt\s+interest|net\s+interest)\b"
    r"|\bdebt\s+(?:ceiling|limit)\b"
    r"|\bheld\s+by\s+the\s+public\b|\bintragovernmental\b"
    r"|\b(?:student(?:\s+loan)?|credit[- ]card|household|consumer|medical|corporate|mortgage|"
    r"auto(?:\s+loan)?|car|state|city|municipal|private|personal|foreign|external)\s+debt\b"
    r"|\bdebt[- ]to[- ]gdp\b|\bdebt\s+(?:as\s+a\s+)?(?:share|percent(?:age)?)\s+of\b",
    re.IGNORECASE)
# "Tuition inflation", "inflation for childcare": one category of prices,
# not the all-items rate. Categories with their own series match those first.
_CATEGORY_INFLATION_RE = re.compile(
    r"\b(?!core\b|headline\b|overall\b|annual\b|consumer\b|cpi\b|price\b|the\b|"
    r"monthly\b|yearly\b|average\b|high\b|low\b|record\b|u\.?s\.?\b|us\b|american\b|"
    r"national\b|current\b|recent\b|runaway\b|rising\b|persistent\b|global\b|"
    r"beating\b|beat\b|outpacing\b|outpaced\b|above\b|below\b|than\b|with\b|of\b|to\b|"
    r"after\b|for\b|adjusted\b|adjusting\b|and\b|by\b|from\b|at\b|cooling\b|falling\b|"
    r"slowing\b|easing\b|sticky\b|stubborn\b|hot\b|higher\b|lower\b|soaring\b|"
    r"official\b|measured\b|year-over-year\b|12-month\b|month-over-month\b)"
    r"([a-z][a-z-]+)\s+inflation\b"
    r"|\binflation\s+(?:for|in|on)\s+(?!the\s+(?:u\.?s\b|united\b|nation\b|country\b|america))(?:the\s+)?(?!the\b|u\.?s\b|united\b|nation\b|country\b|america|\d|19|20|january|february|march|"
    r"april|may|june|july|august|september|october|november|december)([a-z][a-z-]+)",
    re.IGNORECASE)
_WAGE_RE = re.compile(r"\bwages?\b|\bpaychecks?\b|\bsalar(?:y|ies)\b", re.IGNORECASE)


# HUD's Area Median Income and the income bands set from it ("middle income",
# "80% of AMI", "a family of four") are not Census median household income:
# HUD adjusts for household size and sets one figure per metro area, so the
# two differ by tens of thousands of dollars. Neither a fixed series nor the
# Census dataset search can answer these; they go to the AI search with a hint.
_INCOME_BAND_RE = re.compile(
    r"\barea median income\b|\bAMI\b|\bincome (?:limits?|thresholds?|eligibility|brackets?|bands?|tiers?)\b"
    r"|\b(?:middle|moderate|low|very[- ]low|extremely[- ]low)[- ]income\b(?!\s+(?:countr|nation|econom))"
    r"|\bmiddle[- ]class\b|\b(?:family|household) of (?:one|two|three|four|five|six|seven|eight|[1-8])\b"
    r"|\b\d{2,3}\s?(?:%|percent) of (?:the )?(?:area )?median\b",
    re.IGNORECASE)
_INCOME_METRICS = ("median_household_income", "personal_income")
_JOBS_METRICS = ("jobs_added", "nonfarm_employment")
# The sectors the jobs report breaks employment into.
_SECTOR_RE = re.compile(
    r"\b(?:manufacturing|construction|health ?care|hospitals?|leisure and hospitality|hospitality|restaurants?|"
    r"retail(?: trade)?|wholesale(?: trade)?|mining|logging|oil and gas|professional and business services|"
    r"business services|financial activities|finance|information sector|tech(?:nology)? (?:sector|industry|jobs)|"
    r"transportation and warehousing|warehousing|utilities|private education|education and health services|"
    r"social assistance|government (?:employment|jobs|payrolls)|federal (?:government )?(?:employment|jobs|workers)|"
    r"state government|local government|temporary help|factory|factories|auto(?:motive)? (?:industry|sector|jobs)|"
    r"clean energy|energy (?:sector|industry|jobs)|coal)\b", re.IGNORECASE)
_SINCE_DATE_RE = re.compile(
    r"\bsince\s+(?:a\s+|the\s+|its\s+)?(?:[a-z-]+\s+){0,4}?(?:in\s+)?(?:(?:" + "|".join(_MONTHS) + r")\s+)?"
    r"(?:19|20)\d\d\b", re.IGNORECASE)


_MONTH_NAMES_RE = (r"january|february|march|april|may|june|july|august|september|october|november|december|"
                   r"jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec")
# A change counted in points is a change, never the level: "rose 0.1
# percentage point to 4.2%" claims 4.2%, and "rose 2 points" claims no level.
_POINT_CHANGE_RE = re.compile(
    r"(?<![\d.,$])\d[\d,]*(?:\.\d+)?\s*-?\s*(?:percentage[- ]points?|pp\b|basis[- ]points?|points?\b)|"
    r"\b(?:a|one|two|three|four|five|six|seven|eight|nine|half a|a tenth of a|a few tenths of a)\s+"
    r"(?:percentage[- ])?point\b", re.IGNORECASE)
_CHANGE_VERBS = (r"rose|risen|rises|fell|fallen|falls|dropped|drops|climbed|climbs|jumped|jumps|declined|declines|"
                 r"increased|increases|decreased|decreases|grew|grows|gained|gains|slipped|slips|dipped|dips|soared|"
                 r"surged|plunged|tumbled|ticked\s+(?:up|down)|edged\s+(?:up|down)|inched\s+(?:up|down)|"
                 r"went\s+(?:up|down)|shot\s+up|(?:is|was|were|are)\s+(?:up|down)")
_CHANGE_HEDGES = (r"(?:by\s+)?(?:(?:about|nearly|roughly|around|almost|just|only|more than|less than|over|under)\s+|"
                  r"(?:a|an)\s+(?:[a-z-]+\s+){0,2})*")
_NUMBER_WITH_UNIT = (r"[$]?\d[\d,]*(?:\.\d+)?\s*(?:trillion|billion|million|thousand|k\b)?\s*"
                     r"(?:%|percent\b|cents?\b|dollars?\b|jobs?\b|people\b)?")
# "increased by 339,000 to 4.8 million": the first figure is how much it
# moved, the second is where it stands. For a level, the second is the claim.
_CHANGE_THEN_LEVEL_RE = re.compile(
    rf"\b(?:{_CHANGE_VERBS})\s+{_CHANGE_HEDGES}({_NUMBER_WITH_UNIT})(?=[^.;]{{0,40}}?\bto\s+"
    rf"(?:(?:a|an)\s+(?:[a-z-]+\s+){{0,2}}|just\s+|about\s+|nearly\s+|roughly\s+)?[$]?\d)", re.IGNORECASE)
# "The unemployment rate rose 0.3%": how much a rate moved, with no level given.
_CHANGE_ONLY_RE = re.compile(
    rf"\b(?:{_CHANGE_VERBS})\s+{_CHANGE_HEDGES}(\d[\d,]*(?:\.\d+)?)\s*(?:%|percent\b)", re.IGNORECASE)
# "up 3.1% from October 2025": the date is what the change is measured from,
# not the period the figure is for.
_BASE_PERIOD_RE = re.compile(
    rf"\b(?:from|compared\s+(?:with|to)|versus|vs\.?|than\s+in|than)\s+(?:(?:{_MONTH_NAMES_RE})\.?\s+)?"
    r"((?:19|20)\d\d)\b(?!\s*(?:to|through|-|\u2013)\s*(?:19|20)\d\d)", re.IGNORECASE)
# A change over one month, where the series is the change over twelve.
_ONE_MONTH_CHANGE_RE = re.compile(
    r"\bmonth[- ](?:over|to)[- ]month\b|\bover the month\b|\bfrom (?:the )?(?:previous|prior|preceding) month\b"
    r"|\b(?:from|than) (?:the )?month before\b|\bon a monthly basis\b"
    r"|\b(?:monthly|one-month)\s+(?:gain|increase|rise|change|jump|decline|drop|basis|rate)\b"
    rf"|\bfrom\s+(?:{_MONTH_NAMES_RE})\b(?!\.?\s*,?\s*(?:19|20)\d\d)", re.IGNORECASE)


# A level that is said to have moved by an amount, in the forms a rate's
# "rose 0.3%" does not cover: "added $1 trillion to the national debt", "an
# increase in the national debt of $1,017 billion", "a $500 billion increase
# in defense spending", "the deficit rose by $300 billion".
_AMOUNT = r"[$]?(\d[\d,]*(?:\.\d+)?)"
_MOVED_BY_RE = re.compile(
    rf"\b(?:{_CHANGE_VERBS})\s+{_CHANGE_HEDGES}{_AMOUNT}"
    rf"|\b(?:increase|rise|jump|gain|growth|decline|drop|decrease|reduction|cut|fall)\s+(?:in|to)\b[^.;]{{0,50}}?"
    rf"\bof\s+(?:about\s+|nearly\s+|roughly\s+|more than\s+|over\s+)?{_AMOUNT}"
    rf"|{_AMOUNT}\s*(?:trillion|billion|million|thousand|bn|tn|mn)?[- ]+(?:dollar\s+)?"
    rf"(?:increase|rise|jump|hike|boost|gain|decline|drop|decrease|reduction|cut)\b"
    rf"|\badd(?:ed|s|ing)?\s+(?:about\s+|nearly\s+|roughly\s+|more than\s+|over\s+)?{_AMOUNT}"
    rf"\s*(?:trillion|billion|million|thousand|bn|tn|mn)?\s+(?:more\s+)?(?:in\s+[a-z ]{{0,20}}?)?to\b",
    re.IGNORECASE)
# "The Fed's 2% inflation target", "was forecast to average 11.5%", "economists
# had expected 90,000": a figure that was aimed at or foreseen, not published.
_NOT_A_PUBLISHED_FIGURE_RE = re.compile(
    r"\btarget(?:s|ed)?\b|\bgoal\b|\bforecast(?:s|ed|ing)?\b|\bproject(?:ed|s|ion|ions)\b|"
    r"\bpredict(?:ed|s|ion|ions)\b|\bexpect(?:ed|s|ing|ation|ations)?\b|\bon track to\b|"
    # "an estimated $15.6 trillion for 2012 based on activity during the first two quarters"
    r"\bestimated?\b.{0,60}?\bbased on\b", re.IGNORECASE)
# What would happen, or what someone wants to happen: "if participation were
# the same, unemployment would be 11%", "raising the minimum wage to $10.10
# would lift a million people out of poverty". Real fact-checked claims are
# full of these, and each was held against today's figure.
_HYPOTHETICAL_RE = re.compile(
    r"\b(?:would|could|might)\b|\bif\b"
    r"|\b(?:raising|increasing|lowering|cutting|boosting|hiking|reducing|expanding|to raise|to increase|to lower|"
    r"to cut|to reduce)\s+(?:the\s+)?[a-z ,'-]{0,40}?\bto\s+(?:about\s+|at least\s+)?[$]?\d", re.IGNORECASE)
# "Reduced the number of uninsured by 10 percent": an amount of change, however the verb is put.
_BY_AN_AMOUNT_RE = re.compile(
    r"\bby\s+(?:about\s+|nearly\s+|roughly\s+|almost\s+|more than\s+|over\s+|at least\s+)?[$]?(\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE)
# GDP as what something else is measured against: "debt to GDP rose to 99%".
_AGAINST_GDP_RE = re.compile(
    r"\bto[- ](?:the\s+)?(?:u\.?s\.?\s+)?gdp\b|\b(?:%|percent|share|percentage|proportion|fraction)\s+of\s+"
    r"(?:the\s+)?(?:u\.?s\.?\s+)?gdp\b|\b(?:ratio|relative)\b[^.;]{0,40}\bgdp\b", re.IGNORECASE)
_OTHER_UNEMPLOYMENT_RE = re.compile(
    r"\bnatural rate\b|\bnairu\b|\bu-?6\b|\bunderemploy\w*|\binsured unemployment\b|\bstructural unemployment\b",
    re.IGNORECASE)
_OTHER_POVERTY_RE = re.compile(
    r"\b(?:supplemental|full-income|deep|extreme|absolute|relative|anchored)\s+poverty\b|\bspm\b", re.IGNORECASE)
# A period named only by comparison with now.
_BY_COMPARISON_RE = re.compile(
    r"\b(?:this|the same|that same)\s+(?:time|point|period|month|quarter|week)\s+(?:in|of|last|a year)\b"
    r"|\bat this point (?:in|last)\b", re.IGNORECASE)


def _is_a_change(spec: MetricSpec) -> bool:
    """Whether the series itself is a change (growth, inflation, jobs added)
    rather than a level (a rate, a price, a count)."""
    return (spec.metric in _SIGNED_CHANGE_METRICS or spec.bls_kind in ("cpi_yoy", "change")
            or "growth" in spec.metric or "inflation" in spec.metric)


def income_band(text: str) -> str:
    """The phrase that makes a claim about HUD income limits or an income band, or ""."""
    m = _INCOME_BAND_RE.search(text or "")
    return m.group(0).strip() if m else ""


def structured_blocker(text: str, spec: Optional[MetricSpec], quarter: Optional[int] = None,
                       claimed: Optional[Quantity] = None) -> str:
    """Why the matched series cannot answer this claim, or "" if it can."""
    if spec is None or not text:
        return ""
    # "Up 3.1% from October 2025": the one date in the claim is the date the
    # change is counted from. Read as the period, it checks the wrong month.
    base = _BASE_PERIOD_RE.search(text)
    if base and _extract_years(text) == [int(base.group(1))]:
        return ("the claim compares with an earlier date; the period its figure is for is not stated")
    if spec.bls_kind == "cpi_yoy" and _ONE_MONTH_CHANGE_RE.search(text):
        return "the claim is about the change over one month; the series is the change over 12 months"
    if _NOT_A_PUBLISHED_FIGURE_RE.search(text):
        return "the claim is about a target, a forecast or an expectation, not a published figure"
    if _HYPOTHETICAL_RE.search(text):
        return "the claim is about what would happen or is proposed, not a published figure"
    if _BY_COMPARISON_RE.search(text):
        return "the claim names its period only by comparison (\u201cthis time in\u201d); the period is not stated"
    if spec.metric in ("gdp", "real_gdp_growth") and _AGAINST_GDP_RE.search(text):
        return "the claim measures something against GDP; it is not a figure for GDP itself"
    if spec.metric.startswith("unemployment_rate") and _OTHER_UNEMPLOYMENT_RE.search(text):
        return "the claim is about a different measure of unemployment than the headline rate"
    if spec.metric == "poverty_rate" and _OTHER_POVERTY_RE.search(text):
        return "the claim is about a different measure of poverty than the official rate"
    number = re.search(r"\d[\d,]*(?:\.\d+)?", (claimed.raw or "")) if claimed is not None else None
    if number and not _is_a_change(spec) and not _CHANGE_THEN_LEVEL_RE.search(text) \
            and any(m.group(1) == number.group(0) for m in _BY_AN_AMOUNT_RE.finditer(text)):
        return "the claim says how much the figure changed, not what it was"
    if number and not _is_a_change(spec):
        # "Added $1 trillion to the national debt" is not a claim that the debt is $1 trillion.
        for m in _MOVED_BY_RE.finditer(text):
            moved = next((g for g in m.groups() if g), "")
            after = text[m.end():m.end() + 12]
            if moved == number.group(0) and not re.match(r"\s*(?:%|percent)", after) \
                    and not _CHANGE_THEN_LEVEL_RE.search(text):
                return "the claim says how much the figure changed, not what it was"
    if number and _is_a_change(spec) and spec.family == _units.PERCENT:
        # "Dealer markups accounted for 35% of new vehicle inflation": a share of it.
        share = re.search(rf"(?<![\d.,]){re.escape(number.group(0))}\s*(?:%|percent)\s+(?:to\s+\d[\d.]*\s*(?:%|percent)\s+)?"
                          r"of\s+(?:the\s+|all\s+|total\s+|overall\s+)?(?:[a-z-]+\s+){0,3}?"
                          r"(?:inflation|growth|increase|rise|gains?)\b", text, re.IGNORECASE)
        if share:
            return "the claim's figure is a share of the change, not the change itself"
    # "The unemployment rate rose 0.3%" says how far a rate moved. Checked as
    # the rate itself, it would be held against 4%.
    if spec.family == _units.PERCENT and not _is_a_change(spec) and claimed is not None:
        number = re.search(r"\d[\d,]*(?:\.\d+)?", claimed.raw or "")
        for m in _CHANGE_ONLY_RE.finditer(text):
            if number and m.group(1) == number.group(0):
                return "the claim says how much the rate changed, not what it was"
    band = income_band(text) if spec.metric in _INCOME_METRICS else ""
    if band:
        return (f"the claim is about an income band or HUD's area median income (\u201c{band}\u201d), "
                f"which HUD sets per metro area and household size; it is not Census median household income")
    # A subgroup mention is fine when it overlaps the metric's own universe
    # (its qualifier, or a defining age bound such as "25 and older").
    allowed = []
    for rx in (spec.qualifier and re.compile(spec.qualifier, re.IGNORECASE),
               _UNIVERSE_OK.get(spec.metric), _UNIVERSE_OK.get(spec.parent or "")):
        if rx:
            allowed.extend(mm.span() for mm in rx.finditer(text))
    for m in _SUBGROUP_RE.finditer(text):
        covered = any(m.start() < e and s0 < m.end() for s0, e in allowed)
        if not covered:
            return f"the claim is about a subgroup (\u201c{m.group(0).strip()}\u201d), not the total the series measures"
    quarterly_ok = spec.metric in _QUARTERLY_METRICS and quarter is not None
    if (_QUARTER_RE.search(text) or named_quarter(text)[0] is not None) and not quarterly_ok:
        return "the claim is about a quarter or an annualised rate; only annual and monthly figures are wired up"
    if _PER_CAPITA_RE.search(text):
        return "the claim is a per-capita figure; the series is a total"
    if spec.metric == "national_debt":
        m = _NOT_THE_DEBT_RE.search(text)
        if m:
            return (f"the claim is about \u201c{m.group(0).strip()}\u201d, not the total "
                    f"federal debt outstanding")
    if spec.metric == "inflation_rate":
        if _WAGE_RE.search(text):
            return "the claim is about wages, not consumer prices"
        for m in _CATEGORY_INFLATION_RE.finditer(text):
            word = m.group(1) or m.group(2) or ""
            if resolve_geography(word) is not None:
                continue  # "Texas inflation": a place, which the geography check handles
            if m.group(2) and word[:1].isupper() and resolve_geography(text) is not None:
                continue  # "inflation in the Chicago area": a place, not a kind of price
            return (f"the claim is about one category of prices (\u201c{m.group(0).strip()}\u201d), "
                    f"not overall consumer inflation")
    if spec.metric in _JOBS_METRICS:
        m = _SECTOR_RE.search(text)
        if m:
            return (f"the claim is about one industry (\u201c{m.group(0).strip()}\u201d), not total "
                    f"nonfarm employment")
        if _SINCE_DATE_RE.search(text):
            return "the claim counts a change since a date; the series gives one month or one year"
    if spec.metric == "wage_growth":
        if _REAL_DOLLARS_RE.search(text) or re.search(r"\breal\b|\bafter inflation\b|\bpurchasing power\b", text, re.I):
            return "the claim is about wages after inflation; the series is in current dollars"
        if _SINCE_DATE_RE.search(text):
            return "the claim counts a change since a date; the series gives a 12-month change"
        if re.search(r"\b(median|minimum|weekly|annual|salar(?:y|ies))\b", text, re.I):
            return "the claim is about a different measure of pay; the series is average hourly earnings"
    if spec.family == _units.USD and _REAL_DOLLARS_RE.search(text):
        return "the claim is inflation-adjusted; the series is in current dollars"
    if spec.metric == "real_gdp_growth" and _NOMINAL_RE.search(text):
        return "the claim is nominal growth; the series is real (inflation-adjusted) growth"
    return ""


def _all_quantities(text: str) -> List[Quantity]:
    """Every quantity in the claim, so multi-point assertions are detectable."""
    out: List[Quantity] = []
    for m in _units._NUMBER_RE.finditer(text or ""):
        frag = m.group(0)
        if not frag or not frag.strip():
            continue
        q = parse_quantity(frag)
        if q is not None:
            out.append(q)
    return out


@dataclass(frozen=True)
class ParsedClaim:
    text: str
    metric: Optional[MetricSpec]
    year: Optional[int]
    year_basis: str = "calendar"          # calendar | fiscal
    month: Optional[int] = None
    geography: Optional[Geography] = None
    claimed_value: Optional[Quantity] = None
    comparator: str = "equals"            # equals | greater than | less than | at least | at most
    # Set when the claim names a year the data cannot possibly cover yet.
    year_is_future: bool = False
    # Years mentioned beyond the primary one — a two-year claim is a trend
    # claim, and answering it with one number would be misleading.
    all_years: List[int] = None
    # Every quantity in the claim. Two or more means the claim asserts a
    # relationship between points, which a single lookup cannot settle.
    all_values: List[Quantity] = None
    # The claim anchors a value to a period it names only relatively
    # ("when he took office"), which this parser cannot resolve to a year.
    has_unresolved_period: bool = False
    unparsed_reason: str = ""
    # Explicit calendar quarter (1-4), when the claim names one.
    quarter: Optional[int] = None
    # The claim said a plain year about a metric reported by fiscal year.
    fiscal_year_assumed: bool = False
    # Set when the claim qualifies the metric in a way the series cannot
    # honour (subgroup, quarter, per-capita, real vs nominal).
    structured_blocker: str = ""
    # Present tense with no year ("unemployment is 4%"): about the latest figure.
    present: bool = False
    # Part of the claim the lookup does not check, said so on the answer.
    aside: str = ""

    @property
    def is_actionable(self) -> bool:
        """True when Tier 1 can attempt an exact lookup."""
        return self.metric is not None

    @property
    def is_trend_claim(self) -> bool:
        """True when the claim asserts more than one point in time or value.

        Either two years, or two quantities: "was X then and Y now" cannot be
        settled by fetching one number, and comparing the first value against
        whatever single figure we retrieved produces a confident wrong answer.
        """
        if self.all_years and len(self.all_years) > 1:
            return True
        return bool(self.all_values) and len(self.all_values) > 1


def _current_year() -> int:
    return datetime.now(timezone.utc).year


_RECORD_SINCE_RE = re.compile(
    r"\b(?:highest|lowest|biggest|largest|smallest|worst|best|fastest|slowest|most|least)\b"
    r"[^.;]{0,40}\b(?:since|going back to|dating back to|dating to)\s+(?:late\s+|early\s+|mid-?\s*)?"
    r"(?:(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+)?$", re.I)


def _extract_years(text: str) -> List[int]:
    """Every plausible calendar year, excluding bill numbers and section refs."""
    years: List[int] = []
    for m in _YEAR_RE.finditer(text):
        preceding = text[max(0, m.start() - 24):m.start()]
        if _NON_YEAR_CONTEXT_RE.search(preceding.strip()):
            continue
        if _RECORD_SINCE_RE.search(text[max(0, m.start() - 60):m.start()]):
            continue  # "the lowest since 1969" bounds a record; it is not the claim's year
        y = int(m.group(1))
        if y not in years:
            years.append(y)
    return years


def _extract_fiscal_year(text: str) -> Optional[int]:
    m = _FY_RE.search(text)
    if not m:
        return None
    if m.group(1):
        return int(m.group(1))
    two = m.group(2)
    return 2000 + int(two) if two else None


# A comparison word only counts when a figure follows it: "under 4%" compares,
# "under Trump" and "rose 3% over the year" do not.
_FIGURE_AHEAD_RE = re.compile(
    r"\s*(?:just |about |roughly |around |nearly |almost |some |a |an )?[$€£]?\d")


def _extract_comparator(text: str) -> str:
    padded = f" {text.lower()} "
    best, best_pos = "equals", len(padded) + 1
    for name, needles in _COMPARATORS:
        for n in needles:
            start = 0
            while True:
                pos = padded.find(n, start)
                if pos == -1 or pos >= best_pos:
                    break
                trailing = n.startswith(" or ")  # "5% or more": the figure comes first
                if trailing or _FIGURE_AHEAD_RE.match(padded, pos + len(n)):
                    best, best_pos = name, pos
                    break
                start = pos + 1
    return best


def _strip_years_for_value(text: str, years: List[int]) -> str:
    """Remove year tokens so a bare year is never read as the claimed value.

    "Inflation was 9% in 2022" must not parse 2022 as the magnitude. Years are
    removed along with any immediately preceding preposition so the remaining
    text still scans cleanly.
    """
    out = text
    for y in years:
        out = re.sub(rf"\b(?:in|during|for|of|by|since|through)\s+{y}\b", " ", out, flags=re.IGNORECASE)
        out = re.sub(rf"\b{y}\b", " ", out)
    out = _FY_RE.sub(" ", out)
    return re.sub(r"\s+", " ", out).strip()


# "Texas had 30.5 million residents", "About 39 million people live in
# California": a head count, said without the word "population". The number
# must sit directly before the noun (so "5 million Hispanic residents" and
# "2 million more people" are not counts of everyone), and nothing may follow
# but the place and the period ("people without insurance" is a subgroup).
_HEADCOUNT = r"\d[\d,.]*\s*(?:million|billion|thousand)?\s+(?:people|residents|inhabitants|persons)"
_POPULATION_RES = (
    re.compile(r"\b(?:has|had|have|is home to|was home to|counts|counted|numbered|"
               r"reached|hit|topped|surpassed|exceeded|passed)\s+"
               r"(?:about |nearly |roughly |almost |around |approximately |some |over |"
               r"more than |just over |just under |under |fewer than |less than )?"
               + _HEADCOUNT + r"(?P<tail>.*)$", re.IGNORECASE),
    re.compile(_HEADCOUNT + r"\s+(?:live|lived|living|reside|resided)\s+in\b(?P<tail>.*)$",
               re.IGNORECASE),
)
# What may follow the count: places (capitalised), the year, and these words.
_TAIL_WORDS = {"in", "as", "of", "the", "at", "by", "during", "end", "start", "year",
               "census", "according", "to", "state", "city", "county", "today", "now",
               "last", "this", "and", "u.s", "us"}


def _population_headcount(text: str) -> bool:
    for rx in _POPULATION_RES:
        m = rx.search(text or "")
        if not m:
            continue
        for w in re.findall(r"[A-Za-z][A-Za-z.'’-]*", m.group("tail")):
            if not (w[0].isupper() or w.lower().rstrip(".") in _TAIL_WORDS):
                return False
        return True
    return False


_SCALE_WORD_RE = re.compile(r"\b(?:thousand|million|billion|trillion)\b", re.IGNORECASE)


def _choose_metric(text: str, claimed: Optional[Quantity]) -> Optional[MetricSpec]:
    """The best-worded series in the claim's own units.

    A claim stating "26 million" people cannot be checked against a rate, and
    one stating "5%" cannot be checked against a dollar price. Rather than
    fetch a series that can only answer "not comparable" (which then sends the
    claim to the paid web search), it is left unmatched, so the dataset search
    can look for a series in its units first.
    """
    specs = match_metrics(text)
    if not specs and _population_headcount(text):
        specs = [BY_METRIC["population"]]
    fam = claimed.family if claimed is not None else _units.UNKNOWN
    if fam == _units.UNKNOWN and claimed is not None and _SCALE_WORD_RE.search(claimed.raw or ""):
        # "26 million Americans": a count with no unit noun. It is never a rate.
        specs = [sp for sp in specs if sp.family != _units.PERCENT]
    if fam == _units.UNKNOWN or not specs:
        return specs[0] if specs else None
    for spec in specs:
        if spec.family == fam:
            return spec
    return None


# Measures Census publishes for every city. "Nashville has about 720,000
# people" names no state and no "in", so nothing marked Nashville as a place
# and it was checked against the U.S. population. Only for these measures: a
# subject in "Biden has added 15 million jobs" is a person, not a place.
_PLACE_MEASURES = frozenset(("population", "median_household_income", "median_home_value",
                             "median_gross_rent", "poverty_rate", "uninsured_rate", "insured_rate",
                             "bachelors_or_higher"))
_NOT_SUBJECT_PLACE = frozenset("""
it this that there here he she they we i you our my his her their its census
median average household households income poverty population rent rents
home homes housing americans people residents everyone nobody one
""".split())
_SUBJECT_PLACE_RE = re.compile(
    r"^\s*(?:(?i:the\s+(?:city|town|village)\s+of)\s+)?"
    r"([A-Z][a-z.'’]+(?:[\s-][A-Z][a-z.'’]+){0,2})"
    r"(?:['’]s\b|\s+(?:(?i:now|still|currently|already|today)\s+)?"
    r"(?i:has|had|have|is\s+home\s+to|was\s+home\s+to|counts?|counted|houses|grew\s+to|"
    r"reached|tops|topped|surpassed|passed|crossed|is|was)\b)")


def _subject_place(raw: str):
    """The city a claim opens with, when it names no other place."""
    from claims.geography import Geography, _NATIONAL_PHRASES, _NOT_A_PLACE, \
        _proper_noun_is_unresolved, _word_bounded
    m = _SUBJECT_PLACE_RE.match(raw)
    if not m:
        return None
    name = m.group(1).strip(" .")
    low = name.lower()
    if low in _NOT_A_PLACE or any(_word_bounded(low, p) for p in _NATIONAL_PHRASES):
        return None
    if not _proper_noun_is_unresolved(name) or match_metrics(name):
        return None  # "Poverty was 11.1%" opens with the measure, not a city
    if low.split()[0] in _NOT_SUBJECT_PLACE:
        return None
    return Geography(name=name, level="place")


def parse_claim(text: str) -> ParsedClaim:
    """Parse a claim into a lookup request. Never raises."""
    raw = (text or "").strip()
    if not raw:
        return ParsedClaim(text="", metric=None, year=None, all_years=[],
                           unparsed_reason="empty claim")

    quarter, quarter_year = _extract_quarter(raw)

    fiscal_year = _extract_fiscal_year(raw)
    years = _extract_years(raw)
    # A year that only names an event ("the 1973 oil crisis") dates nothing.
    events = _event_years(raw)
    if events:
        # "During the 2008 financial crisis, GDP fell 5% from the spring of
        # 2008 to the spring of 2009": 2008 names the event and is also one
        # end of the span, so it still counts where it stands on its own.
        apart = _extract_years(_EVENT_YEAR_RE.sub(" ", raw))
        events = [y for y in events if y not in apart]
    dated_by_event = bool(events) and all(y in events for y in years)
    years = [y for y in years if y not in events] if events else years
    present = False
    month = None
    mm = _MONTH_YEAR_RE.search(raw)
    if mm:
        month = _MONTHS.get(mm.group(1).lower())
    # "rose to 4.6% in November": a month with no year beside it. With one
    # year elsewhere in the claim it is that year's November; with none, the
    # period cannot be pinned down (a news story means "this November").
    dated_by_month = False
    if mm is None:
        bare = _BARE_MONTH_RE.search(raw)
        if bare:
            if len(years) == 1 and fiscal_year is None:
                month = _MONTHS.get(bare.group(1).lower())
            elif not years:
                dated_by_month = True

    if fiscal_year is not None:
        year, basis = fiscal_year, "fiscal"
    elif years:
        # The LAST year mentioned is the one a claim is usually asserting about
        # ("was X in 2019, and Y in 2024"). For a single-year claim they agree.
        year, basis = years[-1], "calendar"
    elif _PRESENT_TENSE_RE.search(raw):
        # "The national debt is over $33 trillion" — a present-tense claim wants
        # the latest available figure, which is not the same as this year's
        # annual average. Leave year unset; the adapter fetches latest.
        year, basis = None, "calendar"
        present = True
    else:
        year, basis = None, "calendar"

    geography = resolve_geography(raw)

    # Age bounds that define a universe ("adults 25 and older", "ages 16+")
    # are not claimed values; left in, they made a one-number claim look like
    # a two-number trend claim.
    # An event's year is still a year, not a second figure.
    value_text = _AGE_BOUND_RE.sub(" ", _strip_years_for_value(raw, years + events))
    # "30-year mortgage", "a 40-year high", "the lowest in 54 years" name spans,
    # not claimed values. A superlative is said to be unchecked, never ignored.
    superlative = _SUPERLATIVE_RE.search(value_text)
    aside = ""
    if superlative:
        aside = (f"The claim also says it was {superlative.group(0).strip().rstrip(',.')}; "
                 f"this check covers the figure only.")
    value_text = _SPAN_RE.sub(" ", _SUPERLATIVE_RE.sub(" ", value_text))
    # "rose 0.1 percentage point to 4.2%": the points are the move, not the level.
    value_text = _POINT_CHANGE_RE.sub(" ", value_text)
    # "Q2" must not be read as the number 2.
    value_text = _QUARTER_TOKEN_RE.sub(" ", value_text)
    claimed = parse_quantity(value_text)
    spec = _select_variant(raw, _choose_metric(raw, claimed))
    # "rose 0.3% to 4.2%", "increased by 339,000 to 4.8 million": for a level,
    # the figure after "to" is the claim; the one before it is how far it moved.
    if spec is not None and not _is_a_change(spec) and _CHANGE_THEN_LEVEL_RE.search(value_text):
        value_text = _CHANGE_THEN_LEVEL_RE.sub(lambda m: m.group(0).replace(m.group(1), " "), value_text)
        claimed = parse_quantity(value_text)
    # "The economy added 3 million jobs": the change in payrolls, not the level.
    jobs = _JOBS_CHANGE_RE.search(raw)
    if jobs and claimed is not None and claimed.family == "jobs" and (
            spec is None or spec.metric in ("nonfarm_employment", "jobs_added")):
        spec = BY_METRIC.get("jobs_added")
        if jobs.group(1).lower() in ("lost", "shed", "cut", "eliminated", "lose", "loses", "sheds"):
            claimed = Quantity(magnitude=-abs(claimed.magnitude), family=claimed.family,
                               approximate=claimed.approximate, raw=claimed.raw,
                               precision=claimed.precision)
    # "Nonfarm payroll employment rose by 165,000": the same change, in the
    # words the jobs report itself uses.
    payrolls = _PAYROLLS_CHANGE_RE.search(raw)
    if payrolls and claimed is not None and claimed.family in ("jobs", "unknown") and (
            spec is None or spec.metric in ("nonfarm_employment", "jobs_added")) \
            and not _PRIVATE_PAYROLLS_RE.search(raw):
        spec = BY_METRIC.get("jobs_added")
        if payrolls.group(1).lower() in _PAYROLLS_FELL:
            claimed = Quantity(magnitude=-abs(claimed.magnitude), family=claimed.family,
                               approximate=claimed.approximate, raw=claimed.raw,
                               precision=claimed.precision)
    # "The number of people working part time" is not the population.
    if spec is not None and spec.metric == "population" and _PEOPLE_COUNT_RE.search(raw) \
            and not _POPULATION_WORD_RE.search(raw):
        spec = None
    # "34.4 hours" is not a value of whatever measure the sentence also names.
    if spec is not None and _in_a_foreign_unit(raw, claimed):
        spec = None
    # "Prices rose 3.4% over the year": prices in general, so CPI inflation.
    # Only at the start, so "egg prices rose" never becomes overall inflation.
    if spec is None and claimed is not None and claimed.family == "percent" \
            and _PRICES_ROSE_RE.match(raw):
        spec = BY_METRIC.get("inflation_rate")
    # A percent claim whose metric is a percent metric: keep as-is. A bare
    # number with no unit against a USD metric is dollars.
    if claimed is not None and spec is not None and claimed.family == "unknown":
        # Keep the written precision: without it "30 million" fell back to a
        # blanket 1% band instead of "to the nearest million".
        claimed = Quantity(
            magnitude=claimed.magnitude, family=spec.family,
            approximate=claimed.approximate, raw=claimed.raw, precision=claimed.precision,
        )

    # "GDP fell 2.2%" is a growth rate of -2.2%; "inflation fell to 3%" is a level.
    if (claimed is not None and spec is not None and spec.metric in _SIGNED_CHANGE_METRICS
            and claimed.magnitude > 0 and _FELL_BY_RE.search(raw)):
        claimed = Quantity(
            magnitude=-claimed.magnitude, family=claimed.family,
            approximate=claimed.approximate, raw=claimed.raw, precision=claimed.precision,
        )

    if geography is None and spec is not None and spec.metric in _PLACE_MEASURES:
        geography = _subject_place(raw)

    all_values = _all_quantities(value_text)
    has_unresolved = (bool(_RELATIVE_PERIOD_RE.search(raw)) or dated_by_event or dated_by_month) and year is None

    if quarter is not None and quarter_year:
        year, basis = quarter_year, "calendar"
        if _QUARTER_BY_MONTHS_RE.search(raw):
            month = None        # "July through September" names the quarter, not September

    fiscal_assumed = False
    if spec is not None and spec.fiscal_year_default and basis == "calendar" and year:
        basis, fiscal_assumed = "fiscal", True

    year_is_future = bool(year and year > _current_year())

    reason = ""
    if spec is None:
        reason = "no wired-up series matches this claim"

    return ParsedClaim(
        text=raw,
        present=present,
        aside=aside,
        metric=spec,
        year=year,
        year_basis=basis,
        month=month,
        geography=geography,
        claimed_value=claimed,
        comparator=_extract_comparator(raw),
        year_is_future=year_is_future,
        all_years=years,
        all_values=all_values,
        has_unresolved_period=has_unresolved,
        unparsed_reason=reason,
        structured_blocker=structured_blocker(raw, spec, quarter, claimed),
        quarter=quarter,
        fiscal_year_assumed=fiscal_assumed,
    )
