"""Eval dataset schema and loader.

Every entry is a real, sourced claim + PolitiFact ruling (see claims.jsonl and
its `source_url`). Nothing here is invented — Phase 0 of the roadmap exists
specifically to stop the product from operating on unverified assumptions, so
fabricating "ground truth" would defeat the entire point.

Ground truth uses three verdict buckets, not PolitiFact's original six, because
that's what this system can express:
    Supported     <- PolitiFact True / Mostly True
    Contradicted  <- PolitiFact False / Mostly False / Pants on Fire
    Mixed         <- PolitiFact Half True / "needs context" — claims that are
                      technically-true-but-misleadingly-framed. This is the
                      exact failure mode named in PLAN.md §1.2: a system that
                      can only say Supported/Contradicted actively harms these
                      cases. The correct system behavior on a Mixed claim is
                      Inconclusive with an explanation of the missing context
                      — NOT a confident Supported or Contradicted. Scoring
                      treats this specially (see metrics.py).
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal, Optional

GroundTruthVerdict = Literal["Supported", "Contradicted", "Mixed"]
Coverage = Literal["yes", "no", "partial"]

_DATASET_PATH = Path(__file__).parent / "claims.jsonl"


@dataclass(frozen=True)
class EvalClaim:
    id: str
    claim: str
    domain: str
    ground_truth_verdict: GroundTruthVerdict
    ground_truth_notes: str
    source_url: str
    source_date: str
    # Does this system currently have a data source that COULD answer this?
    # "no" claims (crime, immigration) exist specifically to test whether the
    # system is honest about not knowing, rather than guessing — see
    # PLAN.md §1.3/§1.4 (coverage gaps, unearned confidence).
    expected_coverage: Coverage
    # A claim is true/false but framed to mislead (wrong baseline, wrong
    # timeframe, cherry-picked source) — PLAN.md §1.2's central blind spot.
    is_framing_trap: bool = False
    is_temporal_trap: bool = False
    notes: str = ""


def load_claims(path: Optional[Path] = None) -> List[EvalClaim]:
    path = path or _DATASET_PATH
    claims = []
    with open(path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{line_num}: invalid JSON — {e}") from e
            claims.append(EvalClaim(**row))
    return claims
