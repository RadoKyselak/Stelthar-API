import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# API Keys
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BEA_API_KEY = os.getenv("BEA_API_KEY")
CENSUS_API_KEY = os.getenv("CENSUS_API_KEY")
CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY")
DATA_GOV_API_KEY = os.getenv("DATA_GOV_API_KEY")
BLS_API_KEY = os.getenv("BLS_API_KEY")
LIVE_WEB_SEARCH_URL = os.getenv("LIVE_WEB_SEARCH_URL")

# Gemini Config
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
EMBEDDING_MODEL_NAME = "text-embedding-004"
GEMINI_EMBED_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL_NAME}:embedContent"
GEMINI_BATCH_EMBED_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL_NAME}:batchEmbedContents"

# Known BEA data tables
BEA_VALID_TABLES = {"T10101", "T20305", "T31600", "T70500"}

# Required API keys
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
    logger.info("Startup: checking for required API keys...")
    missing = [k for k in REQUIRED_KEYS if k != "DATA_GOV_API_KEY" and not os.getenv(k)]
    if missing:
        logger.warning(
            "Missing critical env vars: %s. Corresponding API calls will fail.",
            ", ".join(missing)
        )
    else:
        logger.info("All critical API keys seem present.")
