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
    GEOGRAPHY_UNSUPPORTED, HIGH, LOW, MEDIUM, MISLEADING, NO_SERIES, NO_VERDICT,
    NOT_EMPIRICAL, PERIOD_UNAVAILABLE, SUPPORTED, CONTRADICTED, Verdict, assess,
)
from config import logger
from evidence.datapoint import Datapoint
from evidence.grounded import GroundedAnswer, ground_claim
from evidence.sources import SourceUnavailable
from evidence.sources import bls as bls_source
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

    # BEA and Census are wired through the legacy adapters and are not yet
    # migrated to typed datapoints; routing them to grounded search is more
    # accurate than returning a figure whose period cannot be verified.
    return []


def _structured_is_applicable(parsed: ParsedClaim) -> Optional[str]:
    """Why Tier 1 cannot handle this claim, or None if it can."""
    if parsed.metric is None:
        return NO_SERIES
    if parsed.geography is not None:
        if parsed.geography.level not in parsed.metric.geo_levels:
            return GEOGRAPHY_UNSUPPORTED
    if parsed.year_is_future:
        return PERIOD_UNAVAILABLE
    if parsed.metric.source in ("BEA", "CENSUS"):
        return NO_SERIES
    return None


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
                g = await ground_claim(text, context_hint=hint)
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
    if blocked == GEOGRAPHY_UNSUPPORTED and parsed.geography:
        if not parsed.geography.is_known:
            hint = (
                f"The claim is about {parsed.geography.name}, which is not a U.S. "
                f"geography. Every structured series here is U.S.-only, so find the "
                f"figure published by that jurisdiction's own statistical agency."
            )
        else:
            hint = (
                f"The claim is about {parsed.geography.name}, which is below the level "
                f"the official series covers. Find a figure for that specific place."
            )

    g = await ground_claim(text, context_hint=hint)
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
