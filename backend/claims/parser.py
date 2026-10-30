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
# Bill designators and other numerals that look like years but are not.
_NON_YEAR_CONTEXT_RE = re.compile(
    r"(?:h\.\s?r\.|h\.\s?res\.|s\.\s?res\.|s\.|bill|section|§|act of)\s*\d{0,4}$",
    re.IGNORECASE,
)

_JOBS_CHANGE_RE = re.compile(
    r"\b(added|adds|add|created|creates|create|gained|gains|lost|lose|loses|shed|sheds|cut|"
    r"eliminated)\s+(?:a\s+(?:net\s+|record\s+)?|about\s+|nearly\s+|roughly\s+|around\s+|almost\s+|"
    r"more than\s+|over\s+|just over\s+|just under\s+|under\s+|some\s+)?[\d,.]+\s*"
    r"(?:million|thousand|k|m)?\s+(?:new\s+|net\s+)?jobs\b",
    re.IGNORECASE,
)

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
    r"children|child|kids|teens?|teenagers?|youth|young (?:people|adults|workers|men|women)|"
    r"seniors|elderly|older (?:americans|adults|workers)|retirees|veterans|"
    r"immigrants?|foreign[- ]born|native[- ]born|non[- ]citizens?|"
    r"rural|urban|disabled|people with disabilities|households headed by|"
    r"married couples?|families with)\b"
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
    r"\bquarterly\b|\bannuali[sz]ed\b|\bannual rate\b|\bsaar\b",
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
_QUARTER_TOKEN_RE = re.compile(r"\bq[1-4]\b|\b(\d{4})q[1-4]\b", re.IGNORECASE)

# Metrics that have a quarterly series wired up, and the phrasing that goes
# with quarterly figures ("at an annual rate") is then expected, not a blocker.
_QUARTERLY_METRICS = {"real_gdp_growth"}


def _extract_quarter(text: str):
    """(quarter, year) for an explicit quarter, else (None, None)."""
    m = _QUARTER_YEAR_RE.search(text or "")
    if not m:
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


def income_band(text: str) -> str:
    """The phrase that makes a claim about HUD income limits or an income band, or ""."""
    m = _INCOME_BAND_RE.search(text or "")
    return m.group(0).strip() if m else ""


def structured_blocker(text: str, spec: Optional[MetricSpec], quarter: Optional[int] = None) -> str:
    """Why the matched series cannot answer this claim, or "" if it can."""
    if spec is None or not text:
        return ""
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
    if _QUARTER_RE.search(text) and not quarterly_ok:
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


def _extract_years(text: str) -> List[int]:
    """Every plausible calendar year, excluding bill numbers and section refs."""
    years: List[int] = []
    for m in _YEAR_RE.finditer(text):
        preceding = text[max(0, m.start() - 24):m.start()]
        if _NON_YEAR_CONTEXT_RE.search(preceding.strip()):
            continue
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


def parse_claim(text: str) -> ParsedClaim:
    """Parse a claim into a lookup request. Never raises."""
    raw = (text or "").strip()
    if not raw:
        return ParsedClaim(text="", metric=None, year=None, all_years=[],
                           unparsed_reason="empty claim")

    quarter, quarter_year = _extract_quarter(raw)

    fiscal_year = _extract_fiscal_year(raw)
    years = _extract_years(raw)
    present = False
    month = None
    mm = _MONTH_YEAR_RE.search(raw)
    if mm:
        month = _MONTHS.get(mm.group(1).lower())

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
    value_text = _AGE_BOUND_RE.sub(" ", _strip_years_for_value(raw, years))
    # "30-year mortgage", "a 40-year high", "the lowest in 54 years" name spans,
    # not claimed values. A superlative is said to be unchecked, never ignored.
    superlative = _SUPERLATIVE_RE.search(value_text)
    aside = ""
    if superlative:
        aside = (f"The claim also says it was {superlative.group(0).strip().rstrip(',.')}; "
                 f"this check covers the figure only.")
    value_text = _SPAN_RE.sub(" ", _SUPERLATIVE_RE.sub(" ", value_text))
    # "Q2" must not be read as the number 2.
    value_text = _QUARTER_TOKEN_RE.sub(" ", value_text)
    claimed = parse_quantity(value_text)
    spec = _select_variant(raw, _choose_metric(raw, claimed))
    # "The economy added 3 million jobs": the change in payrolls, not the level.
    jobs = _JOBS_CHANGE_RE.search(raw)
    if jobs and claimed is not None and claimed.family == "jobs" and (
            spec is None or spec.metric in ("nonfarm_employment", "jobs_added")):
        spec = BY_METRIC.get("jobs_added")
        if jobs.group(1).lower() in ("lost", "shed", "cut", "eliminated", "lose", "loses", "sheds"):
            claimed = Quantity(magnitude=-abs(claimed.magnitude), family=claimed.family,
                               approximate=claimed.approximate, raw=claimed.raw,
                               precision=claimed.precision)
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

    all_values = _all_quantities(value_text)
    has_unresolved = bool(_RELATIVE_PERIOD_RE.search(raw)) and year is None

    if quarter is not None and quarter_year:
        year, basis = quarter_year, "calendar"

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
        structured_blocker=structured_blocker(raw, spec, quarter),
        quarter=quarter,
        fiscal_year_assumed=fiscal_assumed,
    )
