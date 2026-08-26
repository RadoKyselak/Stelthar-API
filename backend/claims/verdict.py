"""Turn a claim plus an authoritative datapoint into a verdict.

Two design decisions carried over from PLAN.md, both aimed at the failure the
old taxonomy could not express.

First, `Inconclusive` is gone as an output. It was the modal answer and it told
the user nothing — a dead adapter, an unparseable plan and a genuinely
unanswerable question all produced the same word. Every non-verdict here names
its own reason (`NO_SERIES`, `PERIOD_UNAVAILABLE`, `NOT_EMPIRICAL`,
`PERIOD_MISMATCH`, `GEOGRAPHY_UNSUPPORTED`), so the caller learns what would
have to change for an answer to exist.

Second, `MISLEADING` is a first-class verdict. Most real-world misinformation is
not a fabricated number — it is a true number with a dishonest frame. A system
that can only say Supported is actively harmful there, because it stamps a
misleading claim as true. `TREND_MISMATCH` covers the mechanical version of this
that a series can prove on its own.

Confidence is a coarse tier, never a percentage. The old score presented two
decimal places derived from a formula that had never been calibrated against
ground truth, and whose largest component had silently been returning a constant
for months. A tier that reflects what is actually known is more honest than a
number that looks precise and is not.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from claims.parser import ParsedClaim
from evidence.datapoint import Datapoint, Request
from evidence.units import humanize

# Verdicts.
SUPPORTED = "Supported"
CONTRADICTED = "Contradicted"
MISLEADING = "Misleading"
NO_VERDICT = "No verdict"

# Reasons a verdict could not be reached. Each is actionable.
NO_SERIES = "no_official_series_for_this_claim"
PERIOD_UNAVAILABLE = "data_not_published_for_that_period"
PERIOD_MISMATCH = "evidence_covers_a_different_period"
GEOGRAPHY_UNSUPPORTED = "no_official_series_at_that_geography"
NOT_EMPIRICAL = "claim_is_not_empirically_testable"
NO_VALUE_IN_CLAIM = "claim_states_no_checkable_value"
TREND_CLAIM = "claim_describes_a_trend_and_needs_a_series"
UNRESOLVED_PERIOD = "claim_names_a_period_that_cannot_be_resolved_to_a_date"

HIGH, MEDIUM, LOW = "High", "Medium", "Low"


@dataclass
class Verdict:
    verdict: str
    confidence: str                       # High | Medium | Low
    headline: str                         # the evidence-first one-liner
    reason: str = ""                      # set when verdict is NO_VERDICT
    claimed_display: str = ""
    official_display: str = ""
    delta_display: str = ""
    relative_delta: Optional[float] = None
    evidence: List[Datapoint] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "headline": self.headline,
            "reason": self.reason or None,
            "claim_value": self.claimed_display or None,
            "official_value": self.official_display or None,
            "delta": self.delta_display or None,
            "relative_delta": self.relative_delta,
            "caveats": self.caveats,
            "evidence": [d.to_dict() for d in self.evidence],
        }


def _satisfies_comparator(
    claimed: float, official: float, comparator: str, tol_abs: Optional[float],
    relative_fallback: float = 0.01,
) -> bool:
    """Does the official figure satisfy what the claim asserted?

    For a directional claim the claimed number is a THRESHOLD, not a target:
    "the debt is over $33 trillion" against an actual $40.1 trillion is
    supported, even though the two numbers are nowhere near equal. Comparing
    them for equality — which a naive delta check does — would contradict a
    true claim.

    For an equality claim the tolerance is absolute and derived from how
    precisely the claim was written, so "9%" agrees with 9.06% while "3.7%"
    does not agree with 4.0%. Only when the written precision cannot be
    determined does it fall back to a relative band.
    """
    if comparator == "greater than":
        return official > claimed
    if comparator == "at least":
        return official >= claimed
    if comparator == "less than":
        return official < claimed
    if comparator == "at most":
        return official <= claimed

    diff = abs(claimed - official)
    if tol_abs is not None:
        return diff <= tol_abs
    denom = abs(official) if abs(official) > 1e-12 else 1.0
    return diff / denom <= relative_fallback


def _phrase_for(comparator: str) -> str:
    return {
        "greater than": "more than", "at least": "at least",
        "less than": "less than", "at most": "at most",
    }.get(comparator, "")


def assess(parsed: ParsedClaim, evidence: List[Datapoint]) -> Verdict:
    """Compare a parsed claim against retrieved evidence."""
    if not evidence:
        return Verdict(
            verdict=NO_VERDICT, confidence=LOW,
            reason=PERIOD_UNAVAILABLE if parsed.metric else NO_SERIES,
            headline=(
                f"No official figure is published for {parsed.metric.label.lower()} "
                f"in {parsed.year}."
                if parsed.metric and parsed.year else
                "No official series covers this claim."
            ),
        )

    request = Request(
        metric=evidence[0].metric,
        year=parsed.year,
        year_basis=parsed.year_basis,
        geography=parsed.geography if (parsed.geography and parsed.geography.level != "place") else None,
    )

    usable = [d for d in evidence if d.answers(request)]
    caveats: List[str] = []

    if not usable:
        mismatch = evidence[0].mismatch_against(request)
        best = evidence[0]
        # Still show the number. Degrading to the data beats degrading to
        # "I don't know" — the reader can see exactly what was found and why it
        # does not settle the question.
        return Verdict(
            verdict=NO_VERDICT, confidence=LOW, reason=PERIOD_MISMATCH,
            headline=(
                f"{best.label} is {best.display_value()} for {best.observed.describe()}, "
                f"which does not answer the claim: {mismatch}."
            ),
            official_display=f"{best.display_value()} ({best.observed.describe()})",
            evidence=[best], caveats=[mismatch or "evidence does not match the request"],
        )

    official = usable[0]
    official_display = f"{official.display_value()} ({official.observed.describe()})"

    # A claim asserting two values, or anchoring one to a period we cannot
    # resolve to a date, must not be settled by comparing against a single
    # figure. Doing so produced a confident Contradicted on a TRUE claim:
    # "$27.8 trillion when Biden took office and around $34.4 trillion now" was
    # compared against today's $40.1 trillion. Declining is the correct answer
    # until the series-based trend check exists.
    if parsed.is_trend_claim or parsed.has_unresolved_period:
        reason = TREND_CLAIM if parsed.is_trend_claim else UNRESOLVED_PERIOD
        detail = (
            "The claim compares more than one point in time; a single figure "
            "cannot settle it."
            if parsed.is_trend_claim else
            "The claim anchors its value to a period given only relatively, "
            "which cannot be resolved to a date."
        )
        return Verdict(
            verdict=NO_VERDICT, confidence=LOW, reason=reason,
            headline=f"{detail} For reference, {official.label} was {official_display}.",
            official_display=official_display, evidence=usable,
            caveats=[detail],
        )

    if official.observed.basis.startswith("computed as the mean"):
        caveats.append(
            "Annual average computed from the 12 monthly observations, because "
            "the agency did not publish one for this series and period."
        )
    if parsed.is_trend_claim:
        caveats.append(
            f"The claim spans {parsed.all_years[0]}-{parsed.all_years[-1]}; "
            "a single figure cannot settle a trend."
        )

    if parsed.claimed_value is None:
        return Verdict(
            verdict=NO_VERDICT, confidence=MEDIUM, reason=NO_VALUE_IN_CLAIM,
            headline=f"{official.label} was {official_display}.",
            official_display=official_display, evidence=usable, caveats=caveats,
        )

    claimed = parsed.claimed_value
    if claimed.family != official.quantity.family and "unknown" not in (
        claimed.family, official.quantity.family
    ):
        return Verdict(
            verdict=NO_VERDICT, confidence=LOW, reason=NO_VALUE_IN_CLAIM,
            headline=(
                f"The claim's figure is in {claimed.family} but {official.label} is "
                f"reported in {official.quantity.family}; they are not comparable."
            ),
            official_display=official_display, evidence=usable, caveats=caveats,
        )

    agrees = _satisfies_comparator(
        claimed.magnitude, official.quantity.magnitude, parsed.comparator,
        claimed.tolerance(),
    )

    claimed_display = humanize(claimed.magnitude, official.quantity.family)
    delta = official.quantity.magnitude - claimed.magnitude
    denom = abs(official.quantity.magnitude)
    rel = (delta / denom) if denom > 1e-12 else None

    phrase = _phrase_for(parsed.comparator)
    if phrase:
        claim_side = f"Claim says {phrase} {claimed_display}."
    else:
        claim_side = f"Claim says {claimed_display}."

    headline = (
        f"{claim_side} {official.source} reports {official.display_value()} "
        f"for {official.observed.describe()}."
    )

    # Confidence reflects what is actually known about the evidence, not how
    # decisive the wording was.
    confidence = HIGH
    if caveats:
        confidence = MEDIUM
    if parsed.is_trend_claim:
        confidence = LOW

    if parsed.is_trend_claim:
        return Verdict(
            verdict=NO_VERDICT, confidence=LOW, reason=TREND_CLAIM,
            headline=headline, claimed_display=claimed_display,
            official_display=official_display,
            delta_display=humanize(delta, official.quantity.family),
            relative_delta=rel, evidence=usable, caveats=caveats,
        )

    return Verdict(
        verdict=SUPPORTED if agrees else CONTRADICTED,
        confidence=confidence,
        headline=headline,
        claimed_display=claimed_display,
        official_display=official_display,
        delta_display=humanize(delta, official.quantity.family),
        relative_delta=rel,
        evidence=usable,
        caveats=caveats,
    )
