"""Tests for the new keyless government data sources and expanded BLS coverage."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from api.usaspending import query_usaspending, _normalize_agency_query, _score_agency_match
from api.treasury import query_treasury, _humanize_usd


def _mock_client(payload, url="https://example.gov/api"):
    """Build an httpx.AsyncClient stand-in returning `payload`."""
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    response.url = url

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ------------------------------------------------------------- USAspending

@pytest.mark.asyncio
async def test_usaspending_matches_agency_and_extracts_budget():
    payload = {
        "fiscal_year": 2023,
        "results": [
            {"agency_name": "Department of Defense", "budget_authority_amount": 820000000000.0,
             "obligated_amount": 780000000000.0, "percentage_of_total_budget_authority": "11.5%"},
            {"agency_name": "Department of Education", "budget_authority_amount": 270000000000.0},
        ],
    }
    with patch("api.usaspending.httpx.AsyncClient", return_value=_mock_client(payload)):
        out = await query_usaspending({"metric": "agency_budget",
                                       "agency": "Department of Defense", "year": "2023"})

    assert len(out) == 1
    assert "error" not in out[0]
    assert out[0]["data_value"] == 820000000000.0
    assert "Department of Defense" in out[0]["title"]
    assert out[0]["year"] == "2023"
    assert "820.00 billion" in out[0]["snippet"]


@pytest.mark.asyncio
async def test_usaspending_resolves_alias_to_full_agency_name():
    payload = {"fiscal_year": 2023, "results": [
        {"agency_name": "National Aeronautics and Space Administration",
         "budget_authority_amount": 25000000000.0},
        {"agency_name": "Department of Defense", "budget_authority_amount": 820000000000.0},
    ]}
    with patch("api.usaspending.httpx.AsyncClient", return_value=_mock_client(payload)):
        out = await query_usaspending({"metric": "agency_budget", "agency": "NASA", "year": "2023"})

    assert out[0]["data_value"] == 25000000000.0
    assert "Aeronautics" in out[0]["title"]


@pytest.mark.asyncio
async def test_usaspending_total_spending_sums_agencies():
    payload = {"fiscal_year": 2023, "results": [
        {"agency_name": "A", "budget_authority_amount": 100.0},
        {"agency_name": "B", "budget_authority_amount": 250.0},
    ]}
    with patch("api.usaspending.httpx.AsyncClient", return_value=_mock_client(payload)):
        out = await query_usaspending({"metric": "total_spending", "year": "2023"})

    assert out[0]["data_value"] == 350.0


@pytest.mark.asyncio
async def test_usaspending_returns_empty_when_no_agency_matches():
    payload = {"fiscal_year": 2023, "results": [{"agency_name": "Department of Defense",
                                                 "budget_authority_amount": 1.0}]}
    with patch("api.usaspending.httpx.AsyncClient", return_value=_mock_client(payload)):
        out = await query_usaspending({"metric": "agency_budget",
                                       "agency": "Ministry of Magic", "year": "2023"})
    assert out == []


@pytest.mark.asyncio
async def test_usaspending_requires_year():
    out = await query_usaspending({"metric": "agency_budget", "agency": "DoD"})
    assert out[0]["status"] == "failed"
    assert "fiscal year" in out[0]["error"].lower()


@pytest.mark.asyncio
async def test_usaspending_handles_http_error():
    request = httpx.Request("GET", "https://api.usaspending.gov/x")
    response = httpx.Response(503, request=request, text="unavailable")
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.HTTPStatusError("err", request=request, response=response))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("api.usaspending.httpx.AsyncClient", return_value=ctx):
        out = await query_usaspending({"metric": "agency_budget", "agency": "DoD", "year": "2023"})

    assert out[0]["status"] == "failed"
    assert "503" in out[0]["error"]


def test_agency_alias_normalization():
    assert _normalize_agency_query("DoD") == "department of defense"
    assert _normalize_agency_query("nasa") == "national aeronautics and space administration"
    assert _normalize_agency_query("Department of Energy") == "department of energy"


def test_agency_match_scoring_prefers_overlap():
    assert _score_agency_match("department of defense", "Department of Defense") > \
           _score_agency_match("department of defense", "Department of Education")


# ---------------------------------------------------------------- Treasury

@pytest.mark.asyncio
async def test_treasury_debt_extracts_latest_value():
    payload = {"data": [{"record_date": "2023-09-30", "record_fiscal_year": "2023",
                         "tot_pub_debt_out_amt": "33167334044723.16"}]}
    with patch("api.treasury.httpx.AsyncClient", return_value=_mock_client(payload)):
        out = await query_treasury({"metric": "debt", "year": "2023"})

    assert len(out) == 1
    assert out[0]["data_value"] == pytest.approx(33167334044723.16)
    assert out[0]["year"] == "2023"
    assert "33.17 trillion" in out[0]["snippet"]
    assert out[0]["source"] == "TREASURY"


@pytest.mark.asyncio
async def test_treasury_returns_empty_when_no_rows():
    with patch("api.treasury.httpx.AsyncClient", return_value=_mock_client({"data": []})):
        out = await query_treasury({"metric": "debt", "year": "1776"})
    assert out == []


@pytest.mark.asyncio
async def test_treasury_handles_request_error():
    request = httpx.Request("GET", "https://api.fiscaldata.treasury.gov/x")
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.RequestError("boom", request=request))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("api.treasury.httpx.AsyncClient", return_value=ctx):
        out = await query_treasury({"metric": "debt"})

    assert out[0]["status"] == "failed"


def test_humanize_usd_scales():
    assert _humanize_usd(33_000_000_000_000) == "$33.00 trillion"
    assert _humanize_usd(820_000_000_000) == "$820.00 billion"
    assert _humanize_usd(5_000_000) == "$5.00 million"
    assert _humanize_usd(None) == "N/A"


# --------------------------------------------------------------------- BLS

@pytest.mark.asyncio
async def test_bls_supports_expanded_metrics():
    """Metrics beyond the original CPI/unemployment pair now resolve."""
    from api.bls import query_bls

    payload = {
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"data": [
            {"year": "2023", "period": "M13", "periodName": "Annual", "value": "62.5"},
        ]}]},
    }
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("api.bls.BLS_API_KEY", "test-key"), \
         patch("api.bls.httpx.AsyncClient", return_value=ctx):
        out = await query_bls({"metric": "labor_force_participation", "year": "2023"})

    assert out[0]["data_value"] == 62.5
    assert "Labor force participation" in out[0]["line_description"]


@pytest.mark.asyncio
async def test_bls_falls_back_to_latest_year_instead_of_failing():
    """Requesting a year with no data yet returns the latest, clearly labelled."""
    from api.bls import query_bls

    payload = {
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"data": [
            {"year": "2023", "period": "M13", "periodName": "Annual", "value": "3.6"},
        ]}]},
    }
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("api.bls.BLS_API_KEY", "test-key"), \
         patch("api.bls.httpx.AsyncClient", return_value=ctx):
        out = await query_bls({"metric": "unemployment", "year": "2030"})

    assert out[0]["data_value"] == 3.6
    assert out[0]["year_fallback"] is True
    assert "latest available is 2023" in out[0]["snippet"]


@pytest.mark.asyncio
async def test_bls_rejects_unknown_metric_with_helpful_message():
    from api.bls import query_bls
    with patch("api.bls.BLS_API_KEY", "test-key"):
        out = await query_bls({"metric": "gdp_growth", "year": "2023"})
    assert out[0]["status"] == "failed"
    assert "Available:" in out[0]["error"]
