"""The tiered verification pipeline.

    Tier 1  deterministic parse -> free government API   0 model calls, ~1s
    Tier 2  grounded Google Search via Gemini            1 model call,  ~4s

The old pipeline ran a model-planned agentic loop for every claim: plan,
retrieve, critique, re-plan, synthesise, embed — four to eight Gemini calls
before answering anything, with a p95 of 10-40 seconds. On a free tier of a few
hundred requests a day that is roughly sixty claims before the quota is gone,
and the planner was a single point of total failure: it had to guess a source,
table, line code, year and geography before any evidence existed, and returned
"Inconclusive" whenever it guessed wrong.

Inverting the order fixes both problems at once. Structured lookups are exact
but narrow, so they run first and cost nothing when a deterministic matcher
recognises the metric. Search is broad but approximate, so it catches everything
else in a single call. Neither is a fallback for a failure of the other; they
answer different kinds of question.

`model_calls` is reported on every response. A pipeline whose cost is invisible
is one whose cost grows.
"""
import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from claims.metrics import MetricSpec
from claims.parser import ParsedClaim, parse_claim
from claims.verdict import (
    ALTERNATE_MEASURE, GEOGRAPHY_UNSUPPORTED, HIGH, LOW, MEDIUM, MISLEADING, NO_SERIES, NO_VERDICT,
    NOT_EMPIRICAL, PERIOD_UNAVAILABLE, SUPPORTED, CONTRADICTED, Verdict, assess,
)
import config
from config import logger
from evidence.datapoint import Datapoint
from evidence.grounded import GroundedAnswer, ground_claim
from evidence.sources import SourceUnavailable
from evidence.sources import bea as bea_source
from evidence.sources import bls as bls_source
from evidence.sources import census as census_source
from evidence.sources import treasury as treasury_source
from evidence.sources import usaspending as usaspending_source

TIER_STRUCTURED = "structured"
TIER_GROUNDED = "grounded"
TIER_NONE = "none"


@dataclass
class VerificationV2:
    claim: str
    verdict: str
    confidence: str
    headline: str
    reason: Optional[str] = None
    claim_value: Optional[str] = None
    official_value: Optional[str] = None
    delta: Optional[str] = None
    relative_delta: Optional[float] = None
    caveats: List[str] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    citations: List[Dict[str, Any]] = field(default_factory=list)
    # Operational transparency.
    tier: str = TIER_NONE
    model_calls: int = 0
    elapsed_seconds: float = 0.0
    parsed: Dict[str, Any] = field(default_factory=dict)
    # Required by the Gemini grounding Terms of Service when citations are shown.
    search_suggestions_html: Optional[str] = None
    notes: List[str] = field(default_factory=list)


async def fetch_structured(parsed: ParsedClaim) -> List[Datapoint]:
    """Dispatch to the right free government API. No model call."""
    spec: MetricSpec = parsed.metric
    year = parsed.year

    if spec.source == "TREASURY":
        return await treasury_source.fetch_national_debt(
            year=year, year_basis=parsed.year_basis
        )

    if spec.source == "BLS":
        if year is None:
            # A present-tense claim wants the most recent complete year.
            from datetime import datetime, timezone
            year = datetime.now(timezone.utc).year - 1
        if spec.bls_kind == "cpi_yoy":
            return await bls_source.fetch_inflation(
                spec.metric, spec.label, spec.series_id, year, month=parsed.month
            )
        if parsed.month is not None:
            return await bls_source.fetch_monthly(
                spec.metric, spec.label, spec.series_id, spec.family, year, parsed.month
            )
        got = await bls_source.fetch_annual(
            spec.metric, spec.label, spec.series_id, spec.family, year
        )
        if not got and parsed.year is None:
            # Latest complete year is not out yet; step back one.
            got = await bls_source.fetch_annual(
                spec.metric, spec.label, spec.series_id, spec.family, year - 1
            )
        return got

    if spec.source == "CENSUS":
        geo = parsed.geography
        if geo is not None and geo.level != "state":
            geo = None  # national; finer levels are blocked before this point
        return await census_source.fetch(spec, year, geo)

    if spec.source == "BEA":
        return await bea_source.fetch(spec, year)

    return []


def has_structured_adapter(spec: Optional[MetricSpec]) -> bool:
    """True when a typed adapter can fetch this metric today."""
    if spec is None or not spec.structured:
        return False
    if spec.source in ("BLS", "TREASURY"):
        return True
    # Census and BEA refuse keyless requests. Without a key the claim goes to
    # grounded search, as it did before these adapters existed, rather than
    # every such claim reporting an outage.
    if spec.source == "CENSUS":
        return bool(spec.census_variable and config.CENSUS_API_KEY)
    if spec.source == "BEA":
        return bool(spec.bea_table and spec.bea_line and config.BEA_API_KEY)
    return False


def _structured_is_applicable(parsed: ParsedClaim) -> Optional[str]:
    """Why Tier 1 cannot handle this claim, or None if it can."""
    if parsed.metric is None:
        return NO_SERIES
    if parsed.geography is not None:
        if parsed.geography.level not in parsed.metric.geo_levels:
            return GEOGRAPHY_UNSUPPORTED
    if parsed.year_is_future:
        return PERIOD_UNAVAILABLE
    if not has_structured_adapter(parsed.metric):
        return NO_SERIES
    if parsed.structured_blocker:
        return NO_SERIES
    if parsed.year is not None and parsed.metric.min_year and parsed.year < parsed.metric.min_year:
        return NO_SERIES
    return None


def _soften_for_alternate_measure(parsed: ParsedClaim, v: Verdict) -> Verdict:
    """A mismatch against one of two official measures is not a contradiction.

    Census headlines median household income and the poverty rate from CPS
    ASEC, while the wired series is ACS. They differ by design ($80,610 vs
    $77,719 for 2023 median income), so a claim quoting the CPS figure is
    correct and must not be reported as Contradicted on the ACS number alone.
    """
    alt = parsed.metric.alternate_measure
    return Verdict(
        verdict=NO_VERDICT, confidence=LOW, reason=ALTERNATE_MEASURE,
        headline=(
            f"{v.headline} Census also publishes {alt}, which differs from the ACS "
            f"figure by design; the claim may be quoting that one."
        ),
        claimed_display=v.claimed_display, official_display=v.official_display,
        delta_display=v.delta_display, relative_delta=v.relative_delta,
        evidence=v.evidence,
        caveats=v.caveats + [f"Not marked wrong on the ACS figure alone: Census also publishes {alt}."],
    )


async def _safe_ground(text: str, hint: str) -> GroundedAnswer:
    """ground_claim, but an unexpected exception is an outage, never a 500."""
    try:
        return await ground_claim(text, context_hint=hint)
    except Exception as e:  # noqa: BLE001 — verify() promises never to raise
        logger.exception("Grounded search raised for %r", text[:120])
        return GroundedAnswer(ok=False, error=f"internal error: {type(e).__name__}")


def _grounded_to_result(
    claim: str, parsed: ParsedClaim, g: GroundedAnswer, started: float
) -> VerificationV2:
    verdict_map = {
        "SUPPORTED": SUPPORTED,
        "CONTRADICTED": CONTRADICTED,
        "MISLEADING": MISLEADING,
        "NO_EVIDENCE": NO_VERDICT,
        "NOT_EMPIRICAL": NO_VERDICT,
    }
    verdict = verdict_map.get(g.verdict, NO_VERDICT)
    reason = None
    if g.verdict == "NOT_EMPIRICAL":
        reason = NOT_EMPIRICAL
    elif g.verdict == "NO_EVIDENCE":
        reason = NO_SERIES

    # Confidence from what the grounding actually returned, not from how
    # decisive the model sounded.
    grounded_share = g.grounded_fraction
    if g.has_official_source and grounded_share >= 0.5:
        confidence = MEDIUM
    else:
        confidence = LOW
    if verdict == NO_VERDICT:
        confidence = LOW

    caveats: List[str] = []
    if not g.has_official_source and g.citations:
        caveats.append(
            "No primary government source was found; this rests on secondary reporting."
        )
    if g.citations and grounded_share < 0.5:
        caveats.append(
            f"Only {grounded_share:.0%} of the answer text is backed by a retrieved source."
        )
    if not g.citations:
        caveats.append("The model returned no citations for this answer.")

    headline = g.explanation or g.answer_text.strip()[:400]
    if g.official_value:
        period = f" ({g.period})" if g.period else ""
        org = f" — {g.source_org}" if g.source_org else ""
        headline = f"Reported figure: {g.official_value}{period}{org}. {headline}"

    return VerificationV2(
        claim=claim,
        verdict=verdict,
        confidence=confidence,
        headline=headline,
        reason=reason,
        official_value=g.official_value or None,
        caveats=caveats,
        citations=[
            {
                "title": c.title, "url": c.uri, "domain": c.domain,
                "is_official_source": c.official,
            }
            for c in g.citations
        ],
        tier=TIER_GROUNDED,
        model_calls=1,
        elapsed_seconds=round(time.monotonic() - started, 2),
        parsed=_parsed_summary(parsed),
        search_suggestions_html=g.search_suggestions_html or None,
        notes=[f"search queries: {', '.join(g.search_queries)}"] if g.search_queries else [],
    )


def _parsed_summary(parsed: ParsedClaim) -> Dict[str, Any]:
    return {
        "metric": parsed.metric.metric if parsed.metric else None,
        "metric_label": parsed.metric.label if parsed.metric else None,
        "year": parsed.year,
        "year_basis": parsed.year_basis,
        "month": parsed.month,
        "geography": parsed.geography.name if parsed.geography else None,
        "geography_level": parsed.geography.level if parsed.geography else None,
        "claimed_value": parsed.claimed_value.magnitude if parsed.claimed_value else None,
        "comparator": parsed.comparator,
        "is_trend_claim": parsed.is_trend_claim,
    }


def _verdict_to_result(
    claim: str, parsed: ParsedClaim, v: Verdict, started: float
) -> VerificationV2:
    return VerificationV2(
        claim=claim,
        verdict=v.verdict,
        confidence=v.confidence,
        headline=v.headline,
        reason=v.reason or None,
        claim_value=v.claimed_display or None,
        official_value=v.official_display or None,
        delta=v.delta_display or None,
        relative_delta=v.relative_delta,
        caveats=v.caveats,
        evidence=[d.to_dict() for d in v.evidence],
        tier=TIER_STRUCTURED,
        model_calls=0,
        elapsed_seconds=round(time.monotonic() - started, 2),
        parsed=_parsed_summary(parsed),
    )


async def verify(claim: str, allow_grounding: bool = True) -> VerificationV2:
    """Verify a claim. Never raises; every failure becomes a stated reason."""
    started = time.monotonic()
    text = (claim or "").strip()
    if not text:
        return VerificationV2(
            claim="", verdict=NO_VERDICT, confidence=LOW,
            headline="No claim was provided.", reason="claim_states_no_checkable_value",
        )

    parsed = parse_claim(text)
    blocked = _structured_is_applicable(parsed)
    structured_ran = False

    if blocked is None:
        structured_ran = True
        try:
            evidence = await fetch_structured(parsed)
        except SourceUnavailable as e:
            # Do not fall through to "not published". The source failed; say so.
            logger.warning("Source unavailable for %r: %s", text[:80], e)
            return VerificationV2(
                claim=text, verdict=NO_VERDICT, confidence=LOW,
                headline=(
                    f"Verification could not be completed: {e.source} was "
                    f"unavailable ({e.reason}). This is not a judgment about the claim."
                ),
                reason="source_unavailable",
                tier=TIER_NONE,
                elapsed_seconds=round(time.monotonic() - started, 2),
                parsed=_parsed_summary(parsed),
                notes=[f"degraded: {e.source}"],
            )
        except Exception:
            # An unexpected failure here is OUR bug, not an absence of data.
            # Falling through to evidence=[] would report it as "not published".
            logger.exception("Structured lookup failed for %r", text[:120])
            return VerificationV2(
                claim=text, verdict=NO_VERDICT, confidence=LOW,
                headline=("Verification could not be completed due to an internal "
                          "error. This is not a judgment about the claim."),
                reason="source_unavailable",
                tier=TIER_NONE,
                elapsed_seconds=round(time.monotonic() - started, 2),
                parsed=_parsed_summary(parsed),
                notes=["degraded: internal error in structured lookup"],
            )

        if evidence:
            verdict = assess(parsed, evidence)
            if verdict.verdict == CONTRADICTED and parsed.metric.alternate_measure:
                verdict = _soften_for_alternate_measure(parsed, verdict)
            # A structured answer that landed cleanly is the best outcome: exact
            # number, stated period, zero model calls.
            if verdict.verdict != NO_VERDICT or not allow_grounding:
                return _verdict_to_result(text, parsed, verdict, started)
            # Otherwise fall through — grounding may still resolve it, and the
            # structured figure is carried along as context.
            hint = (
                f"An official figure was retrieved but did not settle the claim: "
                f"{verdict.headline}"
            )
            if allow_grounding:
                g = await _safe_ground(text, hint)
                if g.ok:
                    result = _grounded_to_result(text, parsed, g, started)
                    result.evidence = [d.to_dict() for d in verdict.evidence]
                    result.notes.append("structured lookup ran first and did not settle it")
                    return result
            return _verdict_to_result(text, parsed, verdict, started)

    # A series that exists but has no row for the requested period is a
    # different situation from having no series at all, and the caller can act
    # on the difference: one resolves by waiting for publication, the other
    # never resolves. Collapsing both into "Inconclusive" is exactly what made
    # the old output useless.
    if structured_ran and parsed.metric is not None:
        unresolved_reason = PERIOD_UNAVAILABLE
        unresolved_headline = (
            f"{parsed.metric.label} is a wired-up series, but no figure is "
            f"published for {parsed.year or 'the requested period'} yet."
        )
    else:
        unresolved_reason = blocked or NO_SERIES
        unresolved_headline = "No wired-up official series covers this claim."

    if not allow_grounding:
        return VerificationV2(
            claim=text, verdict=NO_VERDICT, confidence=LOW,
            headline=unresolved_headline,
            reason=unresolved_reason,
            tier=TIER_NONE,
            elapsed_seconds=round(time.monotonic() - started, 2),
            parsed=_parsed_summary(parsed),
        )

    hint = ""
    if parsed.structured_blocker and parsed.metric is not None:
        hint = (
            f"Do not answer with the all-population annual total for "
            f"{parsed.metric.label.lower()}: {parsed.structured_blocker}. Find the "
            f"figure for exactly what the claim describes."
        )
    if blocked == GEOGRAPHY_UNSUPPORTED and parsed.geography:
        if not parsed.geography.is_known:
            hint = (
                f"The claim names \"{parsed.geography.name}\", which could not be "
                f"resolved to the United States or a U.S. state, so no U.S. national "
                f"series was used. If it is a place outside the U.S. (or a city, or the "
                f"world), find the figure from that jurisdiction's own statistical agency."
            )
        else:
            hint = (
                f"The claim is about {parsed.geography.name}, which is below the level "
                f"the official series covers. Find a figure for that specific place."
            )

    g = await _safe_ground(text, hint)
    if not g.ok:
        return VerificationV2(
            claim=text, verdict=NO_VERDICT, confidence=LOW,
            headline=f"Could not retrieve evidence for this claim ({g.error}).",
            reason=unresolved_reason,
            tier=TIER_NONE, model_calls=1,
            elapsed_seconds=round(time.monotonic() - started, 2),
            parsed=_parsed_summary(parsed),
        )
    return _grounded_to_result(text, parsed, g, started)


def to_response_dict(v: VerificationV2) -> Dict[str, Any]:
    return asdict(v)
