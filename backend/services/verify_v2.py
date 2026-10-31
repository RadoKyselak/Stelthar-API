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
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, List, Optional

from claims.metrics import MetricSpec
from claims.parser import ParsedClaim, parse_claim
from claims.verdict import (
    ALTERNATE_MEASURE, GEOGRAPHY_UNSUPPORTED, HIGH, LOW, MEDIUM, MISLEADING, MOSTLY_ACCURATE,
    NO_SERIES, NO_VERDICT, NOT_EMPIRICAL, PERIOD_UNAVAILABLE, SUPPORTED, CONTRADICTED, Verdict, assess,
    _phrase_for,
)
import config
from config import logger
from evidence.datapoint import Datapoint
from evidence import ai_search
from evidence.grounded import GroundedAnswer, ground_claim  # noqa: F401 (tests patch it)
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
    # One entry per figure compared: the claim's number beside the official
    # one, with period, place and the size of any gap. Two for a two-point claim.
    points: List[Dict[str, Any]] = field(default_factory=list)
    # Web-search answers: the figure the search reported, kept apart from the
    # explanation so a client can show each once.
    reported: Dict[str, str] = field(default_factory=dict)
    # Other outlets' published fact-checks of similarly worded claims (Google
    # Fact Check Tools). Shown beside the verdict; never used to reach it.
    fact_checks: List[Dict[str, str]] = field(default_factory=list)


async def fetch_structured(parsed: ParsedClaim) -> List[Datapoint]:
    """Dispatch to the right free government API. No model call."""
    spec: MetricSpec = parsed.metric
    year = parsed.year

    if spec.source == "TREASURY":
        if spec.treasury_metric in ("deficit", "receipts", "outlays"):
            return await treasury_source.fetch_budget_total(spec.treasury_metric, year)
        return await treasury_source.fetch_national_debt(
            year=year, year_basis=parsed.year_basis, month=parsed.month
        )

    if spec.source == "BLS":
        series_id = spec.series_id
        geo = parsed.geography
        if geo is not None and geo.level == "state" and geo.fips and spec.state_series_annual:
            template = spec.state_series_monthly if parsed.month else spec.state_series_annual
            series_id = template.format(fips=geo.fips)
        if spec.bls_kind == "price" and year is None:
            # "Gas is $3 a gallon" is about now: the latest monthly price.
            return await bls_source.fetch_latest_monthly(
                spec.metric, spec.label, series_id, spec.family
            )
        spec = replace(spec, series_id=series_id)
        if spec.bls_kind == "change":
            if year is None:
                return []  # "added jobs" with no period: nothing to subtract
            return await bls_source.fetch_jobs_change(
                spec.metric, spec.label, spec.series_id, year, parsed.month)
        if year is None and parsed.present and parsed.month is None:
            # "Unemployment is 4%" is about the latest month, not last year's average.
            if spec.bls_kind == "cpi_yoy":
                latest = await bls_source.fetch_latest_monthly(
                    spec.metric, spec.label, spec.series_id, spec.family)
                if latest:
                    obs = latest[0].observed
                    got = await bls_source.fetch_inflation(
                        spec.metric, spec.label, spec.series_id, obs.year, month=obs.month)
                    if got:
                        return got
            elif spec.bls_kind == "rate":
                if geo is not None and geo.level == "state" and geo.fips and spec.state_series_monthly:
                    spec = replace(spec, series_id=spec.state_series_monthly.format(fips=geo.fips))
                got = await bls_source.fetch_latest_monthly(
                    spec.metric, spec.label, spec.series_id, spec.family)
                if got:
                    return got
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

    if spec.source == "FRED":
        from evidence.sources import fred_free
        claimed = parsed.claimed_value.magnitude if parsed.claimed_value else None
        return await fred_free.fetch(spec.metric, parsed.text, claimed, year, parsed.month,
                                     parsed.present or year is None)

    if spec.source == "BEA":
        if parsed.quarter is not None and year is not None:
            return await bea_source.fetch_quarter(spec, year, parsed.quarter)
        return await bea_source.fetch(spec, year)

    return []


def has_structured_adapter(spec: Optional[MetricSpec]) -> bool:
    """True when a typed adapter can fetch this metric today."""
    if spec is None or not spec.structured:
        return False
    if spec.source in ("BLS", "TREASURY", "FRED"):
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


def _alternate_measure_applies(parsed: ParsedClaim, v: Verdict) -> bool:
    """Only a near-miss can be explained by a different official survey.

    Census's CPS and ACS medians differ by a few percent; a claim of $10 gas
    against $3.30 is not a survey difference, it is wrong."""
    spec = parsed.metric
    if not (spec and spec.alternate_measure and v.verdict in (CONTRADICTED, MOSTLY_ACCURATE)):
        return False
    if v.relative_delta is None:
        return False
    return abs(v.relative_delta) <= spec.alternate_band


def _soften_for_alternate_measure(parsed: ParsedClaim, v: Verdict) -> Verdict:
    """A near-miss against one of several official measures is not a
    contradiction. Census headlines median household income and the poverty
    rate from CPS ASEC while the wired series is ACS ($80,610 vs $77,719 for
    2023 median income); EIA and BLS publish different gas price averages."""
    if not _alternate_measure_applies(parsed, v):
        return v
    alt = parsed.metric.alternate_measure
    source = v.evidence[0].source if v.evidence else "the agency"
    return Verdict(
        verdict=NO_VERDICT, confidence=LOW, reason=ALTERNATE_MEASURE,
        headline=(
            f"{v.headline} Another official measure also exists ({alt}); it differs "
            f"from the {source} figure by design, and the claim may be quoting it."
        ),
        claimed_display=v.claimed_display, official_display=v.official_display,
        delta_display=v.delta_display, relative_delta=v.relative_delta,
        evidence=v.evidence,
        caveats=v.caveats + [f"Not marked wrong on the {source} figure alone: another "
                             f"official measure exists ({alt})."],
    )


async def _safe_ground(text: str, hint: str, client_ip: Optional[str] = None,
                       names_a_metric: bool = False) -> GroundedAnswer:
    """The paid AI tier, behind its gate and spend limits. Never raises."""
    try:
        return await ai_search.search(text, hint, client_ip=client_ip,
                                      names_a_metric=names_a_metric)
    except Exception as e:  # noqa: BLE001 — verify() promises never to raise
        logger.exception("Grounded search raised for %r", text[:120])
        return GroundedAnswer(ok=False, error=f"internal error: {type(e).__name__}")


NO_SOURCE = "no_source_found"


def _grounded_to_result(
    claim: str, parsed: ParsedClaim, g: GroundedAnswer, started: float
) -> VerificationV2:
    # A government-data fact checker does not hand down a verdict it cannot
    # source. An AI answer with no retrieved page behind it is not shown as
    # Supported / Contradicted, however plausible it sounds.
    if not g.citations and g.verdict in ("SUPPORTED", "MOSTLY_ACCURATE", "CONTRADICTED",
                                         "MISLEADING"):
        return VerificationV2(
            claim=claim, verdict=NO_VERDICT, confidence=LOW,
            headline=("No source could be found to check this claim against, so no "
                      "verdict is given."),
            reason=NO_SOURCE, tier=TIER_GROUNDED, model_calls=g.model_calls,
            elapsed_seconds=round(time.monotonic() - started, 2),
            parsed=_parsed_summary(parsed),
        )
    verdict_map = {
        "SUPPORTED": SUPPORTED,
        "MOSTLY_ACCURATE": MOSTLY_ACCURATE,
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
    grounded_share = g.grounded_fraction if g.span_attribution else 1.0
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
    if g.citations and g.span_attribution and grounded_share < 0.5:
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
                "quote": getattr(c, "quote", "") or "",
            }
            for c in g.citations
        ],
        tier=TIER_GROUNDED,
        model_calls=g.model_calls,
        elapsed_seconds=round(time.monotonic() - started, 2),
        parsed=_parsed_summary(parsed),
        search_suggestions_html=g.search_suggestions_html or None,
        notes=[f"search queries: {', '.join(g.search_queries)}"] if g.search_queries else [],
        reported={"value": g.official_value or "", "period": g.period or "",
                  "source_org": g.source_org or "", "explanation": g.explanation or ""},
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


def _point(v: Verdict, label: Optional[str] = None) -> Dict[str, Any]:
    """The claim's figure beside the official one, for display."""
    from evidence.citation import clean_basis, render as render_citation, when
    dp = v.evidence[0]
    phrase = _phrase_for(v.comparator)
    claim = f"{phrase} {v.claimed_display}".strip() if v.claimed_display else ""
    cite = render_citation(dp)
    return {
        "label": label,
        "verdict": v.verdict,
        "claim": claim,
        "official": dp.display_value(),
        "period": when(dp.observed),
        "basis": clean_basis(dp.observed.basis or ""),
        "place": dp.geography.name,
        "source": dp.source,
        "gap": v.gap_display,
        "comparator": v.comparator,
        "measure": dp.label,
        "citation": cite["note"],
        "citation_html": cite["note_html"],
        "citation_inline": cite["inline"],
        "url": cite["url"],
        "excerpt": _excerpt(dp),
    }


def _excerpt(dp) -> Optional[Dict[str, Any]]:
    """The published rows the figure was read from, headed the way the agency
    labels them: "Local Area Unemployment Statistics · series LAUST48… · Texas"."""
    from evidence.citation import clean_basis, dataset_name, identifier
    ex = (dp.extra or {}).get("excerpt")
    if not ex:
        return None
    sid = dp.series_id or ""
    head = [dataset_name(dp.source, sid, clean_basis(dp.observed.basis or "")),
            identifier(dp.source, sid, table=(dp.extra or {}).get("table"), as_of=dp.as_of)]
    if dp.geography.name and dp.geography.name != "United States":
        head.append(dp.geography.name)
    return {**ex, "heading": " · ".join(h for h in head if h)}


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
        caveats=list(v.caveats or []) + ([parsed.aside] if parsed.aside and v.evidence else []),
        evidence=[d.to_dict() for d in v.evidence],
        tier=TIER_STRUCTURED,
        model_calls=0,
        elapsed_seconds=round(time.monotonic() - started, 2),
        parsed=_parsed_summary(parsed),
        points=[_point(v)] if v.evidence else [],
    )


_TIER_RANK = {LOW: 0, MEDIUM: 1, HIGH: 2}


def _is_two_point(parsed: ParsedClaim) -> bool:
    """"Unemployment fell from 8.1% in 2020 to 3.6% in 2023": two years, two
    values, each checkable on its own. Pairing is by order of appearance,
    which matches how these claims are written ("from X in A to Y in B",
    "X in B, down from Y in A")."""
    return (
        parsed.metric is not None
        and len(parsed.all_years or []) == 2
        and len(parsed.all_values or []) == 2
        and parsed.comparator == "equals"
        and parsed.month is None and parsed.quarter is None
        and not parsed.has_unresolved_period
    )


async def _verify_two_points(text: str, parsed: ParsedClaim, started: float) -> Optional[VerificationV2]:
    spec = parsed.metric
    verdicts: List[Verdict] = []
    for year, value in zip(parsed.all_years, parsed.all_values):
        if value.family == "unknown":
            value = replace(value, family=spec.family)
        sub = replace(parsed, year=year, claimed_value=value, all_years=[year],
                      all_values=[value],
                      year_is_future=bool(parsed.year_basis == "calendar" and year > datetime_now_year()))
        if sub.year_is_future:
            return None
        try:
            evidence = await fetch_structured(sub)
        except SourceUnavailable:
            return None  # the normal path reports the outage
        except Exception:
            logger.exception("Two-point lookup failed for %r", text[:120])
            return None
        v = assess(sub, evidence)
        if v.verdict != MOSTLY_ACCURATE:
            v = _soften_for_alternate_measure(sub, v)
        verdicts.append(v)

    if any(v.verdict == NO_VERDICT for v in verdicts):
        verdict, reason = NO_VERDICT, next(v.reason for v in verdicts if v.verdict == NO_VERDICT)
    elif any(v.verdict == CONTRADICTED for v in verdicts):
        verdict, reason = CONTRADICTED, None
    elif any(v.verdict == MOSTLY_ACCURATE for v in verdicts):
        verdict, reason = MOSTLY_ACCURATE, None
    else:
        verdict, reason = SUPPORTED, None

    headline = " ".join(f"{y}: {v.headline}" for y, v in zip(parsed.all_years, verdicts))
    caveats: List[str] = []
    for v in verdicts:
        for c in v.caveats:
            if c not in caveats:
                caveats.append(c)
    evidence = [d.to_dict() for v in verdicts for d in v.evidence]
    confidence = min((v.confidence for v in verdicts), key=lambda t: _TIER_RANK.get(t, 0))
    return VerificationV2(
        claim=text, verdict=verdict, confidence=confidence if verdict != NO_VERDICT else LOW,
        headline=headline, reason=reason, caveats=caveats, evidence=evidence,
        claim_value=" → ".join(v.claimed_display for v in verdicts if v.claimed_display) or None,
        official_value=" → ".join(v.official_display for v in verdicts if v.official_display) or None,
        tier=TIER_STRUCTURED, model_calls=0,
        elapsed_seconds=round(time.monotonic() - started, 2),
        parsed=_parsed_summary(parsed),
        notes=["two-point claim: each value checked against its own year"],
        points=[_point(v, str(y)) for y, v in zip(parsed.all_years, verdicts) if v.evidence],
    )


def datetime_now_year() -> int:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).year


async def verify(claim: str, allow_grounding: bool = True,
                 client_ip: Optional[str] = None) -> VerificationV2:
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

    if blocked is None and _is_two_point(parsed):
        two = await _verify_two_points(text, parsed, started)
        if two is not None and (two.verdict != NO_VERDICT or not allow_grounding):
            return two
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
            base = assess(parsed, evidence)
            verdict = _soften_for_alternate_measure(parsed, base)
            # A near miss that another official measure might explain is sent
            # to the search, but the search can only upgrade it (to Supported,
            # when the claim matches e.g. an earlier BEA estimate exactly). If
            # it finds nothing better, the near miss stands.
            near_miss = base.verdict == MOSTLY_ACCURATE
            fallback = base if near_miss else verdict
            # A structured answer that landed cleanly is the best outcome: exact
            # number, stated period, zero model calls.
            if verdict.verdict != NO_VERDICT or not allow_grounding:
                return _verdict_to_result(text, parsed, fallback, started)
            # Otherwise fall through — grounding may still resolve it, and the
            # structured figure is carried along as context.
            hint = (
                f"An official figure was retrieved but did not settle the claim: "
                f"{verdict.headline}"
            )
            if allow_grounding:
                g = await _safe_ground(text, hint, client_ip, parsed.metric is not None)
                if g.ok:
                    result = _grounded_to_result(text, parsed, g, started)
                    if not near_miss or result.verdict == SUPPORTED:
                        result.evidence = [d.to_dict() for d in verdict.evidence]
                        result.notes.append("structured lookup ran first and did not settle it")
                        return result
                if near_miss:
                    kept = _verdict_to_result(text, parsed, base, started)
                    kept.model_calls = g.model_calls
                    if g.ok:
                        kept.notes.append("AI search found no official figure the claim "
                                          "matches exactly")
                    return kept
            return _verdict_to_result(text, parsed, fallback, started)

    # A series that exists but has no row for the requested period is a
    # different situation from having no series at all, and the caller can act
    # on the difference: one resolves by waiting for publication, the other
    # never resolves. Collapsing both into "Inconclusive" is exactly what made
    # the old output useless.
    if structured_ran and parsed.metric is not None:
        unresolved_reason = PERIOD_UNAVAILABLE
        unresolved_headline = (
            f"No official {parsed.metric.label.lower()} figure is published for "
            f"{parsed.year or 'that period'} yet."
        )
    else:
        unresolved_reason = blocked or NO_SERIES
        unresolved_headline = "None of the official data series we check covers this claim."

    if not allow_grounding:
        return VerificationV2(
            claim=text, verdict=NO_VERDICT, confidence=LOW,
            headline=unresolved_headline,
            reason=unresolved_reason,
            tier=TIER_NONE,
            elapsed_seconds=round(time.monotonic() - started, 2),
            parsed=_parsed_summary(parsed),
        )

    # Before any web search: look for the figure in a government dataset.
    # Costs one small model call at most, often nothing (cached picks).
    from services import dataset_tier
    ds = await dataset_tier.verify(text, parsed, client_ip, started)
    if ds is not None:
        return ds
    # Then Claude, with the same datasets as tools: about $0.002, against a
    # web search's ~$0.03, and the figure is still the agency's own.
    ds = await dataset_tier.verify_found(text, parsed, client_ip, started)
    if ds is not None:
        return ds

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

    from claims.parser import income_band
    band = income_band(text)
    if band:
        hint = (
            f"The claim is about an income band or HUD's Area Median Income (\u201c{band}\u201d), "
            f"not Census median household income. Use HUD's published income limits for that "
            f"area, year and household size (huduser.gov), or the local housing agency's own "
            f"table built from them (for New York City, HPD's area median income chart). If "
            f"the claim gives a definition, such as a range for \u201cmiddle income\u201d, say "
            f"whose definition it matches, if any; there is no single official one."
        )
    g = await _safe_ground(text, hint, client_ip, parsed.metric is not None)
    if not g.ok and g.skipped:
        # Deliberately not sent: a limit, or a claim the gate keeps away from
        # the paid tier. Not an outage, and nothing was spent.
        return VerificationV2(
            claim=text, verdict=NO_VERDICT, confidence=LOW,
            headline=g.error if g.limited else f"{unresolved_headline} {g.error}",
            reason=unresolved_reason,
            tier=TIER_NONE, model_calls=0,
            elapsed_seconds=round(time.monotonic() - started, 2),
            parsed=_parsed_summary(parsed),
            notes=["degraded: ai limit reached"] if g.limited else [],
        )
    if not g.ok:
        return VerificationV2(
            claim=text, verdict=NO_VERDICT, confidence=LOW,
            headline=f"Could not retrieve evidence for this claim ({g.error}).",
            reason=unresolved_reason,
            tier=TIER_NONE, model_calls=g.model_calls,
            elapsed_seconds=round(time.monotonic() - started, 2),
            parsed=_parsed_summary(parsed),
        )
    return _grounded_to_result(text, parsed, g, started)


def to_response_dict(v: VerificationV2) -> Dict[str, Any]:
    from services.presentation import build_display
    out = asdict(v)
    out["display"] = build_display(v)
    return out
