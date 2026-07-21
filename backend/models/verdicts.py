from typing import Literal, List, Dict
from typing_extensions import TypedDict

VerdictType = Literal["Supported", "Contradicted", "Inconclusive"]

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
    verdict: VerdictType
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
