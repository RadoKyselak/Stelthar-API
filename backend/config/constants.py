from dataclasses import dataclass
from typing import Dict

@dataclass(frozen=True)
class LLMConfig:
    MAX_CONTEXT_LENGTH: int = 30000
    MAX_BATCH_SIZE: int = 100
    REQUEST_TIMEOUT: float = 60.0
    EMBED_TIMEOUT: float = 45.0
    BATCH_EMBED_TIMEOUT: float = 45.0

@dataclass(frozen=True)
class SourceReliabilityWeights:
    BEA: float = 1.0
    CENSUS: float = 1.0
    BLS: float = 1.0
    CONGRESS: float = 0.8
    DATA_GOV: float = 0.7
    LIVE_WEB: float = 0.6
    DEFAULT: float = 0.6
    
    def get_weight_for_url(self, url: str) -> float:
        url_lower = url.lower()
        if "apps.bea.gov" in url_lower:
            return self.BEA
        elif "api.census.gov" in url_lower:
            return self.CENSUS
        elif "api.bls.gov" in url_lower or "data.bls.gov" in url_lower:
            return self.BLS
        elif "api.congress.gov" in url_lower:
            return self.CONGRESS
        elif "catalog.data.gov" in url_lower:
            return self.DATA_GOV
        elif any(t in url_lower for t in [".gov", ".edu", "reuters.com", "apnews.com", "worldbank.org", "imf.org", "oecd.org"]):
            return self.LIVE_WEB
        elif "live_web" in url_lower:
            return self.LIVE_WEB
        else:
            return self.DEFAULT

@dataclass(frozen=True)
class ConfidenceConfig:
    R_WEIGHT: float = 0.5
    S_WEIGHT: float = 0.3
    E_WEIGHT: float = 0.2 
    
    S_LLM_WEIGHT: float = 0.7 
    S_EMBEDDING_WEIGHT: float = 0.3

    VERDICT_CONFIDENCE_SUPPORTED: float = 0.95
    VERDICT_CONFIDENCE_CONTRADICTED: float = 0.90
    VERDICT_CONFIDENCE_INCONCLUSIVE: float = 0.50

    MAX_SOURCES_FOR_FULL_DENSITY: int = 5

    HIGH_THRESHOLD: float = 0.75
    MEDIUM_THRESHOLD: float = 0.5

    DEFAULT_CONFIDENCE: float = 0.3
    DEFAULT_R: float = 0.5
    DEFAULT_E: float = 0.0
    DEFAULT_S: float = 0.3
    DEFAULT_S_SEMANTIC: float = 0.0

@dataclass(frozen=True)
class APITimeouts:
    """Timeout configurations for external API calls."""
    BEA: float = 30.0
    CENSUS: float = 30.0
    BLS: float = 30.0
    CONGRESS: float = 30.0
    DATA_GOV: float = 30.0

@dataclass(frozen=True)
class BEAConfig:
    """BEA API-specific configuration."""
    VALID_TABLES: set = frozenset({"T10101", "T20305", "T31600", "T70500"})
    DEFAULT_DATASET: str = "NIPA"
    DEFAULT_TABLE: str = "T31600"
    DEFAULT_FREQUENCY: str = "A"

LLM_CONFIG = LLMConfig()
SOURCE_WEIGHTS = SourceReliabilityWeights()
CONFIDENCE_CONFIG = ConfidenceConfig()
API_TIMEOUTS = APITimeouts()
BEA_CONFIG = BEAConfig()


class RATE_LIMITS:
    BEA = 100
    CENSUS = 500
    BLS = 500
    CONGRESS = 5000
    DATA_GOV = 1000
    GEMINI = 60

class RATE_LIMITS_PER_SECOND:
    BEA = RATE_LIMITS.BEA / 60.0
    CENSUS = RATE_LIMITS.CENSUS / (24 * 3600)
    BLS = RATE_LIMITS.BLS / (24 * 3600)
    CONGRESS = RATE_LIMITS.CONGRESS / 3600.0
    DATA_GOV = RATE_LIMITS.DATA_GOV / 3600.0
    GEMINI = RATE_LIMITS.GEMINI / 60.0
