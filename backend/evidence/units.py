"""Parse and compare the quantities that appear in claims and in API payloads.

Two jobs, and they are deliberately separate from any notion of *which* metric
is being discussed:

  * `parse_quantity` turns the number as a human wrote it ("$33 trillion",
    "3.7%", "about 790.9 billion dollars") into a magnitude in base units plus
    the unit family it belongs to.
  * `Quantity.compare_to` decides whether two magnitudes agree, given that a
    claim saying "about 3.7%" is asserting something weaker than one saying
    "exactly 3.7%".

Keeping magnitude and unit family together is what stops the comparison bug
this codebase is most exposed to: BEA reports in millions, Treasury in whole
dollars, BLS in percent, and Census in people. A bare float compared against
another bare float will silently call $790,895 (millions) different from
$790.9 billion. Everything here normalises to base units first — dollars,
persons, index points, percent — so the comparison happens in one space.
"""
import math
import re
from dataclasses import dataclass
from typing import Optional

# Unit families. Two quantities are only comparable within the same family.
PERCENT = "percent"
USD = "usd"
PERSONS = "persons"
INDEX = "index"
JOBS = "jobs"
UNKNOWN = "unknown"

# Multiplier words, longest first so "trillion" is not shadowed by a prefix.
_SCALE_WORDS = [
    ("quadrillion", 1e15),
    ("trillion", 1e12),
    ("billion", 1e9),
    ("million", 1e6),
    ("thousand", 1e3),
    ("hundred", 1e2),
    ("bn", 1e9),
    ("tn", 1e12),
    ("mn", 1e6),
    ("k", 1e3),
]

# BEA's UNIT_MULT is an exponent: "6" means the value is in millions.
def scale_from_exponent(unit_mult: Optional[object]) -> float:
    """BEA-style UNIT_MULT ('6' -> 1e6). Returns 1.0 when absent or unusable."""
    if unit_mult is None:
        return 1.0
    try:
        exp = int(str(unit_mult).strip())
    except (TypeError, ValueError):
        return 1.0
    # Guard against absurd exponents rather than producing inf.
    if not -30 <= exp <= 30:
        return 1.0
    return float(10 ** exp)


def written_precision(number_text: str, scale: float = 1.0) -> Optional[float]:
    """The place value of the last digit the writer actually wrote.

    Someone who writes "4.0%" is asserting to a tenth of a point; someone who
    writes "9%" is asserting to a whole point; "$33 trillion" asserts to the
    nearest trillion. Comparing all three against a fixed relative tolerance
    treats them as equally precise and produces absurd results — a claim of
    "9%" against a true 9.06% comes out Contradicted, even though 9.06 rounds
    to 9 and the claim is plainly right.

    Trailing zeros in an integer are treated as insignificant, per the usual
    convention: "$73,000" asserts to the nearest thousand, not the nearest
    dollar, so a true 73,035 agrees with it.

    Returns the place value in BASE units, or None if it cannot be determined.
    """
    if not number_text:
        return None
    s = re.sub(r"[$€£,\s%]", "", str(number_text)).strip()
    s = re.sub(r"(?i)(percent|dollars?|usd|trillion|billion|million|thousand|bn|tn|mn|k)", "", s).strip()
    s = s.lstrip("+-")
    if not s or not re.fullmatch(r"\d*\.?\d*", s) or s in (".", ""):
        return None

    if "." in s:
        decimals = len(s.split(".", 1)[1])
        return (10.0 ** -decimals) * scale

    digits = s
    stripped = digits.rstrip("0")
    trailing_zeros = len(digits) - len(stripped)
    # An all-zero integer ("0") has no significant trailing zeros to strip.
    if not stripped:
        trailing_zeros = 0
    return (10.0 ** trailing_zeros) * scale


@dataclass(frozen=True)
class Quantity:
    """A magnitude in BASE units (dollars, persons, percent points, …)."""
    magnitude: float
    family: str
    # True when the source text hedged ("about", "roughly", "nearly", "~").
    approximate: bool = False
    # The text this came from, for display and debugging.
    raw: str = ""
    # Place value of the last digit the writer wrote, in base units. This is
    # what makes "9%" and "9.06%" agree while "3.7%" and "4.0%" do not.
    precision: Optional[float] = None

    def tolerance(self) -> Optional[float]:
        """Absolute tolerance implied by how precisely the claim was written."""
        if self.precision is None:
            return None
        # Half the last written place: 9% covers [8.5, 9.5).
        tol = self.precision / 2.0
        # A hedge ("about $73,000") widens it by an order of magnitude.
        return tol * 10.0 if self.approximate else tol

    def compare_to(self, other: "Quantity", tolerance: Optional[float] = None) -> "Comparison":
        """Compare against an authoritative quantity.

        `tolerance` is a RELATIVE tolerance (0.02 == 2%). When omitted it is
        chosen from whether the claim hedged: a claim that says "about $33
        trillion" is not refuted by $33.4 trillion, but one that says
        "$33.0 trillion" is a precise assertion and gets a tight tolerance.
        """
        if self.family != other.family and UNKNOWN not in (self.family, other.family):
            return Comparison(
                agrees=False, incomparable=True, delta=None, relative_delta=None,
                reason=f"units differ: claim is in {self.family}, evidence in {other.family}",
            )

        if tolerance is None:
            tolerance = 0.05 if self.approximate else 0.005

        delta = self.magnitude - other.magnitude
        denom = abs(other.magnitude)
        # A zero reference makes a relative comparison meaningless; fall back to
        # absolute equality rather than dividing by zero.
        if denom < 1e-12:
            agrees = abs(delta) < 1e-12
            return Comparison(agrees=agrees, incomparable=False, delta=delta,
                              relative_delta=None,
                              reason="" if agrees else "evidence value is zero")
        rel = delta / denom
        agrees = abs(rel) <= tolerance
        return Comparison(
            agrees=agrees, incomparable=False, delta=delta, relative_delta=rel,
            reason="" if agrees else f"differs by {rel:+.1%}",
        )


@dataclass(frozen=True)
class Comparison:
    agrees: bool
    incomparable: bool
    delta: Optional[float]
    relative_delta: Optional[float]
    reason: str


# Words that weaken a numeric assertion, so "about $33 trillion" is not refuted
# by $33.4 trillion. Deliberately excludes over/under/more than/less than/at
# least/at most: those are COMPARATORS, not hedges. Treating "over $33 trillion"
# as approximate would loosen the tolerance tenfold on a claim that is actually
# making a precise directional assertion.
_HEDGE_RE = re.compile(
    r"\b(about|approximately|approx|roughly|around|nearly|almost|close to|"
    r"some|circa|somewhere near|in the region of|give or take)\b|~",
    re.IGNORECASE,
)

_NUMBER_RE = re.compile(
    r"""
    (?P<currency>[$€£])?\s*
    (?P<sign>[-+]|\(\s*(?=[\d.]))?          # leading sign, or an accounting "("
    (?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)
    \s*
    (?P<scale>quadrillion|trillion|billion|million|thousand|hundred|bn|tn|mn|k)?
    \s*
    (?P<closeparen>\))?
    \s*
    (?P<unit>%|percent|percentage\s+points?|pp|dollars?|usd|people|persons?|jobs?)?
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _is_jam_value(v: float) -> bool:
    """Census 'jam values' (-666666666, -999999999, …) are annotations.

    They mean 'estimate suppressed' or 'not applicable', not a measurement.
    Reading one as data yields a median income of minus 666 million dollars.
    """
    return v <= -66666666


def parse_number(text: str) -> Optional[float]:
    """Parse a single numeric token from API payloads.

    Handles comma grouping, accounting negatives "(1,234)", stray currency
    symbols, and the sentinel strings government APIs use for missing data.
    Returns None rather than raising, and rather than returning 0.0 — a
    suppressed Census cell must never be read as the number zero.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        v = float(text)
        if math.isnan(v) or math.isinf(v):
            return None
        # Census jam values arrive as JSON numbers as often as strings, so the
        # check has to happen on both paths, not only after string cleanup.
        return None if _is_jam_value(v) else v

    s = str(text).strip()
    if not s:
        return None

    # Sentinels meaning "no data". Census also uses large negative codes.
    if s.upper() in {"(NA)", "NA", "N/A", "(D)", "(S)", "(X)", "-", "--", "...", "NULL", "NONE"}:
        return None

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()

    s = re.sub(r"[$€£,\s]", "", s)
    s = re.sub(r"(?i)(percent|%|dollars?|usd)$", "", s).strip()
    if not s:
        return None

    try:
        v = float(s)
    except ValueError:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if negative:
        v = -v

    return None if _is_jam_value(v) else v


def _family_from_unit_word(word: Optional[str], had_currency: bool) -> str:
    w = (word or "").strip().lower()
    if w in ("%", "percent"):
        return PERCENT
    if w in ("pp", "percentage point", "percentage points"):
        return PERCENT
    if w in ("dollar", "dollars", "usd"):
        return USD
    if w in ("people", "person", "persons"):
        return PERSONS
    if w in ("job", "jobs"):
        return JOBS
    return USD if had_currency else UNKNOWN


def parse_quantity(text: str) -> Optional[Quantity]:
    """Extract the first quantity a human wrote, normalised to base units.

    >>> parse_quantity("over $33 trillion").magnitude
    33000000000000.0
    >>> parse_quantity("about 3.7%").approximate
    True
    """
    if not text:
        return None
    m = _NUMBER_RE.search(text)
    if not m:
        return None

    base = parse_number(m.group("number"))
    if base is None:
        return None

    sign = m.group("sign") or ""
    if sign == "-":
        base = -base
    elif sign.startswith("(") and m.group("closeparen"):
        base = -base

    scale_word = (m.group("scale") or "").lower()
    multiplier = 1.0
    for word, mult in _SCALE_WORDS:
        if scale_word == word:
            multiplier = mult
            break

    family = _family_from_unit_word(m.group("unit"), bool(m.group("currency")))

    # "$5 billion" is dollars even without the word "dollars"; a bare scale word
    # with no unit ("10 million people" handled above) stays unknown so the
    # caller can resolve it from the metric instead of guessing.
    magnitude = base * multiplier

    # Percentages are compared as percentage points, never rescaled.
    if family == PERCENT:
        magnitude = base

    # Percentages are never rescaled, so their precision is not scaled either.
    precision = written_precision(m.group("number"), 1.0 if family == PERCENT else multiplier)

    return Quantity(
        magnitude=magnitude,
        family=family,
        approximate=bool(_HEDGE_RE.search(text)),
        raw=m.group(0).strip(),
        precision=precision,
    )


def humanize(magnitude: float, family: str) -> str:
    """Render a base-unit magnitude the way a person would write it."""
    if family == PERCENT:
        return f"{magnitude:.2f}".rstrip("0").rstrip(".") + "%"
    if family in (PERSONS, JOBS):
        a = abs(magnitude)
        if a >= 1e9:
            return f"{magnitude / 1e9:.2f} billion"
        if a >= 1e6:
            return f"{magnitude / 1e6:.2f} million"
        if a >= 1e3:
            return f"{magnitude:,.0f}"
        return f"{magnitude:,.0f}"
    if family == USD:
        a = abs(magnitude)
        sign = "-" if magnitude < 0 else ""
        if a >= 1e12:
            return f"{sign}${a / 1e12:.2f} trillion"
        if a >= 1e9:
            return f"{sign}${a / 1e9:.2f} billion"
        if a >= 1e6:
            return f"{sign}${a / 1e6:.2f} million"
        return f"{sign}${a:,.0f}"
    return f"{magnitude:,.2f}".rstrip("0").rstrip(".")
