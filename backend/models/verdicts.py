from typing import Literal, List, Dict, Optional
from typing_extensions import TypedDict

# What the synthesis step may conclude about a claim.
VerdictType = Literal["Supported", "Contradicted", "Inconclusive"]

# What the endpoint may return. Wider than VerdictType because the pipeline
# can fail without reaching a conclusion, and "Error" is not a judgment about
# the claim. It was previously absent, so _build_error_response produced a
# payload the declared response_model rejected — meaning the entire graceful
# degradation path raised instead of degrading.
ResponseVerdictType = Literal["Supported", "Contradicted", "Inconclusive", "Error"]

class EvidenceLink(TypedDict):
    """Individual evidence citation linking finding to source."""
    finding: str
    source_url: str

class SynthesisResult(TypedDict):
    """LLM synthesis output with verdict and justification."""
    verdict: VerdictType
    summary: str
    justification: str
    evidence_links: List[EvidenceLink]

class VerificationResponse(TypedDict):
    """Complete response from /verify endpoint."""
    claim_original: str
    claim_normalized: str
    claim_type: str
    # True when the pipeline failed before reaching a conclusion. A client MUST
    # check this before showing the verdict: a degraded response carries the
    # default "Inconclusive", which would otherwise read as a finding that no
    # supporting data exists — a statement about the world, not about an outage.
    degraded: bool
    degraded_reason: Optional[str]
    verdict: ResponseVerdictType
    confidence: float
    confidence_tier: Literal["High", "Medium", "Low"]
    confidence_breakdown: Dict[str, float]
    summary: str
    evidence_links: List[EvidenceLink]
    sources: List[Dict]
    debug_plan: Dict
    debug_log: List[Dict]
    # Audit trail for the multi-pass verification loop: one entry per
    # plan/retrieve/critique cycle, plus any confidence caps that were applied.
    debug_iterations: List[Dict]
    debug_notes: List[str]
