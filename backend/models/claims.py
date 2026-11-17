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

class LookupRequest(BaseModel):
    """Request body for /v2/lookup.

    Shorter minimum than VerifyRequest: a lookup is terse by design ("gdp",
    "cpi 2022"), and a three-character floor would reject the shortest real
    queries the omnibox is built for.
    """
    # Up to 600: extension 2.1.2 and older send highlighted sentences here.
    query: str = Field(..., min_length=2, max_length=600)
    # True on an explicit search (Enter / Find), never on a keystroke: only
    # then may Claude search the datasets when the rules find nothing.
    deep: bool = False

    @field_validator('query')
    @classmethod
    def sanitize_query(cls, v):
        from utils.validation import InputValidator

        return InputValidator.sanitize_claim(v)


class CheckRequest(BaseModel):
    """Request body for /v2/check: a passage, every claim in it checked."""
    # The cap in characters is config.CHECK_MAX_CHARS, applied in the validator
    # so it can be changed without a deploy; 12,000 is its ceiling.
    text: str = Field(..., min_length=3, max_length=12000)
    # "text": something a person pasted. "page": a page scan, which keeps only
    # the claims free government data can settle.
    mode: str = Field("text", pattern="^(text|page)$")

    @field_validator('text')
    @classmethod
    def sanitize_text(cls, v):
        import config
        from utils.validation import InputValidator

        return InputValidator.sanitize_passage(v, config.CHECK_MAX_CHARS)


class VerifyRequest(BaseModel):
    """Request body for /verify endpoint with validation."""
    claim: str = Field(..., min_length=3, max_length=1000)

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
