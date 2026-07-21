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

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
EMBEDDING_MODEL_NAME = "text-embedding-004"
GEMINI_EMBED_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL_NAME}:embedContent"
GEMINI_BATCH_EMBED_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL_NAME}:batchEmbedContents"

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
