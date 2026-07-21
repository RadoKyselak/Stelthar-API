"""Scoring for the eval harness.

The scoring model is deliberately not a naive "did the verdict match"
comparison — see PLAN.md §1.2 and §1.4 for why. Three things are measured
separately because they test different failure modes:

  * verdict accuracy — did the system reach the right conclusion, with
    special handling for "Mixed" (PolitiFact Half-True) claims: Inconclusive
    is the CORRECT answer there, not a miss. Confidently saying
    Supported/Contradicted on a Mixed claim is scored as its own failure mode
    ("overconfident_on_mixed"), because that's actively worse than admitting
    uncertainty.
  * coverage honesty — for claims this system has no data source for, did it
    say so (Inconclusive / low confidence) rather than guess? This is the
    metric that catches an unearned-confidence problem before it ships.
  * retrieval recall — independent of what the LLM concluded, did the source
    layer actually find a real datapoint? A wrong verdict with good retrieval
    is a synthesis bug; a wrong verdict with no retrieval is a coverage gap.
    These need different fixes, so they're tracked separately.

Calibration (do 0.8-confidence answers get it right ~80% of the time) is only
meaningful with a large N. At the seed-set size this dataset actually has, the
per-bucket counts are reported explicitly so nobody mistakes a 3-sample bucket
for a real calibration curve.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from eval.dataset import EvalClaim

# Below this confidence, an answer is being treated as "we said we don't know" —
# matches the Inconclusive confidence cap enforced in verification_service.py.
HONESTY_CONFIDENCE_THRESHOLD = 0.45


@dataclass
class ClaimResult:
    claim: EvalClaim
    predicted_verdict: Optional[str]
    predicted_confidence: Optional[float]
    had_datapoint: bool
    latency_seconds: Optional[float]
    error: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None


def score_verdict(claim: EvalClaim, predicted_verdict: Optional[str]) -> str:
    """Returns one of: correct, wrong, overconfident_on_mixed, no_response."""
    if predicted_verdict is None:
        return "no_response"

    gt = claim.ground_truth_verdict
    if gt == "Mixed":
        if predicted_verdict == "Inconclusive":
            return "correct"
        return "overconfident_on_mixed"

    # gt is Supported or Contradicted
    if predicted_verdict == gt:
        return "correct"
    return "wrong"


def score_coverage_honesty(claim: EvalClaim, result: ClaimResult) -> Optional[str]:
    """Only meaningful for expected_coverage == 'no' claims.

    Returns "honest" (admitted it doesn't know), "overconfident" (answered
    confidently anyway), or None if not applicable to this claim.
    """
    if claim.expected_coverage != "no":
        return None
    if result.predicted_verdict is None:
        return "honest"  # errored out rather than guessing — acceptable
    is_humble = (
        result.predicted_verdict == "Inconclusive"
        or (result.predicted_confidence or 0) <= HONESTY_CONFIDENCE_THRESHOLD
    )
    return "honest" if is_humble else "overconfident"


@dataclass
class DomainReport:
    domain: str
    n: int
    correct: int = 0
    wrong: int = 0
    overconfident_on_mixed: int = 0
    no_response: int = 0
    retrieval_hits: int = 0
    retrieval_applicable: int = 0
    coverage_honest: int = 0
    coverage_overconfident: int = 0
    coverage_applicable: int = 0


@dataclass
class EvalReport:
    n_total: int
    domains: Dict[str, DomainReport]
    accuracy: float
    overconfident_on_mixed_rate: float
    retrieval_recall: Optional[float]
    coverage_honesty_rate: Optional[float]
    latency_p50: Optional[float]
    latency_p95: Optional[float]
    calibration_buckets: List[Dict[str, Any]]
    misses: List[Dict[str, Any]]


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(pct * (len(s) - 1))))
    return s[idx]


def compute_report(results: List[ClaimResult]) -> EvalReport:
    domains: Dict[str, DomainReport] = {}
    misses: List[Dict[str, Any]] = []
    latencies: List[float] = []

    correct_total = 0
    overconfident_mixed_total = 0
    no_response_total = 0
    retrieval_hits_total = 0
    retrieval_applicable_total = 0
    coverage_honest_total = 0
    coverage_applicable_total = 0

    # confidence bucket -> list of (is_correct: bool)
    buckets: Dict[str, List[bool]] = {"0.0-0.25": [], "0.25-0.5": [], "0.5-0.75": [], "0.75-1.0": []}

    for r in results:
        d = domains.setdefault(r.claim.domain, DomainReport(domain=r.claim.domain, n=0))
        d.n += 1

        outcome = score_verdict(r.claim, r.predicted_verdict)
        if outcome == "correct":
            d.correct += 1
            correct_total += 1
        elif outcome == "overconfident_on_mixed":
            d.overconfident_on_mixed += 1
            overconfident_mixed_total += 1
            misses.append({
                "id": r.claim.id, "claim": r.claim.claim, "outcome": outcome,
                "ground_truth": r.claim.ground_truth_verdict,
                "predicted": r.predicted_verdict,
                "notes": r.claim.ground_truth_notes,
            })
        elif outcome == "no_response":
            d.no_response += 1
            no_response_total += 1
            misses.append({
                "id": r.claim.id, "claim": r.claim.claim, "outcome": outcome,
                "error": r.error,
            })
        else:  # wrong
            d.wrong += 1
            misses.append({
                "id": r.claim.id, "claim": r.claim.claim, "outcome": outcome,
                "ground_truth": r.claim.ground_truth_verdict,
                "predicted": r.predicted_verdict,
                "notes": r.claim.ground_truth_notes,
            })

        if r.claim.expected_coverage in ("yes", "partial"):
            d.retrieval_applicable += 1
            retrieval_applicable_total += 1
            if r.had_datapoint:
                d.retrieval_hits += 1
                retrieval_hits_total += 1

        honesty = score_coverage_honesty(r.claim, r)
        if honesty is not None:
            d.coverage_applicable += 1
            coverage_applicable_total += 1
            if honesty == "honest":
                d.coverage_honest += 1
                coverage_honest_total += 1
            else:
                d.coverage_overconfident += 1
                misses.append({
                    "id": r.claim.id, "claim": r.claim.claim,
                    "outcome": "overconfident_no_coverage",
                    "predicted": r.predicted_verdict,
                    "predicted_confidence": r.predicted_confidence,
                })

        if r.latency_seconds is not None:
            latencies.append(r.latency_seconds)

        if r.predicted_confidence is not None:
            c = r.predicted_confidence
            key = (
                "0.0-0.25" if c < 0.25 else
                "0.25-0.5" if c < 0.5 else
                "0.5-0.75" if c < 0.75 else
                "0.75-1.0"
            )
            buckets[key].append(outcome == "correct")

    n = len(results)
    calibration = []
    for bucket, vals in buckets.items():
        calibration.append({
            "confidence_bucket": bucket,
            "n": len(vals),
            "observed_accuracy": (sum(vals) / len(vals)) if vals else None,
            "reliable": len(vals) >= 10,  # rule of thumb; seed sets won't hit this
        })

    return EvalReport(
        n_total=n,
        domains=domains,
        accuracy=correct_total / n if n else 0.0,
        overconfident_on_mixed_rate=(
            overconfident_mixed_total / max(1, sum(1 for r in results if r.claim.ground_truth_verdict == "Mixed"))
        ),
        retrieval_recall=(retrieval_hits_total / retrieval_applicable_total) if retrieval_applicable_total else None,
        coverage_honesty_rate=(coverage_honest_total / coverage_applicable_total) if coverage_applicable_total else None,
        latency_p50=_percentile(latencies, 0.5),
        latency_p95=_percentile(latencies, 0.95),
        calibration_buckets=calibration,
        misses=misses,
    )
