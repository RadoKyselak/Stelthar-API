from typing import Literal, List, Dict, Any
from typing_extensions import TypedDict
from pydantic import BaseModel, field_validator, Field, ConfigDict

ClaimType = Literal[
    "Economic",
    "Demographic",
    "Legislative",
    "Budget",
    "Employment",
    "Other"
]

class APIQueryPlan(TypedDict, total=False):
    """Structure for multi-tier API query plan."""
    tier1_params: Dict[str, Any]
    tier2_keywords: List[str]

class ClaimAnalysis(TypedDict):
    """LLM analysis result for a user claim."""
    claim_normalized: str
    claim_type: ClaimType
    entities: List[str]
    relationship: str
    api_plan: APIQueryPlan

class VerifyRequest(BaseModel):
    """Request body for /verify endpoint with validation."""
    claim: str = Field(..., min_length=3, max_length=5000)

    @field_validator('claim')
    @classmethod
    def sanitize_claim(cls, v):
        """Sanitize and validate claim input."""
        from utils.validation import InputValidator

        return InputValidator.sanitize_claim(v)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "claim": "The unemployment rate in 2023 was 3.7%"
            }
        }
    )
