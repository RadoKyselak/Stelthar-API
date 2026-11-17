"""POST /mcp: the checks as tools for an assistant (routes/mcp.py).

Protocol messages are written by hand here, as a client would send them.
What is held: the handshake, the tool list, each tool's answer, and that no
tool can reach a paid tier or outrun the per-visitor limits.
"""
from unittest.mock import AsyncMock, patch

import pytest

import config
from claims.verdict import CONTRADICTED, HIGH, LOW, NO_SERIES, NO_VERDICT, SUPPORTED
from routes import mcp
from services import check_text as ct
from services.verify_v2 import VerificationV2


@pytest.fixture(autouse=True)
def fresh_counters():
    from utils import spend_guard
    spend_guard._reset_for_tests()
    yield
    spend_guard._reset_for_tests()


@pytest.fixture
def engine():
    calls = []

    async def fake(claim, allow_grounding=True, client_ip=None):
        calls.append((claim, allow_grounding))
        if "parade" in claim:
            return VerificationV2(claim=claim, verdict=NO_VERDICT, confidence=LOW, headline="h",
                                  reason=NO_SERIES, tier="none")
        verdict = CONTRADICTED if "9.9%" in claim else SUPPORTED
        return VerificationV2(
            claim=claim, verdict=verdict, confidence=HIGH, tier="structured",
            headline="Claim says 3.6%. BLS reports 3.6% for 2023.",
            points=[{"label": None, "verdict": verdict, "claim": "3.6%", "official": "3.6%", "period": "2023",
                     "basis": "annual average", "place": "United States", "source": "BLS", "gap": "",
                     "comparator": "equals", "measure": "Unemployment rate",
                     "citation": "U.S. Bureau of Labor Statistics, series LNS14000000, 2023: 3.6%.",
                     "citation_html": "", "citation_inline": "", "url": "https://data.bls.gov/x", "excerpt": None}],
            evidence=[{"source": "BLS", "source_url": "https://data.bls.gov/timeseries/LNS14000000",
                       "series_id": "LNS14000000", "label": "Unemployment rate", "period_label": "2023"}])

    with patch.object(ct, "verify", fake):
        yield calls


def rpc(test_client, method, params=None, msg_id=1, **kw):
    body = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    return test_client.post("/mcp", json=body, headers={"accept": "application/json, text/event-stream"}, **kw)


def call(test_client, name, arguments):
    return rpc(test_client, "tools/call", {"name": name, "arguments": arguments}).json()["result"]


class TestHandshake:
    def test_initialize_agrees_a_version_and_offers_tools(self, test_client):
        r = rpc(test_client, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                            "clientInfo": {"name": "t", "version": "0"}})
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
        got = r.json()
        assert got["jsonrpc"] == "2.0" and got["id"] == 1
        assert got["result"]["protocolVersion"] == "2025-03-26"
        assert "tools" in got["result"]["capabilities"] and got["result"]["serverInfo"]["name"] == "project-mirador"

    def test_an_unknown_version_gets_the_newest(self, test_client):
        got = rpc(test_client, "initialize", {"protocolVersion": "1999-01-01"}).json()
        assert got["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]

    def test_a_notification_is_accepted_with_no_body(self, test_client):
        r = rpc(test_client, "notifications/initialized", msg_id=None)
        assert r.status_code == 202 and r.content == b""

    def test_ping(self, test_client):
        assert rpc(test_client, "ping").json() == {"jsonrpc": "2.0", "id": 1, "result": {}}

    def test_no_stream_is_offered(self, test_client):
        r = test_client.get("/mcp", headers={"accept": "text/event-stream"})
        assert r.status_code == 405 and r.headers["allow"] == "POST"


class TestTools:
    def test_the_list_names_three_tools_with_schemas(self, test_client):
        tools = rpc(test_client, "tools/list").json()["result"]["tools"]
        assert [t["name"] for t in tools] == ["check_text", "check_claim", "find_figure"]
        for t in tools:
            assert t["inputSchema"]["type"] == "object" and t["inputSchema"]["required"]
            assert t["description"] and t["annotations"]["readOnlyHint"] is True

    def test_check_claim(self, test_client, engine):
        got = call(test_client, "check_claim", {"claim": "Unemployment was 3.6% in 2023."})
        assert got["isError"] is False
        text = got["content"][0]["text"]
        assert text.startswith("Supported: “Unemployment was 3.6% in 2023.”")
        assert "Official figure: 3.6% (Unemployment rate, United States, 2023)" in text
        assert "Source: U.S. Bureau of Labor Statistics https://data.bls.gov/" in text
        s = got["structuredContent"]
        assert s["verdict"] == "Supported" and s["official_figure"] == "3.6%" and s["period"] == "2023"

    def test_check_text_answers_each_claim(self, test_client, engine):
        got = call(test_client, "check_text", {
            "text": "Unemployment was 3.6% in 2023. Inflation was 9.9% in 2022. "
                    "About 2 million people attended the parade last weekend."})
        s = got["structuredContent"]
        assert [c["verdict"] for c in s["claims"]] == ["Supported", "Contradicted", "Can't verify"]
        assert s["tally"] == {"supported": 1, "contradicted": 1, "no-verdict": 1}
        assert got["content"][0]["text"].startswith("3 claims checked against official data.")
        assert "None of the official data series we check covers this claim." in got["content"][0]["text"]

    def test_find_figure(self, test_client):
        found = type("R", (), dict(
            query="q", ok=True, reason="", headline="", value="4.1%", metric="unemployment_rate",
            label="Unemployment rate", evidence=[], also=[], suggestions=[], degraded=False, model_calls=0,
            elapsed_seconds=0.1, note="", deeper=False,
            citations={"value": "4.1%", "attribution": "BLS, Texas, 2023 annual average",
                       "url": "https://data.bls.gov/x", "note": "U.S. Bureau of Labor Statistics ... 4.1%."}))()
        fake = AsyncMock(return_value=found)
        with patch.object(mcp, "lookup", fake):
            got = call(test_client, "find_figure", {"query": "unemployment rate texas 2023"})
        assert got["structuredContent"]["figure"] == "4.1%"
        assert got["content"][0]["text"].startswith("Unemployment rate: 4.1% (BLS, Texas, 2023 annual average)")
        assert fake.await_args.kwargs["deep"] is False        # never the paid dataset search

    def test_a_miss_says_what_to_try(self, test_client):
        miss = type("R", (), dict(
            query="q", ok=False, reason="no_match", headline="No official series matches that search.",
            value="", metric="", label="", evidence=[], also=[], degraded=False, model_calls=0,
            elapsed_seconds=0.1, note="", deeper=False, citations={},
            suggestions=[{"example": "unemployment rate 2023", "label": "x", "source": "BLS"}]))()
        with patch.object(mcp, "lookup", AsyncMock(return_value=miss)):
            got = call(test_client, "find_figure", {"query": "happiness index"})
        assert got["isError"] is False and got["structuredContent"]["found"] is False
        assert "Try: unemployment rate 2023." in got["content"][0]["text"]


class TestNeverPaid:
    def test_one_claim_stays_on_agency_data(self, test_client, engine):
        call(test_client, "check_claim", {"claim": "Unemployment was 3.6% in 2023."})
        assert engine == [("Unemployment was 3.6% in 2023.", False)]

    def test_a_passage_stays_on_agency_data(self, test_client, engine):
        call(test_client, "check_text", {"text": "Unemployment was 3.6% in 2023. Inflation was 9.9% in 2022."})
        assert all(grounding is False for _, grounding in engine) and len(engine) == 2

    def test_it_shares_the_visitors_allowance(self, test_client, engine, monkeypatch):
        monkeypatch.setattr(config, "VERIFY_PER_MINUTE", 1)
        first = call(test_client, "check_claim", {"claim": "Unemployment was 3.6% in 2023."})
        second = call(test_client, "check_claim", {"claim": "Unemployment was 3.6% in 2023."})
        assert first["isError"] is False and second["isError"] is True
        assert "Too many requests" in second["content"][0]["text"] and len(engine) == 1


class TestBadInput:
    def test_an_unknown_tool(self, test_client):
        got = rpc(test_client, "tools/call", {"name": "delete_everything", "arguments": {}}).json()
        assert got["error"]["code"] == -32602

    def test_a_missing_argument_is_a_tool_error_the_assistant_can_read(self, test_client):
        got = call(test_client, "check_claim", {})
        assert got["isError"] is True and "claim" in got["content"][0]["text"]

    def test_text_over_the_cap(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "CHECK_MAX_CHARS", 700)
        got = call(test_client, "check_text", {"text": "x " * 500})
        assert got["isError"] is True and "700" in got["content"][0]["text"]

    def test_an_unknown_method(self, test_client):
        assert rpc(test_client, "resources/list").json()["error"]["code"] == -32601

    def test_not_json_rpc(self, test_client):
        r = test_client.post("/mcp", json={"hello": "world"})
        assert r.json()["error"]["code"] == -32600

    def test_not_json(self, test_client):
        r = test_client.post("/mcp", content=b"not json", headers={"content-type": "application/json"})
        assert r.status_code == 400 and r.json()["error"]["code"] == -32700

    def test_a_tool_that_breaks_still_answers(self, test_client):
        with patch.dict(mcp._HANDLERS, {"check_claim": AsyncMock(side_effect=RuntimeError("boom"))}):
            got = call(test_client, "check_claim", {"claim": "Unemployment was 3.6% in 2023."})
        assert got["isError"] is True and "not a judgment about the claim" in got["content"][0]["text"]


class TestSwitch:
    def test_off_takes_the_endpoint_away(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "MCP_ENABLED", False)
        assert rpc(test_client, "ping").status_code == 404
        assert test_client.get("/mcp").status_code == 404
