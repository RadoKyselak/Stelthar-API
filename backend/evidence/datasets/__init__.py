"""Dataset search: answer claims from government datasets the fixed metric
list does not cover, without a web search.

Each provider exposes the same three steps:

  candidates(...)  find series that might measure what a claim states,
                   from the provider's own catalog (no model call)
  fetch(...)       read one series' value for a place and period straight
                   from the provider's API, as a Datapoint

A short model call (picker.py) chooses among the candidates. It never sees or
produces a number: it only says which real series the claim is about.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Set

_STOP = set("""
a an and are as at be been but by for from had has have in into is it its of on or per
than that the their there these this to was were what when which who will with about
around nearly roughly almost over under more less above below least most up down
percent percentage people person americans american us u.s nation national country
year years month rate rates total number average median mean
""".split())
# "median" and "average" are kept out of the stop list for scoring below
_KEEP = {"median", "average", "mean", "rate", "total"}

# How people say it -> how the agency labels it.
SYNONYMS: Dict[str, List[str]] = {
    "immigrant": ["foreign-born", "foreign"], "immigrants": ["foreign-born", "foreign"],
    "abroad": ["foreign-born"], "born": ["birth", "born"],
    "homeownership": ["owner-occupied"], "homeowners": ["owner-occupied"], "own": ["owner-occupied"],
    "renters": ["renter-occupied"], "renting": ["renter-occupied"], "rent": ["rent", "gross"],
    "commute": ["travel", "commuting"], "commuting": ["travel", "commuting"],
    "internet": ["internet", "broadband"], "broadband": ["broadband", "internet"],
    "veterans": ["veterans"], "veteran": ["veterans"],
    "disabled": ["disability"], "disability": ["disability"],
    "spanish": ["spanish", "language"], "english": ["english", "language"],
    "uninsured": ["insurance", "no"], "insurance": ["insurance", "health"],
    "income": ["income"], "earnings": ["earnings"], "poverty": ["poverty"], "poor": ["poverty"],
    "age": ["age"], "older": ["years", "over"], "seniors": ["65", "over"], "elderly": ["65", "over"],
    "children": ["under", "18"], "kids": ["under", "18"],
    "home": ["housing", "value"], "homes": ["housing", "value"], "house": ["housing", "value"],
    "vacant": ["vacant"], "vacancy": ["vacant", "vacancy"],
    "married": ["married"], "divorced": ["divorced"], "college": ["bachelor's", "degree"],
    "graduates": ["graduate", "degree"], "degree": ["degree"],
    "population": ["population"], "residents": ["population"], "live": ["population"],
    "households": ["households"], "families": ["families"], "jobs": ["employed", "employment"],
    "unemployment": ["unemployment"], "unemployed": ["unemployment"],
    "mortgage": ["mortgage"], "inflation": ["consumer", "price"], "prices": ["price"],
}


def tokens(text: str) -> Set[str]:
    """Lower-case content words, with naive stemming, numbers dropped."""
    out: Set[str] = set()
    for w in re.findall(r"[a-zA-Z][a-zA-Z'\-]+", (text or "").lower()):
        w = re.sub(r"'s$", "", w).strip("'-")
        if len(w) < 3 or (w in _STOP and w not in _KEEP):
            continue
        out.add(w)
        if w.endswith("ies") and len(w) > 4:
            out.add(w[:-3] + "y")
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            out.add(w[:-1])
    return out


# Phrases people use for a measure the agency names differently.
PHRASES = [
    (re.compile(r"born (in another|in a foreign|abroad|outside)", re.I), ["foreign-born"]),
    (re.compile(r"\bown(s|ed)? (their|a) home", re.I), ["owner-occupied"]),
    (re.compile(r"\bhealth (insurance|coverage)", re.I), ["insurance", "coverage"]),
    (re.compile(r"\bwork from home|\bremote work", re.I), ["worked", "home"]),
    (re.compile(r"\bat home\b", re.I), ["home"]),
    # "people" is too common to score on, but a count of people is a population.
    (re.compile(r"\b(people|persons|residents|inhabitants)\b", re.I), ["population"]),
]


def query_tokens(claim: str, drop: Set[str] = frozenset()) -> Set[str]:
    base = tokens(claim) - {t.lower() for t in drop}
    for rx, words in PHRASES:
        if rx.search(claim or ""):
            base |= set(words)
    expanded = set(base)
    for t in base:
        for s in SYNONYMS.get(t, []):
            expanded |= tokens(s) or {s}
    return expanded


@dataclass
class Candidate:
    provider: str          # "census_profile" | "fred"
    id: str                # "DP05_0018E" | "MORTGAGE30US"
    title: str             # what it measures, in the agency's words
    units: str             # "percent", "dollars", "years", "Billions of Dollars", ...
    frequency: str = "annual"
    detail: str = ""       # one short line for the picker (universe, coverage)
    meta: Dict[str, str] = field(default_factory=dict)

    def line(self, key: str) -> str:
        bits = [self.title, f"units: {self.units}", self.frequency]
        if self.detail:
            bits.append(self.detail)
        return f"{key} | " + " | ".join(bits)
