"""Retrieval, not verification: give a writer the number and its citation.

`verify` answers "is this claim true", which requires the caller to already
suspect something. That is a rare motion. Looking a figure up while writing is
a constant one — and it is the same engine, pointed the other way: parse the
metric, period and geography, fetch the datapoint, and format its provenance.

The difference that matters is who chooses the question. In verification the
claim comes from a stranger's article, so narrow coverage means most claims go
unanswered. Here the user chooses the query, learns the boundary within a day,
and stops asking outside it — so the boundary has to be *legible*. Every
decline therefore names what is available instead of only what is missing.

Tier 1 only, by construction. A lookup that silently costs a model call is a
lookup someone stops trusting to be instant, and the grounded tier cannot
produce a citation with a series id and a stated period anyway — which is the
entire product.
"""
import difflib
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Dict, List, Optional

from claims.metrics import BY_METRIC, MetricSpec, all_metrics
from claims.geography import USPS, Geography, _state_from, resolve_geography
from claims.parser import parse_claim
from config import logger
from evidence.citation import render
from evidence.datapoint import Datapoint
from evidence.sources import SourceUnavailable
from evidence.datasets import finder
from services import dataset_tier
from services.verify_v2 import fetch_structured, has_structured_adapter

# Reasons a lookup produced nothing. Each names a different fix, because
# "no data" covering an outage is the failure this codebase already paid for
# once: a dead adapter and a genuinely unavailable figure are not the same
# problem and must not read the same.
METRIC_UNKNOWN = "metric_not_recognised"
METRIC_NOT_WIRED = "metric_recognised_but_not_yet_wired"
QUALIFIER_UNSUPPORTED = "qualifier_not_covered_by_series"
GEOGRAPHY_UNSUPPORTED = "no_official_series_at_that_geography"
PERIOD_UNAVAILABLE = "not_published_for_that_period"
SOURCE_DOWN = "source_unavailable"
AMBIGUOUS = "query_matches_more_than_one_series"
PLACE_UNKNOWN = "place_not_recognised"

# Terse forms people type into a search box but never write into a sentence.
# They live here rather than in claims/metrics.py because that registry indexes
# the phrases a *claim* uses: an article says "the consumer price index rose",
# it does not say "cpi". Adding these there would change how claims parse, for
# the benefit of a motion claims do not have.
_QUERY_ALIASES = {
    "debt": "national_debt",
    "jobs": "nonfarm_employment",
    "payrolls": "nonfarm_employment",
    "employment": "nonfarm_employment",
    "wages": "avg_hourly_earnings",
    "earnings": "avg_hourly_earnings",
    "lfpr": "labor_force_participation",
    "participation": "labor_force_participation",
    "u3": "unemployment_rate",
}

# Terse forms that genuinely name more than one series. Guessing between them
# is how a writer ends up citing the index when they meant the rate, so the
# lookup asks instead.
_AMBIGUOUS_QUERIES = {
    "cpi": ("inflation_rate", "cpi_index"),
    "prices": ("inflation_rate", "producer_price_inflation"),
}

# Sources with a typed-datapoint adapter today. A suggestion pointing at a
# metric that cannot be fetched is worse than no suggestion.
def is_wired(spec: MetricSpec) -> bool:
    return has_structured_adapter(spec)


def wired_metrics() -> List[MetricSpec]:
    return [s for s in all_metrics() if is_wired(s)]


@dataclass
class LookupResult:
    query: str
    ok: bool
    reason: Optional[str] = None
    headline: str = ""
    value: Optional[str] = None
    metric: Optional[str] = None
    label: Optional[str] = None
    citations: Dict[str, str] = field(default_factory=dict)
    evidence: Optional[Dict[str, Any]] = None
    # Other periods retrieved alongside the answer, so "2023" can offer 2022
    # without a second round trip.
    also: List[Dict[str, Any]] = field(default_factory=list)
    # What the user could have asked instead. Populated on every decline.
    suggestions: List[Dict[str, str]] = field(default_factory=list)
    degraded: bool = False
    model_calls: int = 0
    elapsed_seconds: float = 0.0
    # How the figure was found, when it was not by the rules alone.
    note: str = ""
    # The rules found nothing, and an explicit search (Enter / Find) would
    # have Claude look through the datasets.
    deeper: bool = False


def _suggest(query: str, limit: int = 5) -> List[Dict[str, str]]:
    """The closest fetchable metrics to what the user typed.

    Matches against every phrase in the registry rather than the canonical id,
    because people type "jobless rate", not "unemployment_rate".
    """
    phrases: Dict[str, MetricSpec] = {}
    for spec in wired_metrics():
        phrases[spec.label.lower()] = spec
        for phrase in spec.phrases:
            phrases.setdefault(phrase.lower(), spec)

    hits = difflib.get_close_matches(query.lower(), list(phrases), n=limit * 2, cutoff=0.4)
    seen, out = set(), []
    for hit in hits:
        spec = phrases[hit]
        if spec.metric in seen:
            continue
        seen.add(spec.metric)
        out.append({"metric": spec.metric, "label": spec.label,
                    "source": spec.source, "example": f"{spec.phrases[0]} 2023"})
        if len(out) >= limit:
            break
    if not out:
        # No lexical overlap at all — show the shortest labels as a menu rather
        # than returning an empty hand.
        for spec in sorted(wired_metrics(), key=lambda s: len(s.label))[:limit]:
            out.append({"metric": spec.metric, "label": spec.label,
                        "source": spec.source, "example": f"{spec.phrases[0]} 2023"})
    return out


_PERIOD_WORDS = re.compile(r"\b(19\d{2}|20\d{2}|q[1-4]|fy\d{2,4}|in|for|the|rate|of)\b",
                           re.IGNORECASE)


def _query_stem(text: str) -> str:
    """What is left of a query once the period and filler words are removed."""
    return " ".join(_PERIOD_WORDS.sub(" ", text.lower()).split())


def _to_evidence(dp: Datapoint) -> Dict[str, Any]:
    return dp.to_dict()


# Words a search box adds around a metric and a place that are neither.
_FILLER = re.compile(
    r"\b(19\d{2}|20\d{2}|q[1-4]|fy\s?\d{2,4}|what|whats|is|was|are|the|of|in|for|at|"
    r"current|currently|latest|today|now|recent|annual|yearly|monthly|average|avg|"
    r"total|number|estimate|estimated|rate|data|stats|statistics|official|how|much|many)\b",
    re.IGNORECASE)


def _with_state(rest: str) -> str:
    """"austin tx" -> "austin, tx"; "salem new york" -> "salem, new york"."""
    words = rest.replace(",", " ").split()
    for n in (2, 1):
        tail = " ".join(words[-n:])
        if len(words) > n and _state_from(tail):
            return " ".join(words[:-n]) + ", " + (tail.upper() if len(tail) == 2 else tail)
    return rest


def _titled(name: str) -> str:
    """"los angeles" -> "Los Angeles"; "austin, TX" keeps its "TX"."""
    return " ".join(w if w.isupper() else w.capitalize() for w in name.split())


def _place_in_query(text: str, parsed, spec: MetricSpec) -> Optional[Geography]:
    """The place a search-box query names, wherever it sits.

    Claims say "in Chicago"; queries say "chicago population" or "los angeles
    population", often in lower case, which the claim parser does not read
    as a place. What is left once the metric and period words are removed is
    the place. A name it cannot classify comes back at level "unknown", so
    it is looked up, never dropped: dropping it answers for the whole
    country, which is a different number from the one asked for."""
    g = parsed.geography
    if g is not None and g.level not in ("unknown", "state"):
        return g
    rest = f" {text.lower()} "
    terms = list(spec.phrases) + [spec.label]
    terms += [k for k, v in _QUERY_ALIASES.items() if v == spec.metric]
    for term in sorted(terms, key=len, reverse=True):
        rest = re.sub(rf"\b{re.escape(term.lower())}\b", " ", rest)
    rest = re.sub(r"['\u2019]s\b", " ", rest)
    rest = _FILLER.sub(" ", rest)
    rest = re.sub(r"[^\w\s,.-]", " ", rest)
    rest = re.sub(r"\s+,", ",", " ".join(rest.split())).strip(" ,.-")
    if not rest:
        return g
    if g is not None and g.level == "state":
        # "salem oregon population": the parser saw Oregon; Salem is the place.
        if g.fips == "11":
            return g  # "Washington, DC" is the District
        state = g.name.lower()
        abbr = next((k.lower() for k, v in USPS.items() if v == state), "")
        head = re.sub(rf"\b({re.escape(state)}|{abbr}|state)\b", " ", rest)
        head = " ".join(head.replace(",", " ").split()).strip(" ,.-")
        if not head:
            return g
        rest = f"{head}, {state}"
    named = _titled(_with_state(rest))
    city_of_state = ", " in named  # "Salem, Oregon" is the city, not Oregon
    for cand in (named, rest.upper() if len(rest) <= 3 else None):
        found = resolve_geography(cand) if cand else None
        if found is None or found.level == "unknown":
            continue
        if city_of_state and found.level in ("state", "nation"):
            continue
        return found
    return Geography(name=named, level="unknown")


_LEVEL_WORDS = {"nation": "the U.S.", "state": "states", "county": "counties",
                "place": "cities", "metro": "metro areas", "zcta": "ZIP codes"}


def _found(text: str, found: List[Datapoint], accessed: Optional[date]) -> LookupResult:
    best = found[0]
    return LookupResult(
        query=text, ok=True,
        metric=best.metric, label=best.label,
        value=best.display_value(),
        headline=f"{best.label}: {best.display_value()} — {best.observed.describe()}",
        citations=render(best, accessed=accessed),
        evidence=_to_evidence(best),
        also=[_to_evidence(d) for d in found[1:4]],
    )


async def _local_lookup(text: str, parsed, spec: MetricSpec,
                        accessed: Optional[date]) -> LookupResult:
    """A city, county, metro, ZIP code (or a state the fixed series skips),
    read from the same free agency APIs the checker uses. No model call."""
    where = parsed.geography.name
    try:
        dps, why = await dataset_tier.local_datapoints(text, replace(parsed, metric=spec))
    except SourceUnavailable as e:
        logger.warning("Lookup source unavailable: %s", e)
        return LookupResult(
            query=text, ok=False, reason=SOURCE_DOWN, degraded=True,
            metric=spec.metric, label=spec.label,
            headline=(f"{e.source} could not be reached ({e.reason}). This is an "
                      f"outage on our side, not a statement about the data."))
    if dps:
        return _found(text, dps, accessed)
    if why == dataset_tier.PLACE_NOT_FOUND:
        return LookupResult(
            query=text, ok=False, reason=PLACE_UNKNOWN,
            metric=spec.metric, label=spec.label,
            headline=(f"No U.S. city, county or metro area named \u201c{where}\u201d was "
                      f"found. Adding the state can help, e.g. \u201cSpringfield, IL\u201d."))
    if why == dataset_tier.NOT_PUBLISHED:
        return LookupResult(
            query=text, ok=False, reason=PERIOD_UNAVAILABLE,
            metric=spec.metric, label=spec.label,
            headline=(f"{spec.label} for {where} is not published for "
                      f"{parsed.year or 'recent years'}."))
    levels = ", ".join(_LEVEL_WORDS.get(lv, lv) for lv in spec.geo_levels)
    return LookupResult(
        query=text, ok=False, reason=GEOGRAPHY_UNSUPPORTED,
        metric=spec.metric, label=spec.label,
        headline=f"{spec.label} is not available for {where}. Available for: {levels}.",
        suggestions=_suggest(text))


# Declines Claude cannot improve on: an outage, or a choice the user must make.
_FINAL = (SOURCE_DOWN, AMBIGUOUS)


async def lookup(query: str, accessed: Optional[date] = None, *, deep: bool = False,
                 client_ip: Optional[str] = None) -> LookupResult:
    """Resolve a query to one official figure and every citation form for it.

    The rules first, free. When they find nothing and the user asked
    explicitly (`deep`: Enter or the Find button, never while typing),
    Claude looks through the same government datasets with tools. The
    figure is still read from the agency; Claude only finds it."""
    started = time.monotonic()
    r = await _by_rules(query, accessed)
    text = (query or "").strip()
    if r.ok or not text or r.reason in _FINAL or parse_claim(text).year_is_future:
        return r
    if not finder.available() or finder.gate(text):
        return r
    if not deep:
        r.deeper = True
        return r
    found = await finder.find(text, "lookup", client_ip=client_ip)
    if found.datapoint is None:
        r.model_calls = found.model_calls
        r.headline += " Claude also searched the government datasets and found no figure for it."
        r.elapsed_seconds = round(time.monotonic() - started, 2)
        return r
    out = _found(text, [found.datapoint], accessed)
    out.model_calls = found.model_calls
    agency = (out.citations or {}).get("agency") or found.datapoint.source
    out.note = (f"Found by Claude in {agency} data."
                + ("" if found.exact else " It is the closest official measure, not an exact match."))
    out.elapsed_seconds = round(time.monotonic() - started, 2)
    return out


async def _by_rules(query: str, accessed: Optional[date] = None) -> LookupResult:
    """The free path: parse, fetch, cite. No model call."""
    started = time.monotonic()
    text = (query or "").strip()

    def done(result: LookupResult) -> LookupResult:
        result.elapsed_seconds = round(time.monotonic() - started, 2)
        return result

    if not text:
        return done(LookupResult(
            query=text, ok=False, reason=METRIC_UNKNOWN,
            headline="Type a metric and a period, e.g. \"unemployment 2023\".",
            suggestions=_suggest("", limit=5),
        ))

    parsed = parse_claim(text)
    spec_override = None

    if parsed.metric is None:
        # Strip period and filler words so "debt 2023" reduces to "debt".
        stem = _query_stem(text)
        if stem in _AMBIGUOUS_QUERIES:
            options = [BY_METRIC[m] for m in _AMBIGUOUS_QUERIES[stem] if m in BY_METRIC]
            return done(LookupResult(
                query=text, ok=False, reason=AMBIGUOUS,
                headline=f"\u201c{stem}\u201d names more than one series. Which did you mean?",
                suggestions=[{"metric": o.metric, "label": o.label, "source": o.source,
                              "example": f"{o.phrases[0]} {parsed.year or 2023}"}
                             for o in options],
            ))
        alias = _QUERY_ALIASES.get(stem)
        if alias and alias in BY_METRIC:
            spec_override = BY_METRIC[alias]

    if parsed.metric is None and spec_override is None:
        return done(LookupResult(
            query=text, ok=False, reason=METRIC_UNKNOWN,
            headline=f"No official series matches “{text}”.",
            suggestions=_suggest(text),
        ))

    spec = spec_override or parsed.metric

    if not is_wired(spec):
        return done(LookupResult(
            query=text, ok=False, reason=METRIC_NOT_WIRED,
            metric=spec.metric, label=spec.label,
            headline=(
                f"{spec.label} comes from {spec.source}, which is not wired up "
                f"for lookup yet."
            ),
            suggestions=_suggest(text),
        ))

    if parsed.structured_blocker:
        return done(LookupResult(
            query=text, ok=False, reason=QUALIFIER_UNSUPPORTED,
            metric=spec.metric, label=spec.label,
            headline=f"{spec.label} is only wired up as a total: {parsed.structured_blocker}.",
            suggestions=_suggest(text),
        ))

    if parsed.year is not None and spec.min_year and parsed.year < spec.min_year:
        return done(LookupResult(
            query=text, ok=False, reason=PERIOD_UNAVAILABLE,
            metric=spec.metric, label=spec.label,
            headline=f"{spec.label} is only wired up from {spec.min_year}.",
        ))

    if parsed.year_is_future:
        return done(LookupResult(
            query=text, ok=False, reason=PERIOD_UNAVAILABLE,
            metric=spec.metric, label=spec.label,
            headline=f"{parsed.year} has not been published yet.",
        ))

    geo = _place_in_query(text, parsed, spec)
    if geo is not parsed.geography:
        parsed = replace(parsed, geography=geo)
    if geo is not None and geo.level != "nation" and geo.level not in spec.geo_levels:
        return done(await _local_lookup(text, parsed, spec, accessed))

    try:
        found = await fetch_structured(
            replace(parsed, metric=spec) if spec is not parsed.metric else parsed
        )
    except SourceUnavailable as e:
        # Explicitly NOT "no data for that period". The distinction is the
        # whole reason this branch exists.
        logger.warning("Lookup source unavailable: %s", e)
        return done(LookupResult(
            query=text, ok=False, reason=SOURCE_DOWN, degraded=True,
            metric=spec.metric, label=spec.label,
            headline=(
                f"{e.source} could not be reached ({e.reason}). This is an "
                f"outage on our side, not a statement about the data."
            ),
        ))

    if not found:
        return done(LookupResult(
            query=text, ok=False, reason=PERIOD_UNAVAILABLE,
            metric=spec.metric, label=spec.label,
            headline=(
                f"{spec.label} is not published for "
                f"{parsed.year if parsed.year else 'that period'}."
            ),
        ))

    return done(_found(text, found, accessed))


def to_response_dict(r: LookupResult) -> Dict[str, Any]:
    return {
        "query": r.query,
        "ok": r.ok,
        "reason": r.reason,
        "headline": r.headline,
        "value": r.value,
        "metric": r.metric,
        "label": r.label,
        "citations": r.citations,
        "evidence": r.evidence,
        "also": r.also,
        "suggestions": r.suggestions,
        "degraded": r.degraded,
        "model_calls": r.model_calls,
        "elapsed_seconds": r.elapsed_seconds,
        "note": r.note,
        "deeper": r.deeper,
    }
