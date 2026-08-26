"""Tests for source citation URL redaction.

Several sources authenticate by query parameter (BEA `UserID`, Census `key`).
httpx merges those into the request URL, so any adapter that stores
`str(response.url)` as a citation would carry them into API responses, into
extension link text, and into LLM prompt context.

The scanning test at the bottom is the important one: it walks whatever an
adapter actually returned and fails on a credential-looking value appearing
anywhere in the structure. A new adapter that stores a request URL is caught by
it without anyone remembering to write a test.
"""
import asyncio
import json
from typing import Any, Dict, List

import httpx
import pytest

from utils.urls import is_secret_param, public_url

BEA_SENTINEL = "BEA-SENTINEL-KEY-8f3a91c2"
CENSUS_SENTINEL = "CENSUS-SENTINEL-KEY-5d7b04e1"

# Every sentinel that must never appear in adapter output.
SENTINELS = (BEA_SENTINEL, CENSUS_SENTINEL)


# --------------------------------------------------------------------------
# public_url unit tests
# --------------------------------------------------------------------------

class TestPublicUrl:
    def test_strips_bea_userid_keeps_provenance(self):
        out = public_url(
            f"https://apps.bea.gov/api/data?UserID={BEA_SENTINEL}"
            "&method=GetData&TableName=T31600&Year=2023&LineCode=2"
        )
        assert BEA_SENTINEL not in out
        # The parameters that make the citation checkable must survive.
        assert "TableName=T31600" in out
        assert "Year=2023" in out
        assert "LineCode=2" in out

    def test_strips_census_key(self):
        out = public_url(
            f"https://api.census.gov/data/2023/acs/acs1?get=NAME,B19013_001E"
            f"&for=state%3A48&key={CENSUS_SENTINEL}"
        )
        assert CENSUS_SENTINEL not in out
        assert "acs1" in out

    def test_param_name_matching_is_case_insensitive(self):
        for name in ("UserID", "userid", "USERID", "UsErId"):
            assert BEA_SENTINEL not in public_url(f"https://x.gov/a?{name}={BEA_SENTINEL}")

    @pytest.mark.parametrize("name", [
        "api_key", "apikey", "API-KEY", "x-api-key", "token", "access_token",
        "registrationKey", "client_secret", "signature", "password",
        "bea_userid", "vendor_token", "some-secret",
    ])
    def test_common_credential_param_names_are_dropped(self, name):
        assert "SEKRIT" not in public_url(f"https://x.gov/a?{name}=SEKRIT&keep=1")

    def test_non_secret_params_are_preserved(self):
        out = public_url("https://x.gov/a?year=2023&geo=US&format=json")
        assert "year=2023" in out and "geo=US" in out and "format=json" in out

    def test_fails_closed_on_unparseable_input(self):
        """A URL we cannot understand loses its query rather than passing through."""
        out = public_url("http://[not-a-valid-host/path?key=SEKRIT")
        assert "SEKRIT" not in out

    def test_accepts_httpx_url_objects(self):
        url = httpx.URL("https://apps.bea.gov/api/data").copy_merge_params(
            {"UserID": BEA_SENTINEL, "Year": "2023"}
        )
        out = public_url(url)
        assert BEA_SENTINEL not in out and "Year=2023" in out

    def test_empty_input(self):
        assert public_url("") == ""
        assert public_url(None) == ""

    def test_is_secret_param(self):
        assert is_secret_param("UserID") and is_secret_param("key")
        assert not is_secret_param("Year") and not is_secret_param("TableName")
        assert not is_secret_param("")


# --------------------------------------------------------------------------
# End-to-end: drive the real adapters with a stubbed transport
# --------------------------------------------------------------------------

def _walk_strings(node: Any):
    """Yield every string anywhere in a nested dict/list structure."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _walk_strings(v)


class _StubTransport(httpx.AsyncBaseTransport):
    """Echoes a canned JSON payload while recording the real outbound URL."""

    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.seen_urls: List[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen_urls.append(str(request.url))
        return httpx.Response(
            200,
            json=self.payload,
            request=request,
            headers={"content-type": "application/json"},
        )


def _run_with_stub(monkeypatch, payload, coro_factory):
    transport = _StubTransport(payload)
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    result = asyncio.run(coro_factory())
    return result, transport


class TestAdaptersDoNotLeakCredentials:
    def test_bea_response_contains_no_api_key(self, monkeypatch):
        import api.bea as bea

        monkeypatch.setattr(bea, "BEA_API_KEY", BEA_SENTINEL)
        payload = {"BEAAPI": {"Results": {"Data": [{
            "SeriesCode": "G16046", "LineDescription": "National defense",
            "DataValue": "790,895", "Year": "2023", "UNIT_MULT": "6",
        }]}}}
        result, transport = _run_with_stub(
            monkeypatch, payload,
            lambda: bea.query_bea({
                "DataSetName": "NIPA", "TableName": "T31600",
                "Frequency": "A", "Year": "2023", "LineCode": "2",
            }),
        )

        # Sanity: the key really was sent on the wire, so the test is meaningful.
        assert any(BEA_SENTINEL in u for u in transport.seen_urls), \
            "stub never saw the key — test would pass vacuously"

        blob = json.dumps(result)
        assert BEA_SENTINEL not in blob, f"BEA API key leaked into adapter output: {blob[:400]}"

    def test_census_response_contains_no_api_key(self, monkeypatch):
        import api.census as census

        monkeypatch.setattr(census, "CENSUS_API_KEY", CENSUS_SENTINEL)
        payload = [["NAME", "B19013_001E", "state"], ["Texas", "73035", "48"]]
        result, transport = _run_with_stub(
            monkeypatch, payload,
            lambda: census.query_census_acs(params={
                "year": "2023", "dataset": "acs/acs1",
                "get": "NAME,B19013_001E", "for": "state:48",
            }),
        )

        assert any(CENSUS_SENTINEL in u for u in transport.seen_urls), \
            "stub never saw the key — test would pass vacuously"

        blob = json.dumps(result)
        assert CENSUS_SENTINEL not in blob, f"Census API key leaked into adapter output: {blob[:400]}"

    def test_no_source_field_looks_like_a_credential(self, monkeypatch):
        """Broad net: scan every string an adapter emitted for any sentinel."""
        import api.bea as bea

        monkeypatch.setattr(bea, "BEA_API_KEY", BEA_SENTINEL)
        payload = {"BEAAPI": {"Results": {"Data": [{
            "SeriesCode": "G16046", "LineDescription": "National defense",
            "DataValue": "790,895", "Year": "2023",
        }]}}}
        result, _ = _run_with_stub(
            monkeypatch, payload,
            lambda: bea.query_bea({
                "DataSetName": "NIPA", "TableName": "T31600",
                "Frequency": "A", "Year": "2023", "LineCode": "2",
            }),
        )

        for text in _walk_strings(result):
            for sentinel in SENTINELS:
                assert sentinel not in text, f"credential surfaced in adapter output: {text[:200]}"
