"""Fewer paid calls for the same answers: shared, wider cache keys."""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

import evidence.cache as cache_mod
from evidence import ai_search
from evidence.datasets import Candidate, picker


@pytest.fixture(autouse=True)
def cold_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path / "cache")


class TestPickShape:
    def test_only_the_stated_figure_is_blanked(self):
        assert picker.shape("Texas unemployment was 4.1% in 2023", "4.1%") == \
            "Texas unemployment was #% in 2023"
        assert picker.shape("Texas unemployment was 3.9% in 2023", "3.9%") == \
            picker.shape("Texas unemployment was 4.1% in 2023", "4.1%")

    def test_numbers_that_name_the_measure_are_kept(self):
        a = picker.shape("17% of Texans are aged 65 and over", "17%")
        b = picker.shape("17% of Texans are aged 18 and over", "17%")
        assert a != b and "65" in a

    def test_unknown_figure_leaves_the_claim_alone(self):
        assert picker.shape("Cook County had 5.1 million people", "") == "Cook County had 5.1 million people"
        assert picker.shape("Cook County had 5.1 million people", "nope") == "Cook County had 5.1 million people"


class _Store:
    kind = "fake"

    def __init__(self):
        self.d = {}

    async def get_str(self, k):
        return self.d.get(k)

    async def set_str(self, k, v, ttl):
        self.d[k] = v


@pytest.mark.asyncio
class TestSharedPickCache:
    async def test_second_figure_reuses_the_first_pick(self, monkeypatch):
        store = _Store()
        monkeypatch.setattr(picker.spend_guard, "get_store", lambda: store)
        c = Candidate("census_profile", "DP02_0094PE", "Foreign-born", "percent")
        await picker._store_pick("k", {"pick": "S1", "stat": "value"})
        assert any(k.startswith("ai:pick:") for k in store.d)
        key1 = picker.shape("27% of Californians were born abroad", "27%")
        key2 = picker.shape("26.6% of Californians were born abroad", "26.6%")
        assert key1 == key2
        # A cached pick is served without a reservation or a model call.
        reserve = AsyncMock()
        monkeypatch.setattr(picker.spend_guard, "reserve_pick", reserve)
        listing = c.line("S1")
        import hashlib
        k = hashlib.sha256(f"{picker._PROMPT_VERSION}|{key1.lower()}|California|2023|{listing}".encode()).hexdigest()
        await picker._store_pick(k, {"pick": "S1", "stat": "value"})
        got = await picker.pick("26.6% of Californians were born abroad", "California", "2023", [c],
                                "1.2.3.4", figure="26.6%")
        assert got.candidate is c and got.model_calls == 0 and got.reason == "cached"
        reserve.assert_not_called()

    async def test_store_outage_falls_back_to_disk(self, monkeypatch):
        class Down(_Store):
            async def get_str(self, k):
                raise RuntimeError("down")

            async def set_str(self, k, v, ttl):
                raise RuntimeError("down")
        monkeypatch.setattr(picker.spend_guard, "get_store", lambda: Down())
        await picker._store_pick("k2", {"pick": "S2", "stat": "peak"})
        assert await picker._cached_pick("k2") == {"pick": "S2", "stat": "peak"}


class TestAnswerCacheKey:
    @pytest.mark.parametrize("a,b", [
        ("Unemployment was 3.6% in 2023.", "unemployment was 3.6% in 2023"),
        ("“The debt hit $34 trillion in 2023”", '"The debt hit $34 trillion in 2023"'),
        ("Wages rose 4%—the most in 2023", "Wages rose 4%-the most in 2023"),
    ])
    def test_same_sentence_same_key(self, a, b):
        assert ai_search._cache_key(a, "") == ai_search._cache_key(b, "")

    def test_different_figures_differ(self):
        assert ai_search._cache_key("x was 3% in 2023", "") != ai_search._cache_key("x was 4% in 2023", "")

    def test_past_year_kept_a_week_present_a_day(self):
        last = datetime.now(timezone.utc).year - 1
        assert ai_search.ttl_for(f"Egg prices rose 30% in {last}") == 7 * 24 * 3600
        assert ai_search.ttl_for("Egg prices are up 30% now") == 24 * 3600
        assert ai_search.ttl_for(f"Egg prices are up 30% since {last}, currently") == 24 * 3600
        assert ai_search.ttl_for(f"Egg prices rose 30% in {last + 1}") == 24 * 3600
