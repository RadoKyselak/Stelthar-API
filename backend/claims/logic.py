"""The logic of a claim: what its words "not", "every", "most", "between" and
"and" do to the check. Rules only, no model, no network.

A claim is read as a formula over checks the engine already runs:

    "wasn't 3.6%"                 not (value = 3.6%)
    "not above 5%"                value <= 5%            (not (value > 5%))
    "between 3% and 4%"           3% <= value <= 4%
    "3.6%, not 5%"                value = 3.6% and not (value = 5%)
    "most Republicans voted no"   Nay > half of the party's members
    "not every Republican ..."    not (every Republican ...)
    "A and B"                     both checked; the worse verdict wins

Verdicts combine as three-valued logic: Supported (true), Contradicted
(false) and unknown (None: the record cannot settle that part). "A and B" is
false if either is false, true only if both are true, otherwise unknown, so a
part this engine cannot read never turns a false claim true.
"""
import re
from typing import Iterable, Optional, Tuple

# not (x > n) is x <= n, and so on: a negated threshold is the other threshold.
NEGATED = {
    "greater than": "at most", "at most": "greater than",
    "at least": "less than", "less than": "at least",
    "equals": "not equal", "not equal": "equals",
}

# "It is false that ...": the whole claim is denied.
_DENIED = re.compile(r"^\s*(?:it(?:['’]s| is| was)\s+(?:false|untrue|not true|a myth)|"
                     r"(?:it\s+)?(?:is\s+)?not true)\s+that\b", re.I)
# A verb denied: "was not", "wasn't", "did not", "never". Not "not only/just",
# which strengthens rather than denies ("not only low, it was 3.6%").
_NOT = re.compile(
    r"(?:\b(?:is|was|were|are|has|have|had|did|does|do|will|would|could|has\s+been|have\s+been|had\s+been)\s+not"
    r"|\b(?:isn|wasn|weren|aren|hasn|haven|hadn|didn|doesn|don|won|couldn|wouldn)['’]?t)"
    r"(?!\s+(?:only|just|merely|simply|seasonally)\b)", re.I)
# Over a stretch of time: "never below 4% in 2023" is about every month of it,
# which one average for the year cannot settle.
_EVERY_POINT = re.compile(r"\b(?:never|always|at no point|at any point|every month|each month)\b", re.I)
_CLAUSE_EDGE = re.compile(r"[;:]|,\s|\b(?:but|while|whereas|although|though)\b", re.I)

# "between 3% and 4%", "between $30,000 and $40,000"
_BETWEEN = re.compile(r"\bbetween\s+(?P<lo>[$€£]?\d[\d,.]*\s*(?:%|percent|[a-z]+)?)\s+and\s+"
                      r"(?P<hi>[$€£]?\d[\d,.]*\s*(?:%|percent|million|billion|trillion|thousand)?)", re.I)
# "3.6%, not 5%", "3.6% rather than 5%": the second figure is the one denied.
_CONTRAST = re.compile(r"(?:,\s*(?:and\s+)?not|\brather than|\binstead of)\s+(?P<fig>[$€£]?\d[\d,.]*\s*"
                       r"(?:%|percent|million|billion|trillion|thousand)?)", re.I)


def _clause_before(text: str, at: int) -> str:
    """The words from the start of the figure's own clause up to the figure."""
    head = text[:max(at, 0)]
    cut = 0
    for m in _CLAUSE_EDGE.finditer(head):
        cut = m.end()
    return head[cut:]


def denied(text: str, figure_at: int) -> bool:
    """Whether the claim denies its figure: an odd number of negations
    ("was not", "it is false that ...") ahead of it in its clause."""
    n = len(_NOT.findall(_clause_before(text, figure_at)))
    if _DENIED.search(text):
        n += 1
    return n % 2 == 1


def every_point(text: str, figure_at: int) -> bool:
    """"never below 4%", "always above": a claim about every point of a period."""
    return bool(_EVERY_POINT.search(_clause_before(text, figure_at)))


def negate(comparator: str) -> str:
    return NEGATED.get(comparator, comparator)


def between(text: str) -> Optional[Tuple[str, str, Tuple[int, int]]]:
    m = _BETWEEN.search(text or "")
    if not m:
        return None
    return m.group("lo").strip(), m.group("hi").strip(), m.span()


def contrast(text: str) -> Optional[Tuple[str, Tuple[int, int]]]:
    m = _CONTRAST.search(text or "")
    if not m:
        return None
    return m.group("fig").strip(), m.span()


# ------------------------------------------------------------ three-valued AND

SUPPORTED, MOSTLY_ACCURATE, MISLEADING, CONTRADICTED = "Supported", "Mostly accurate", "Misleading", "Contradicted"
_RANK = {SUPPORTED: 0, MOSTLY_ACCURATE: 1, MISLEADING: 2, CONTRADICTED: 3}


def conjunction(outcomes: Iterable[Optional[str]]) -> Optional[str]:
    """The verdict on "A and B and ...": any false part makes it false; it is
    true only when every part is known; otherwise unknown (None)."""
    got = list(outcomes)
    if any(o == CONTRADICTED for o in got):
        return CONTRADICTED
    if not got or any(o not in _RANK for o in got):
        return None
    return max(got, key=_RANK.__getitem__)
