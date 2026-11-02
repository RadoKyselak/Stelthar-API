import pytest
import os
import sys
from pathlib import Path
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    """Set up test environment variables before any imports."""
    env_vars = {
        "GEMINI_API_KEY": "test_gemini_key",
        "BEA_API_KEY": "test_bea_key",
        "CENSUS_API_KEY": "test_census_key",
        "BLS_API_KEY": "test_bls_key",
        "CONGRESS_API_KEY": "test_congress_key",
        "DATA_GOV_API_KEY": "test_datagov_key",
        "GEMINI_MODEL": "gemini-3.8-flash"
    }
    for key, value in env_vars.items():
        os.environ[key] = value
    yield
    # Cleanup
    for key in env_vars.keys():
        os.environ.pop(key, None)


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Mock all required environment variables."""
    env_vars = {
        "GEMINI_API_KEY": "test_gemini_key",
        "BEA_API_KEY": "test_bea_key",
        "CENSUS_API_KEY": "test_census_key",
        "BLS_API_KEY": "test_bls_key",
        "CONGRESS_API_KEY": "test_congress_key",
        "DATA_GOV_API_KEY": "test_datagov_key",
        "GEMINI_MODEL": "gemini-3.8-flash"
    }
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)
    return env_vars


@pytest.fixture(autouse=True)
def generous_request_limits(monkeypatch):
    """The suite sends many requests from one test client; per-visitor limits
    are tested on their own in test_web_security."""
    import config
    for name in ("VERIFY_PER_MINUTE", "VERIFY_PER_DAY", "LOOKUP_PER_MINUTE", "LOOKUP_PER_DAY"):
        monkeypatch.setattr(config, name, 10**6)


@pytest.fixture(autouse=True)
def finder_off(monkeypatch):
    """The dataset finder calls Claude. Off unless a test turns it on (with a
    fake client), so no test can reach Anthropic even with a test key set."""
    import config
    monkeypatch.setattr(config, "AI_FINDER", False)
    monkeypatch.setattr(config, "AI_INTERPRETER", False)


@pytest.fixture
def test_client():
    """Create a TestClient for FastAPI app."""
    import main
    return TestClient(main.app)


@pytest.fixture
def mock_httpx_client():
    """Mock httpx.AsyncClient for API calls."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()
    return mock_client


@pytest.fixture
def sample_gemini_response():
    """Sample Gemini API response."""
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": '{"claim_normalized": "Test claim", "claim_type": "quantitative_value", "entities": ["Test"], "relationship": "equals", "api_plan": {"tier1_params": {"bea": null, "census_acs": null, "bls": null}, "tier2_keywords": ["test keyword"]}}'
                        }
                    ]
                }
            }
        ]
    }


@pytest.fixture
def sample_bea_response():
    """Sample BEA API response."""
    return {
        "BEAAPI": {
            "Results": {
                "Data": [
                    {
                        "LineCode": "2",
                        "LineDescription": "National defense",
                        "DataValue": "790895",
                        "TimePeriod": "2023",
                        "Unit": "Millions of Dollars",
                        "UnitMultiplier": 1000000
                    }
                ]
            }
        }
    }

@pytest.fixture
def sample_census_response():
    """Sample Census API response."""
    return [
        ["NAME", "DP05_0001E", "state"],
        ["Alabama", "5024279", "01"]
    ]

@pytest.fixture
def sample_bls_response():
    """Sample BLS API response."""
    return {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {
                    "seriesID": "CUSR0000SA0",
                    "data": [
                        {"year": "2023", "period": "M13", "value": "304.702"},
                        {"year": "2022", "period": "M13", "value": "292.655"}
                    ]
                }
            ]
        }
    }


@pytest.fixture(autouse=True)
def computed_checks_offline(monkeypatch):
    """The computed checks read FRED, ALFRED and Treasury live. No series
    unless a test supplies one, so no test depends on today's data."""
    from services import derived

    async def no_rows(*a, **k):
        return []
    monkeypatch.setattr(derived, "_rows", no_rows)
    monkeypatch.setattr(derived, "_bls_monthly", no_rows)
    monkeypatch.setattr(derived, "_debt_held_by_public", lambda *a, **k: _none())
    # openFDA: nothing found unless a test supplies records.
    from evidence.sources import openfda

    async def no_fda(*a, **k):
        return {"results": [], "total": 0}
    monkeypatch.setattr(openfda, "_get", no_fda)
    # FoodData Central: nothing found unless a test supplies foods.
    from evidence.sources import fooddata

    async def no_food(*a, **k):
        return None
    monkeypatch.setattr(fooddata, "_get", no_food)


async def _none():
    return None
