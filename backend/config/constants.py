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
    TREASURY: float = 1.0
    USASPENDING: float = 0.95
    CONGRESS: float = 0.8
    DATA_GOV: float = 0.7
    SEARCH_GOV: float = 0.65  # official site, but the LLM extracted this — lower than structured data
    DEFAULT: float = 0.6

    def get_weight_for_url(self, url: str) -> float:
        url_lower = (url or "").lower()
        if "apps.bea.gov" in url_lower:
            return self.BEA
        elif "api.census.gov" in url_lower:
            return self.CENSUS
        elif "api.bls.gov" in url_lower or "data.bls.gov" in url_lower:
            return self.BLS
        elif "fiscaldata.treasury.gov" in url_lower or "treasury.gov" in url_lower:
            return self.TREASURY
        elif "usaspending.gov" in url_lower:
            return self.USASPENDING
        elif "api.congress.gov" in url_lower or "congress.gov" in url_lower:
            return self.CONGRESS
        elif "catalog.data.gov" in url_lower or "data.gov" in url_lower:
            return self.DATA_GOV
        elif ".gov" in url_lower or ".mil" in url_lower:
            # Anything else on a federal domain — e.g. a GAO/CBO/agency report
            # surfaced by the search.gov research harness. Official, but the
            # value was LLM-extracted from prose rather than a structured API,
            # so it's trusted less than the dedicated structured sources above.
            return self.SEARCH_GOV
        else:
            return self.DEFAULT

@dataclass(frozen=True)
class ConfidenceConfig:
    # Relevance (S) now dominates so that highly-reliable-but-irrelevant sources
    # (e.g. a Data.gov catalog page that mentions the topic but contains no
    # answer) can no longer prop up the score. Reliability still matters, but a
    # .gov URL alone is not evidence.
    R_WEIGHT: float = 0.30
    S_WEIGHT: float = 0.45
    E_WEIGHT: float = 0.25

    # Within S, weight the embedding-based claim<->evidence similarity as heavily
    # as the verdict signal, so semantic irrelevance actually drags the score.
    S_LLM_WEIGHT: float = 0.5
    S_EMBEDDING_WEIGHT: float = 0.5

    VERDICT_CONFIDENCE_SUPPORTED: float = 0.95
    VERDICT_CONFIDENCE_CONTRADICTED: float = 0.92
    VERDICT_CONFIDENCE_INCONCLUSIVE: float = 0.35

    MAX_SOURCES_FOR_FULL_DENSITY: int = 4

    HIGH_THRESHOLD: float = 0.75
    MEDIUM_THRESHOLD: float = 0.5

    DEFAULT_CONFIDENCE: float = 0.25
    DEFAULT_R: float = 0.5
    DEFAULT_E: float = 0.0
    DEFAULT_S: float = 0.2
    DEFAULT_S_SEMANTIC: float = 0.0

    # Minimum claim<->evidence cosine similarity for a source to be treated as
    # "on-topic". Sources below this are flagged by the relevance gate.
    RELEVANCE_SIMILARITY_FLOOR: float = 0.55

@dataclass(frozen=True)
class APITimeouts:
    """Timeout configurations for external API calls."""
    BEA: float = 25.0
    CENSUS: float = 25.0
    BLS: float = 25.0
    CONGRESS: float = 25.0
    DATA_GOV: float = 20.0
    TREASURY: float = 25.0
    USASPENDING: float = 25.0
    SEARCH_GOV: float = 20.0

@dataclass(frozen=True)
class BEAConfig:
    """BEA API-specific configuration."""
    # Broadened well beyond the original 4-table whitelist so the planner can
    # actually reach GDP components, prices, personal income & outlays, gov
    # receipts/expenditures, and trade — the tables most claims land on.
    VALID_TABLES: frozenset = frozenset({
        "T10101",  # Real GDP, percent change
        "T10105",  # GDP (current $)
        "T10106",  # Real GDP (chained $)
        "T10501",  # GDP by major type of product
        "T20100",  # Personal income & its disposition
        "T20305",  # Personal consumption expenditures by major type
        "T20600",  # Personal income & outlays (monthly)
        "T30100",  # Government current receipts & expenditures
        "T31600",  # Government current expenditures by function
        "T40100",  # Foreign transactions (exports/imports)
        "T50100",  # Saving and investment
        "T70500",  # Employment by industry
    })
    DEFAULT_DATASET: str = "NIPA"
    DEFAULT_TABLE: str = "T31600"
    DEFAULT_FREQUENCY: str = "A"


@dataclass(frozen=True)
class AgentConfig:
    """Controls the multi-pass (agentic) verification loop."""
    MAX_ITERATIONS: int = 3          # plan/retrieve/critique cycles before concluding
    ITERATION_TIMEOUT_SECONDS: float = 18.0
    GLOBAL_TIMEOUT_SECONDS: float = 45.0


# BLS series catalog — maps friendly metric names (what the planner emits) to
# BLS series ids and how to interpret them. Vastly expanded from the original
# 2-series map (national CPI + national unemployment).
#   kind: "rate"  -> value is already a percentage/level, read latest annual avg
#   kind: "cpi"   -> compute YoY inflation from the index
BLS_SERIES_CATALOG: Dict[str, Dict[str, str]] = {
    "unemployment":            {"series_id": "LNS14000000", "kind": "rate",  "label": "National unemployment rate", "unit": "%"},
    "labor_force_participation": {"series_id": "LNS11300000", "kind": "rate", "label": "Labor force participation rate", "unit": "%"},
    "employment_level":        {"series_id": "CES0000000001", "kind": "rate", "label": "Total nonfarm employment", "unit": "thousands"},
    "avg_hourly_earnings":     {"series_id": "CES0500000003", "kind": "rate", "label": "Average hourly earnings, private", "unit": "USD"},
    "cpi":                     {"series_id": "CUUR0000SA0",  "kind": "cpi",   "label": "CPI-U, all items", "unit": "index"},
    "cpi_core":                {"series_id": "CUUR0000SA0L1E", "kind": "cpi", "label": "CPI-U, less food & energy", "unit": "index"},
    "ppi":                     {"series_id": "WPUFD4",       "kind": "cpi",   "label": "PPI, final demand", "unit": "index"},
}

LLM_CONFIG = LLMConfig()
SOURCE_WEIGHTS = SourceReliabilityWeights()
CONFIDENCE_CONFIG = ConfidenceConfig()
API_TIMEOUTS = APITimeouts()
BEA_CONFIG = BEAConfig()
AGENT_CONFIG = AgentConfig()


class RATE_LIMITS:
    BEA = 100
    CENSUS = 500
    BLS = 500
    CONGRESS = 5000
    DATA_GOV = 1000
    GEMINI = 60

class RATE_LIMITS_PER_SECOND:
    """Legacy per-second rates. Retained for backwards compatibility only.

    Do NOT use these for daily quotas: dividing a daily allowance by 86400 and
    feeding it to the interval-based limiter forces a ~173-second gap between
    consecutive calls. Use RATE_LIMIT_QUOTAS with get_quota_limiter instead.
    """
    BEA = RATE_LIMITS.BEA / 60.0
    CENSUS = RATE_LIMITS.CENSUS / (24 * 3600)
    BLS = RATE_LIMITS.BLS / (24 * 3600)
    CONGRESS = RATE_LIMITS.CONGRESS / 3600.0
    DATA_GOV = RATE_LIMITS.DATA_GOV / 3600.0
    GEMINI = RATE_LIMITS.GEMINI / 60.0


class RATE_LIMIT_QUOTAS:
    """(max_calls, window_seconds) — the actual published quotas.

    Enforced with a sliding window so calls run at full speed until the quota is
    genuinely exhausted, instead of being artificially spaced apart.
    """
    BEA = (100, 60)             # 100 per minute
    CENSUS = (500, 24 * 3600)   # 500 per day
    BLS = (500, 24 * 3600)      # 500 per day (registered key)
    CONGRESS = (5000, 3600)     # 5000 per hour
    DATA_GOV = (1000, 3600)     # 1000 per hour
    GEMINI = (60, 60)           # 60 per minute
    TREASURY = (120, 60)        # no published hard cap; stay courteous
    USASPENDING = (120, 60)     # no published hard cap; stay courteous
    SEARCH_GOV = (60, 60)       # conservative default; raise once real limits are confirmed
