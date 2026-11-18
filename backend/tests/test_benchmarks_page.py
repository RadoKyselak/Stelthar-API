"""The public benchmarks page states only what eval/results/benchmarks.json
records, and states all of it."""
import json
import pathlib
import re

import pytest

BACK = pathlib.Path(__file__).resolve().parent.parent
PAGE = (BACK / "static" / "benchmarks.html").read_text()
DATA = json.loads((BACK / "eval" / "results" / "benchmarks.json").read_text())


def text():
    return " ".join(re.sub(r"<[^>]+>", " ", PAGE).split())


class TestTheNumbers:
    def test_every_quantemp_bar_is_a_recorded_score(self):
        recorded = {DATA["quantemp"]["mirador"]["score"]} | {o["score"] for o in DATA["quantemp"]["others"]}
        bars = {float(v) for v in re.findall(r'<meter min="0" max="100" value="([\d.]+)"', PAGE)}
        assert bars == recorded

    def test_each_bar_shows_its_own_value_rounded(self):
        for value, shown in re.findall(r'value="([\d.]+)"></meter><span class="bench-score">([\d.]+)</span>', PAGE):
            assert f"{float(value):.1f}" == shown

    def test_mirador_is_the_one_bar_set_apart_and_its_margin_is_stated(self):
        m = DATA["quantemp"]["mirador"]
        assert PAGE.count('class="bench-us"') == 1
        assert f'value="{m["score"]:.2f}"' in PAGE.split('class="bench-us"')[1].split("</li>")[0]
        assert f"{m['claims']} random claims, so &plusmn;{round((m['high'] - m['low']) / 2)}" in PAGE

    def test_the_bars_are_in_order(self):
        bars = [float(v) for v in re.findall(r'value="([\d.]+)"></meter>', PAGE)]
        assert bars == sorted(bars, reverse=True)

    def test_econwebarena(self):
        e, t = DATA["econwebarena"], text()
        assert f"{e['correct']} of {e['answered']} answered correctly" in t
        assert f"{e['answered']} of {e['questions']} answered" in t
        assert f"{e['wrong']} wrong" in t
        assert f"{e['revised_since']} answers differ" in t and f"{e['benchmark_errors']} of the benchmark" in t

    def test_own_tests(self):
        for o in DATA["own"]:
            assert f"{o['right']} of {o['of']}" in text()

    def test_the_model_named_is_the_one_the_live_site_uses(self):
        import config
        assert DATA["model"] == config.CLAUDE_SEARCH_MODEL
        assert "with Haiku, the AI model the live site uses" in PAGE


class TestThePage:
    def test_it_is_served_and_listed(self, test_client):
        r = test_client.get("/benchmarks")
        assert r.status_code == 200 and "Real fact-checked claims" in r.text
        assert "/benchmarks</loc>" in (BACK / "static" / "sitemap.xml").read_text()

    @pytest.mark.parametrize("name", ["index.html", "about.html", "privacy.html", "methodology.html",
                                      "developers.html", "benchmarks.html"])
    def test_every_page_links_to_it(self, name):
        assert '<a href="/benchmarks">Benchmarks</a>' in (BACK / "static" / name).read_text()

    def test_no_inline_script_or_style(self):
        assert "<script" not in PAGE and "style=" not in PAGE
