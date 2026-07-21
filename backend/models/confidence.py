from typing_extensions import TypedDict

class ConfidenceBreakdown(TypedDict):
    confidence: float
    R: float  # Source Reliability
    E: float  # Evidence Density
    S: float  # Semantic Alignment
    S_semantic_sim: float  # Raw Semantic similarity score
