"""Run the v2 engine against the eval set and report what it actually did.

Two rules this runner follows that the previous harness did not, both aimed at
the same failure: a number that looks like accuracy but is not.

  1. Accuracy is NOT published when the degraded rate is non-zero. If some
     claims failed because a service was unavailable, the surviving sample is
     not a random subset — it is biased towards whatever the quota allowed that
     day. Reporting "83%" from it would be a measurement of the weather.

  2. Retrieval and judgement are scored separately. A wrong verdict with good
     retrieval is a comparison bug; a wrong verdict with no retrieval is a
     coverage gap. They need different fixes, so a single number that merges
     them tells you nothing about what to do next.

Honesty cases are scored on their REASON, not just on declining to answer. A
system that says "no verdict" for the wrong reason has not been honest, it has
been vague — and the whole point of replacing "Inconclusive" was to stop
"unavailable", "unpublished" and "unanswerable" from collapsing into one word.

    python -m eval.v2_runner
"""
import argparse
import asyncio
import collections
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from eval.v2_dataset import V2Claim, load_v2_claims  # noqa: E402
from services.verify_v2 import verify  # noqa: E402


@dataclass
class Outcome:
    claim: V2Claim
    predicted: str
    predicted_reason: Optional[str]
    correct: bool
    reason_correct: Optional[bool]
    had_evidence: bool
    tier: str
    model_calls: int
    seconds: float
    headline: str
    degraded: bool = False


async def run_one(c: V2Claim, allow_grounding: bool) -> Outcome:
    t0 = time.monotonic()
    try:
        r = await verify(c.claim, allow_grounding=allow_grounding)
    except Exception as e:  # a crash is a result, not an excuse to stop
        return Outcome(c, "CRASH", str(e), False, None, False, "none", 0,
                       time.monotonic() - t0, f"{type(e).__name__}: {e}", degraded=True)

    correct = r.verdict == c.expected_verdict
    reason_correct = None
    if c.expected_verdict == "No verdict":
        reason_correct = (r.reason == c.expected_reason)
        # Declining for the wrong reason is not the right answer.
        correct = correct and reason_correct

    # A grounded answer that could not reach the model is degraded, not wrong.
    degraded = (r.reason == "source_unavailable") or (r.tier == "none" and r.model_calls > 0)

    return Outcome(
        claim=c, predicted=r.verdict, predicted_reason=r.reason,
        correct=correct, reason_correct=reason_correct,
        had_evidence=bool(r.evidence), tier=r.tier, model_calls=r.model_calls,
        seconds=round(time.monotonic() - t0, 2), headline=r.headline,
        degraded=degraded,
    )


def report(outcomes: List[Outcome], allow_grounding: bool) -> int:
    n = len(outcomes)
    degraded = [o for o in outcomes if o.degraded]
    correct = [o for o in outcomes if o.correct]
    calls = sum(o.model_calls for o in outcomes)
    lat = sorted(o.seconds for o in outcomes)

    print("=" * 76)
    print("STELTHAR v2 EVAL")
    print("=" * 76)
    print(f"claims              : {n}")
    print(f"grounding enabled   : {allow_grounding}")
    print(f"total model calls   : {calls}  ({calls / n:.2f} per claim)")
    if lat:
        print(f"latency p50 / p95   : {lat[len(lat)//2]:.2f}s / {lat[min(len(lat)-1, int(0.95*(len(lat)-1)))]:.2f}s")
    print(f"degraded            : {len(degraded)}/{n}")
    print()

    if degraded:
        print("!! ACCURACY NOT REPORTED.")
        print(f"!! {len(degraded)} claim(s) failed because a service was unavailable, so the")
        print("!! surviving sample is not representative. Fix the outage and re-run.")
        for o in degraded[:5]:
            print(f"     {o.claim.id}: {o.headline[:90]}")
        print()
        return 1

    print(f"ACCURACY            : {len(correct)}/{n} = {len(correct)/n:.1%}")
    print()

    by_domain: Dict[str, List[Outcome]] = collections.defaultdict(list)
    for o in outcomes:
        by_domain[o.claim.domain].append(o)
    print(f"{'domain':<12} {'n':>3} {'correct':>8} {'acc':>7} {'evidence':>9} {'calls':>6}")
    print("-" * 76)
    for d, os_ in sorted(by_domain.items()):
        c = sum(1 for o in os_ if o.correct)
        ev = sum(1 for o in os_ if o.had_evidence)
        mc = sum(o.model_calls for o in os_)
        print(f"{d:<12} {len(os_):>3} {c:>8} {c/len(os_):>6.0%} {ev:>9} {mc:>6}")
    print()

    # Retrieval vs judgement, separated.
    answerable = [o for o in outcomes if o.claim.expected_verdict != "No verdict"]
    if answerable:
        got = [o for o in answerable if o.had_evidence]
        print(f"retrieval recall    : {len(got)}/{len(answerable)} = {len(got)/len(answerable):.0%}"
              "   (did the source layer find a datapoint at all)")
        if got:
            judged = sum(1 for o in got if o.correct)
            print(f"judgement given evi.: {judged}/{len(got)} = {judged/len(got):.0%}"
                  "   (was the comparison right when evidence existed)")
    honesty = [o for o in outcomes if o.claim.expected_verdict == "No verdict"]
    if honesty:
        h = sum(1 for o in honesty if o.correct)
        print(f"coverage honesty    : {h}/{len(honesty)} = {h/len(honesty):.0%}"
              "   (declined AND named the right reason)")
    print()

    misses = [o for o in outcomes if not o.correct]
    if misses:
        print(f"MISSES ({len(misses)})")
        print("-" * 76)
        for o in misses:
            print(f"  [{o.claim.id}] {o.claim.claim}")
            print(f"     expected {o.claim.expected_verdict}"
                  f"{' / ' + str(o.claim.expected_reason) if o.claim.expected_reason else ''}")
            print(f"     got      {o.predicted}"
                  f"{' / ' + str(o.predicted_reason) if o.predicted_reason else ''}")
            print(f"     {o.headline[:100]}")
            if o.claim.truth_value is not None:
                print(f"     ground truth: {o.claim.truth_source} "
                      f"{o.claim.truth_series} {o.claim.truth_period} = {o.claim.truth_value}")
            print()
    else:
        print("no misses")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-grounding", action="store_true",
                    help="Tier 1 only. Runs with no API keys at all.")
    args = ap.parse_args()
    allow = not args.no_grounding

    claims = load_v2_claims()
    outcomes = [await run_one(c, allow) for c in claims]
    return report(outcomes, allow)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
