"""Counting checks (utils/usage.py) and reading the counts (GET /usage)."""
from unittest.mock import patch

import pytest

import config
from utils import spend_guard, usage


@pytest.fixture(autouse=True)
def fresh_store():
    spend_guard._reset_for_tests()
    spend_guard._store = spend_guard._MemoryStore()
    yield
    spend_guard._reset_for_tests()


class TestCounting:
    async def test_a_count_goes_to_today_and_to_the_total(self):
        await usage.count("claim")
        await usage.count("claims", 5)
        got = await usage.report(days=1)
        assert got["totals"] == {"claim": 1, "claims": 5} and got["claims_checked"] == 6
        assert got["by_day"][0]["claim"] == 1

    async def test_verdicts_are_tallied_and_an_unknown_one_is_no_verdict(self):
        await usage.count_verdicts(["supported", "supported", "contradicted", "something-new"])
        totals = (await usage.report(days=1))["totals"]
        assert totals == {"verdict:supported": 2, "verdict:contradicted": 1, "verdict:no-verdict": 1}

    async def test_ai_spend_is_reported_in_dollars(self):
        await spend_guard.record_spend(0.0123)
        totals = (await usage.report(days=1))["totals"]
        assert totals["ai_calls"] == 1 and totals["ai_spend_usd"] == pytest.approx(0.0123)

    async def test_without_a_shared_store_the_report_says_it_is_not_reliable(self):
        got = await usage.report(days=1)
        assert got["reliable"] is False and "one server instance" in got["note"]

    async def test_a_store_that_fails_loses_the_count_not_the_answer(self):
        class Broken(spend_guard._MemoryStore):
            async def incr(self, *a, **k):
                raise spend_guard.StoreUnavailable("down")
        spend_guard._store = Broken()
        await usage.count("claim")            # does not raise

    async def test_nothing_about_the_claim_is_in_any_key(self):
        await usage.count("claim")
        keys = list(spend_guard._store._data)
        assert all(k.startswith(("use:", "ai:")) for k in keys) and len(keys) == 2


class TestTheEndpoint:
    def test_hidden_without_the_operator_token(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "HEALTH_TOKEN", "secret")
        assert test_client.get("/usage").status_code == 404
        assert test_client.get("/usage", headers={"x-health-token": "wrong"}).status_code == 404

    def test_hidden_when_no_token_is_set(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "HEALTH_TOKEN", "")
        assert test_client.get("/usage", headers={"x-health-token": ""}).status_code == 404

    def test_a_passage_check_is_counted(self, test_client, monkeypatch):
        from routes import check as route
        monkeypatch.setattr(config, "HEALTH_TOKEN", "secret")

        async def fake(text, mode="text", client_ip=None, said_on=None):
            one = {"result": {"display": {"verdict": {"key": "supported"}}}}
            return {"claims": [one, one], "count": 2, "tally": {}, "truncated": False, "model_calls": 0,
                    "mode": "text", "unsettled": 0, "elapsed_seconds": 0}
        with patch.object(route, "check_text", fake):
            assert test_client.post("/v2/check", json={"text": "Two claims here, both about numbers."}).status_code == 200
        got = test_client.get("/usage", headers={"x-health-token": "secret"}).json()
        assert got["totals"]["passage"] == 1 and got["totals"]["claims"] == 2
        assert got["totals"]["verdict:supported"] == 2 and got["claims_checked"] == 2
