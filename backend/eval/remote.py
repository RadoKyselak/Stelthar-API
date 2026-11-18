"""Run the eval set against a deployed service, over HTTP, for free.

`eval.v2_runner` calls the engine in this checkout, so it needs every agency
key on the machine it runs on. The keys live on the server. This asks the
server instead: the eval's claims go to `POST /v2/check` in batches, which
checks each on free government data only (no model call, whatever the claim),
and the answers are scored by the same rules and printed by the same report.

    python -m eval.remote                                   # the public service
    python -m eval.remote --base http://localhost:8000      # a local one
    python -m eval.remote --out eval/results/2026-11-17.json
    python -m eval.remote --set news                        # real sentences (news_claims.jsonl)

What it measures is the free engine as deployed: the part that answers from
agency data. A claim only the paid search can settle will show as a miss here,
which is the honest reading of "what does it get right for nothing".

The same rule as the local runner: if any claim failed because a source was
down, accuracy is not reported.
"""
import argparse
import json
import pathlib
import sys
import time
from datetime import date
from typing import Any, Dict, List, Optional

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from eval.v2_dataset import V2Claim, load_v2_claims  # noqa: E402
from eval.v2_runner import Outcome, report  # noqa: E402

DEFAULT_BASE = "https://stelthar-api.vercel.app"
_HERE = pathlib.Path(__file__).resolve().parent
# None is the default set (v2_claims.jsonl).
SETS = {"agency": None, "news": _HERE / "news_claims.jsonl"}
BATCH = 10            # claims per request; the service checks at most 12
PAUSE_SECONDS = 11.0  # the service allows 6 passage checks a minute per visitor
NOT_A_CLAIM = "not_recognised_as_a_claim"
_NOTHING_TO_CHECK = ("no_official_series_for_this_claim", "claim_is_not_empirically_testable")


def _key(text: str) -> str:
    """How a claim is matched to its answer: as written, less end punctuation."""
    return " ".join(text.split()).rstrip(".!?").lower()


def _outcome(c: V2Claim, entry: Optional[Dict[str, Any]], seconds: float) -> Outcome:
    if entry is None:
        # The service did not read the sentence as a claim at all.
        predicted, reason, result = "No verdict", NOT_A_CLAIM, {}
    else:
        result = entry["result"]
        predicted, reason = result.get("verdict", "No verdict"), result.get("reason")
    correct = predicted == c.expected_verdict
    reason_correct = None
    if c.expected_verdict == "No verdict":
        # Leaving a sentence out of a passage's claims is the service saying
        # there is nothing in it to check against data: the right answer when
        # the expected reason is that no series covers it or it is an opinion.
        reason_correct = reason == c.expected_reason or (
            reason == NOT_A_CLAIM and c.expected_reason in _NOTHING_TO_CHECK)
        correct = correct and reason_correct
    return Outcome(
        claim=c, predicted=predicted, predicted_reason=reason, correct=correct,
        reason_correct=reason_correct, had_evidence=bool(result.get("evidence")),
        tier=result.get("tier", "none"), model_calls=result.get("model_calls") or 0,
        seconds=seconds, headline=result.get("headline") or "(not read as a claim)",
        degraded=reason == "source_unavailable",
    )


def run(claims: List[V2Claim], base: str, client: Optional[httpx.Client] = None,
        pause: float = PAUSE_SECONDS) -> List[Outcome]:
    own = client is None
    client = client or httpx.Client(timeout=90.0)
    outcomes: List[Outcome] = []
    try:
        for start in range(0, len(claims), BATCH):
            batch = claims[start:start + BATCH]
            if start and pause:
                time.sleep(pause)
            t0 = time.monotonic()
            # One claim per line. Two or more claims in a request are always
            # checked on free data; a lone last claim is padded so it is too.
            lines = [c.claim for c in batch]
            if len(lines) == 1:
                lines.append("The unemployment rate in 2019 was 3.7%.")
            r = client.post(f"{base.rstrip('/')}/v2/check", json={"text": "\n".join(lines), "mode": "text"})
            r.raise_for_status()
            body = r.json()
            each = (time.monotonic() - t0) / max(len(batch), 1)
            answers = {_key(e["span"]): e for e in body.get("claims", [])}
            for c in batch:
                outcomes.append(_outcome(c, answers.get(_key(c.claim)), round(each, 2)))
    finally:
        if own:
            client.close()
    return outcomes


def to_json(outcomes: List[Outcome], base: str) -> Dict[str, Any]:
    degraded = sum(1 for o in outcomes if o.degraded)
    return {
        "service": base, "date": date.today().isoformat(), "mode": "free data only",
        "claims": len(outcomes), "correct": sum(1 for o in outcomes if o.correct),
        "degraded": degraded,
        # Not a number to quote when a source was down (see the module note).
        "accuracy": None if degraded or not outcomes else round(
            sum(1 for o in outcomes if o.correct) / len(outcomes), 4),
        "model_calls": sum(o.model_calls for o in outcomes),
        "outcomes": [{
            "id": o.claim.id, "claim": o.claim.claim, "expected": o.claim.expected_verdict,
            "expected_reason": o.claim.expected_reason, "got": o.predicted, "got_reason": o.predicted_reason,
            "correct": o.correct, "tier": o.tier, "headline": o.headline,
            "truth": {"source": o.claim.truth_source, "series": o.claim.truth_series,
                      "period": o.claim.truth_period, "value": o.claim.truth_value},
        } for o in outcomes],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default=DEFAULT_BASE, help="The service to test.")
    ap.add_argument("--out", help="Write the full results here as JSON.")
    ap.add_argument("--set", choices=sorted(SETS), default="agency",
                    help="agency: figures read from the agencies (default). "
                         "news: sentences as releases and wire stories wrote them.")
    args = ap.parse_args()

    outcomes = run(load_v2_claims(SETS[args.set]), args.base)
    if args.out:
        path = pathlib.Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_json(outcomes, args.base), indent=2) + "\n")
        print(f"results written to {path}")
    return report(outcomes, allow_grounding=False)


if __name__ == "__main__":
    raise SystemExit(main())
