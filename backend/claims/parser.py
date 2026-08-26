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
from claims.metrics import MetricSpec, match_metric
from evidence.datapoint import Geography
from evidence.units import Quantity, parse_quantity
import evidence.units as _units

# Comparators, longest first so "at least" beats "least".
_COMPARATORS = [
    ("greater than", (" more than ", " greater than ", " over ", " above ",
                      " exceeded ", " exceeds ", " higher than ", " north of ")),
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


def _extract_comparator(text: str) -> str:
    padded = f" {text.lower()} "
    best, best_pos = "equals", len(padded) + 1
    for name, needles in _COMPARATORS:
        for n in needles:
            pos = padded.find(n)
            if pos != -1 and pos < best_pos:
                best, best_pos = name, pos
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


def parse_claim(text: str) -> ParsedClaim:
    """Parse a claim into a lookup request. Never raises."""
    raw = (text or "").strip()
    if not raw:
        return ParsedClaim(text="", metric=None, year=None, all_years=[],
                           unparsed_reason="empty claim")

    spec = match_metric(raw)

    fiscal_year = _extract_fiscal_year(raw)
    years = _extract_years(raw)
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
    else:
        year, basis = None, "calendar"

    geography = resolve_geography(raw)

    value_text = _strip_years_for_value(raw, years)
    claimed = parse_quantity(value_text)
    # A percent claim whose metric is a percent metric: keep as-is. A bare
    # number with no unit against a USD metric is dollars.
    if claimed is not None and spec is not None and claimed.family == "unknown":
        claimed = Quantity(
            magnitude=claimed.magnitude, family=spec.family,
            approximate=claimed.approximate, raw=claimed.raw,
        )

    all_values = _all_quantities(value_text)
    has_unresolved = bool(_RELATIVE_PERIOD_RE.search(raw)) and year is None

    year_is_future = bool(year and basis == "calendar" and year > _current_year())

    reason = ""
    if spec is None:
        reason = "no wired-up series matches this claim"

    return ParsedClaim(
        text=raw,
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
    )
