"""Eval harness runner.

Usage:
    python -m eval.runner                  # run the seed dataset against the
                                            # live pipeline (needs real API keys)
    python -m eval.runner --dataset PATH   # run a different claim set
    python -m eval.runner --out PATH.md    # write the report elsewhere

Requires real GEMINI_API_KEY / BEA_API_KEY / CENSUS_API_KEY / BLS_API_KEY /
CONGRESS_API_KEY env vars to produce a real result — with fake/missing keys
every claim will error out and the report will say so rather than silently
producing a fabricated-looking score.
"""
import argparse
import asyncio
import os
import time
from pathlib import Path
from typing import List, Optional

from eval.dataset import EvalClaim, load_claims
from eval.metrics import ClaimResult, compute_report, EvalReport

_REQUIRED_KEYS = [
    "GEMINI_API_KEY", "BEA_API_KEY", "CENSUS_API_KEY",
    "BLS_API_KEY", "CONGRESS_API_KEY",
]


def missing_keys() -> List[str]:
    return [k for k in _REQUIRED_KEYS if not os.getenv(k)]


async def run_claim(service, claim: EvalClaim) -> ClaimResult:
    start = time.monotonic()
    try:
        response = await service.verify_claim(claim.claim)
    except Exception as e:
        return ClaimResult(
            claim=claim, predicted_verdict=None, predicted_confidence=None,
            had_datapoint=False, latency_seconds=time.monotonic() - start,
            error=str(e),
        )
    elapsed = time.monotonic() - start

    verdict = response.get("verdict")
    if verdict == "Error":
        return ClaimResult(
            claim=claim, predicted_verdict=None, predicted_confidence=None,
            had_datapoint=False, latency_seconds=elapsed,
            error=response.get("summary", "unknown error"), raw_response=response,
        )

    sources = response.get("sources", []) or []
    had_datapoint = any(isinstance(s, dict) and s.get("data_value") is not None for s in sources)

    return ClaimResult(
        claim=claim,
        predicted_verdict=verdict,
        predicted_confidence=response.get("confidence"),
        had_datapoint=had_datapoint,
        latency_seconds=elapsed,
        raw_response=response,
    )


async def run_eval(claims: List[EvalClaim], service=None) -> List[ClaimResult]:
    if service is None:
        from services.verification_service import VerificationService
        service = VerificationService()

    results = []
    for claim in claims:
        result = await run_claim(service, claim)
        results.append(result)
    return results


def render_report_markdown(report: EvalReport, dataset_size: int, keys_missing: List[str]) -> str:
    lines = ["# Stelthar Eval Report", ""]

    if keys_missing:
        lines += [
            "> **Not a real accuracy measurement.** The following API keys were "
            f"missing at run time: {', '.join(keys_missing)}. Every claim below "
            "either errored or ran against a non-functional pipeline. Set real "
            "keys and re-run before treating any number here as ground truth "
            "about system accuracy.",
            "",
        ]

    lines += [
        f"Dataset: {dataset_size} claims (seed set — see PLAN.md Phase 0 for the "
        "300-500 claim target this is a pilot toward).",
        "",
        f"- **Overall accuracy**: {report.accuracy:.1%} ({report.n_total} claims)",
        f"- **Overconfident-on-Mixed rate**: {report.overconfident_on_mixed_rate:.1%} "
        "(confidently answered Supported/Contradicted on a claim whose correct answer is nuanced)",
    ]
    if report.retrieval_recall is not None:
        lines.append(f"- **Retrieval recall** (found a real datapoint when one exists): {report.retrieval_recall:.1%}")
    if report.coverage_honesty_rate is not None:
        lines.append(
            f"- **Coverage honesty** (admitted no source instead of guessing): {report.coverage_honesty_rate:.1%}"
        )
    if report.latency_p50 is not None:
        lines.append(f"- **Latency**: p50={report.latency_p50:.1f}s, p95={report.latency_p95:.1f}s")

    lines += ["", "## By domain", "", "| Domain | N | Correct | Wrong | Overconfident-Mixed | No response |",
              "|---|---|---|---|---|---|"]
    for domain, d in sorted(report.domains.items()):
        lines.append(f"| {domain} | {d.n} | {d.correct} | {d.wrong} | {d.overconfident_on_mixed} | {d.no_response} |")

    lines += ["", "## Calibration", "",
              "*Only meaningful with enough samples per bucket — flagged `reliable: false` below that threshold.*", "",
              "| Confidence bucket | N | Observed accuracy | Reliable? |", "|---|---|---|---|"]
    for b in report.calibration_buckets:
        acc = f"{b['observed_accuracy']:.1%}" if b["observed_accuracy"] is not None else "n/a"
        lines.append(f"| {b['confidence_bucket']} | {b['n']} | {acc} | {b['reliable']} |")

    if report.misses:
        lines += ["", "## Every non-correct result (for inspection, not just a score)", ""]
        for m in report.misses:
            lines.append(f"- **{m['id']}** [{m['outcome']}]: {m['claim']}")
            if "ground_truth" in m:
                lines.append(f"  - ground truth: {m['ground_truth']} — {m.get('notes', '')}")
            if "predicted" in m:
                lines.append(f"  - predicted: {m['predicted']}" + (
                    f" (confidence {m['predicted_confidence']:.2f})" if m.get("predicted_confidence") is not None else ""
                ))
            if "error" in m:
                lines.append(f"  - error: {m['error']}")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Run the Stelthar eval harness.")
    parser.add_argument("--dataset", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "report.md")
    args = parser.parse_args()

    claims = load_claims(args.dataset)
    keys_missing = missing_keys()
    if keys_missing:
        print(f"WARNING: missing API keys ({', '.join(keys_missing)}). "
              "Results will not reflect real system accuracy.")

    results = asyncio.run(run_eval(claims))
    report = compute_report(results)
    markdown = render_report_markdown(report, len(claims), keys_missing)

    args.out.write_text(markdown)
    print(f"Report written to {args.out}")
    print(f"Accuracy: {report.accuracy:.1%} | Coverage honesty: "
          f"{report.coverage_honesty_rate if report.coverage_honesty_rate is not None else 'n/a'}")


if __name__ == "__main__":
    main()
