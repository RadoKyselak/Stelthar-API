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
from claims.parser import parse_claim
from config import logger
from evidence.citation import render
from evidence.datapoint import Datapoint
from evidence.sources import SourceUnavailable
from services.verify_v2 import fetch_structured

# Reasons a lookup produced nothing. Each names a different fix, because
# "no data" covering an outage is the failure this codebase already paid for
# once: a dead adapter and a genuinely unavailable figure are not the same
# problem and must not read the same.
METRIC_UNKNOWN = "metric_not_recognised"
METRIC_NOT_WIRED = "metric_recognised_but_not_yet_wired"
GEOGRAPHY_UNSUPPORTED = "no_official_series_at_that_geography"
PERIOD_UNAVAILABLE = "not_published_for_that_period"
SOURCE_DOWN = "source_unavailable"
AMBIGUOUS = "query_matches_more_than_one_series"

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
_WIRED_SOURCES = ("BLS", "TREASURY", "USASPENDING")


def is_wired(spec: MetricSpec) -> bool:
    return spec.source in _WIRED_SOURCES


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


async def lookup(query: str, accessed: Optional[date] = None) -> LookupResult:
    """Resolve a query to one official figure and every citation form for it."""
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

    if parsed.geography is not None and parsed.geography.level not in spec.geo_levels:
        where = parsed.geography.name
        return done(LookupResult(
            query=text, ok=False, reason=GEOGRAPHY_UNSUPPORTED,
            metric=spec.metric, label=spec.label,
            headline=(
                f"{spec.label} is not published for {where}. Available at: "
                f"{', '.join(spec.geo_levels)}."
            ),
            suggestions=_suggest(text),
        ))

    if parsed.year_is_future:
        return done(LookupResult(
            query=text, ok=False, reason=PERIOD_UNAVAILABLE,
            metric=spec.metric, label=spec.label,
            headline=f"{parsed.year} has not been published yet.",
        ))

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

    best = found[0]
    citations = render(best, accessed=accessed)
    return done(LookupResult(
        query=text, ok=True,
        metric=best.metric, label=best.label,
        value=best.display_value(),
        headline=f"{best.label}: {best.display_value()} — {best.observed.describe()}",
        citations=citations,
        evidence=_to_evidence(best),
        also=[_to_evidence(d) for d in found[1:4]],
    ))


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
    }
