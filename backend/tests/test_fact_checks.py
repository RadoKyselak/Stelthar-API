"""Related fact-checks (Google Fact Check Tools): offline, no key, no network."""
import asyncio

import pytest

from evidence import fact_checks as fc


def review(claim, publisher="PolitiFact", rating="False", url="https://www.politifact.com/x/",
           date="2024-03-12T00:00:00Z", title=None):
    return {"text": claim, "claimant": "Someone",
            "claimReview": [{"publisher": {"name": publisher, "site": "politifact.com"}, "url": url,
                             "title": title or claim, "reviewDate": date, "textualRating": rating}]}


CLAIM = "Crime fell 10% in 2023"


class TestPick:
    def test_a_similar_claim_is_kept_with_its_own_rating(self):
        [got] = fc.pick(CLAIM, [review("Violent crime fell 10% in 2023, FBI data show", rating="Mostly True")])
        assert got["rating"] == "Mostly True" and got["publisher"] == "PolitiFact"
        assert got["date"] == "2024-03-12" and got["url"].startswith("https://")

    def test_an_unrelated_claim_is_dropped(self):
        assert fc.pick(CLAIM, [review("The moon landing was staged in a studio")]) == []

    def test_only_https_links(self):
        assert fc.pick(CLAIM, [review("Crime fell 10% in 2023", url="http://x.com/a"),
                               review("Crime fell 10% in 2023", url="javascript:alert(1)")]) == []

    def test_at_most_three_and_no_duplicates(self):
        found = [review("Crime fell 10% in 2023", url=f"https://a.org/{i % 4}") for i in range(10)]
        got = fc.pick(CLAIM, found)
        assert len(got) == 3 and len({g["url"] for g in got}) == 3

    def test_the_same_figure_ranks_first(self):
        got = fc.pick(CLAIM, [review("Crime fell sharply in 2023", url="https://a.org/1"),
                              review("Crime fell 10% in 2023", url="https://a.org/2")])
        assert got[0]["url"] == "https://a.org/2"

    def test_long_text_is_clipped(self):
        [got] = fc.pick(CLAIM, [review("Crime fell 10% in 2023 " + "very " * 100, rating="x" * 200)])
        assert len(got["rating"]) <= 60 and len(got["claim"]) <= 200


@pytest.mark.asyncio
class TestSearch:
    async def test_no_key_no_call(self, monkeypatch):
        monkeypatch.setattr(fc.config, "GOOGLE_FACTCHECK_API_KEY", None, raising=False)

        async def boom(q):
            raise AssertionError("must not call Google without a key")
        monkeypatch.setattr(fc, "_query", boom)
        assert await fc.search(CLAIM) == [] and fc.start(CLAIM) is None

    async def test_falls_back_to_keywords(self, monkeypatch):
        monkeypatch.setattr(fc.config, "GOOGLE_FACTCHECK_API_KEY", "k", raising=False)
        asked = []

        async def query(q):
            asked.append(q)
            return [] if len(asked) == 1 else [review("Crime fell 10% in 2023")]
        monkeypatch.setattr(fc, "_query", query)
        assert len(await fc.search(CLAIM)) == 1 and asked[1] == "crime fell"

    async def test_errors_never_reach_the_check(self, monkeypatch):
        monkeypatch.setattr(fc.config, "GOOGLE_FACTCHECK_API_KEY", "k", raising=False)

        async def query(q):
            raise RuntimeError("down")
        monkeypatch.setattr(fc, "_query", query)
        assert await fc.search(CLAIM) == []

    async def test_a_slow_search_is_dropped(self, monkeypatch):
        monkeypatch.setattr(fc, "_WAIT", 0.05)

        async def slow():
            await asyncio.sleep(5)
            return [{"x": 1}]
        assert await fc.finish(asyncio.create_task(slow())) == []

    async def test_the_key_is_never_logged(self, monkeypatch, caplog):
        monkeypatch.setattr(fc.config, "GOOGLE_FACTCHECK_API_KEY", "SECRET-KEY-123", raising=False)

        async def no_cache(ns, key, ttl, loader):
            return await loader()
        monkeypatch.setattr(fc, "cached", no_cache)

        class Resp:
            status_code = 403

        class Client:
            def __init__(self, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, params=None):
                assert params["key"] == "SECRET-KEY-123"
                return Resp()
        monkeypatch.setattr(fc.httpx, "AsyncClient", Client)
        assert await fc._query(CLAIM) == []
        assert "SECRET-KEY-123" not in caplog.text


@pytest.mark.asyncio
async def test_shown_beside_the_verdict_never_changing_it(monkeypatch):
    from services import verify_compat
    from services.verify_v2 import VerificationV2

    async def fake_verify(claim, client_ip=None):
        return VerificationV2(claim=claim, verdict="No verdict", confidence="Low", headline="h")
    monkeypatch.setattr(verify_compat, "verify_v2", fake_verify)
    monkeypatch.setattr(fc.config, "GOOGLE_FACTCHECK_API_KEY", "k", raising=False)

    async def query(q):
        return [review("Crime fell 10% in 2023", rating="True")]
    monkeypatch.setattr(fc, "_query", query)
    out = await verify_compat.verify(CLAIM)
    assert out["display"]["fact_checks"][0]["rating"] == "True"
    assert out["display"]["verdict"]["label"] != "True"
