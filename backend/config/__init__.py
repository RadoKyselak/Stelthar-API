import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BEA_API_KEY = os.getenv("BEA_API_KEY")
CENSUS_API_KEY = os.getenv("CENSUS_API_KEY")
CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY")
DATA_GOV_API_KEY = os.getenv("DATA_GOV_API_KEY")
BLS_API_KEY = os.getenv("BLS_API_KEY")

# search.gov (search.usa.gov) — free federal-site search, the research harness
# for anything the structured sources below can't answer. Sign up at
# https://search.gov/ to get these.
SEARCH_GOV_AFFILIATE = os.getenv("SEARCH_GOV_AFFILIATE")
SEARCH_GOV_API_KEY = os.getenv("SEARCH_GOV_API_KEY")

# gemini-2.5-flash was retired for this project's key in 2026-10 and every call
# 404'd, silently degrading the whole service. GEMINI_MODEL is now a preference,
# not a single point of failure: utils.gemini_models walks the fallback list
# when a model reports itself gone. Override either via env without a deploy.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip()
GEMINI_FALLBACK_MODELS = [
    m.strip() for m in os.getenv(
        "GEMINI_FALLBACK_MODELS", f"{DEFAULT_GEMINI_MODEL},gemini-flash-latest"
    ).split(",") if m.strip()
]
GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
# text-embedding-004 was shut down 2026-01-14; gemini-embedding-001 replaces it.
EMBEDDING_MODEL_NAME = (os.getenv("GEMINI_EMBEDDING_MODEL") or "gemini-embedding-001").strip()
GEMINI_EMBED_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL_NAME}:embedContent"
GEMINI_BATCH_EMBED_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL_NAME}:batchEmbedContents"

# ---------------------------------------------------------------------------
# AI search tier (claims no official series covers) — and its spend limits.
#
# Every number below is a HARD cap enforced in code before a request is sent.
# Values are clamped to a safe range, so a typo in Vercel cannot lift a cap
# to something dangerous. The Anthropic Console workspace spend limit is the
# backstop above all of these.
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
# "claude" | "gemini" | "off". Default: Claude when its key is set, else off.
# Gemini is only used when chosen explicitly.
AI_SEARCH_PROVIDER = (os.getenv("AI_SEARCH_PROVIDER") or ("claude" if ANTHROPIC_API_KEY else "off")).strip().lower()
CLAUDE_SEARCH_MODEL = (os.getenv("CLAUDE_SEARCH_MODEL") or "claude-haiku-5-5").strip()

# Per-call caps.
AI_MAX_SEARCHES_PER_CALL = _env_int("AI_MAX_SEARCHES_PER_CALL", 2, 1, 3)
AI_MAX_OUTPUT_TOKENS = _env_int("AI_MAX_OUTPUT_TOKENS", 1500, 512, 4000)
AI_MAX_CLAIM_CHARS = _env_int("AI_MAX_CLAIM_CHARS", 300, 40, 600)
AI_REQUEST_TIMEOUT_SECONDS = _env_float("AI_REQUEST_TIMEOUT_SECONDS", 45.0, 5.0, 90.0)

# Daily caps (UTC day). Shared across all instances when Upstash Redis is
# configured; otherwise per serverless instance (weaker — see spend_guard).
AI_MAX_CALLS_PER_DAY = _env_int("AI_MAX_CALLS_PER_DAY", 100, 0, 5000)
AI_MAX_CALLS_PER_CLIENT_PER_DAY = _env_int("AI_MAX_CALLS_PER_CLIENT_PER_DAY", 10, 0, 500)
AI_MAX_SPEND_USD_PER_DAY = _env_float("AI_MAX_SPEND_USD_PER_DAY", 1.00, 0.0, 50.0)

# Shared counter store (Upstash Redis REST; Vercel's Upstash/KV integration
# sets the KV_* names).
REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL")
REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN")

# The old multi-call planner spends 4-8 model calls per request with no caps.
# Off unless explicitly enabled.
ENABLE_LEGACY_V1 = (os.getenv("ENABLE_LEGACY_V1") or "").strip().lower() in ("1", "true", "yes")

# Keyless government data endpoints (no API key required)
USASPENDING_BASE_URL = os.getenv("USASPENDING_BASE_URL", "https://api.usaspending.gov")
TREASURY_FISCAL_BASE_URL = os.getenv(
    "TREASURY_FISCAL_BASE_URL",
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service",
)

from .constants import (
    LLM_CONFIG,
    SOURCE_WEIGHTS,
    CONFIDENCE_CONFIG,
    API_TIMEOUTS,
    BEA_CONFIG,
    AGENT_CONFIG,
    BLS_SERIES_CATALOG,
)

BEA_VALID_TABLES = BEA_CONFIG.VALID_TABLES

REQUIRED_KEYS = [
    "GEMINI_API_KEY",
    "BEA_API_KEY",
    "CENSUS_API_KEY",
    "BLS_API_KEY",
    "CONGRESS_API_KEY",
    "DATA_GOV_API_KEY"
]

def check_api_keys_on_startup():
    """Check for required API keys on startup."""
    missing_keys = []
    for key_name in REQUIRED_KEYS:
        if not globals().get(key_name):
            missing_keys.append(key_name)
    
    if missing_keys:
        logger.warning(f"Missing API keys: {', '.join(missing_keys)}")
    else:
        logger.info("All required API keys are configured.")

__all__ = [
    "logger",
    "GEMINI_API_KEY",
    "BEA_API_KEY",
    "CENSUS_API_KEY",
    "CONGRESS_API_KEY",
    "DATA_GOV_API_KEY",
    "BLS_API_KEY",
    "GEMINI_ENDPOINT",
    "GEMINI_MODEL",
    "DEFAULT_GEMINI_MODEL",
    "GEMINI_FALLBACK_MODELS",
    "GEMINI_EMBED_ENDPOINT",
    "GEMINI_BATCH_EMBED_ENDPOINT",
    "EMBEDDING_MODEL_NAME",
    "BEA_VALID_TABLES",
    "SEARCH_GOV_AFFILIATE",
    "SEARCH_GOV_API_KEY",
    "USASPENDING_BASE_URL",
    "TREASURY_FISCAL_BASE_URL",
    "check_api_keys_on_startup",
    "LLM_CONFIG",
    "SOURCE_WEIGHTS",
    "CONFIDENCE_CONFIG",
    "API_TIMEOUTS",
    "BEA_CONFIG",
    "AGENT_CONFIG",
    "BLS_SERIES_CATALOG",
]
