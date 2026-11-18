"""The public benchmark page: one graph, stating only what
eval/results/benchmarks.json records, and all of it."""
import json
import pathlib
import re

import pytest

BACK = pathlib.Path(__file__).resolve().parent.parent
PAGE = (BACK / "static" / "benchmarks.html").read_text()
DATA = json.loads((BACK / "eval" / "results" / "benchmarks.json").read_text())
CLAIMS = PAGE.split("<h2>Rating claims</h2>")[1]
LOOKUP = PAGE.split("<h2>Looking up government figures</h2>")[1].split("<h2>")[0]
ROWS = re.findall(r'<li( class="bench-us")?><span class="bench-name">(.*?)</span>'
                  r'<meter min="0" max="100" value="([\d.]+)"></meter><span class="bench-score">([\d.]+)</span>', CLAIMS)


class TestTheGraph:
    def test_two_graphs_lookup_first(self):
        assert PAGE.count('<ol class="bench">') == 2 and PAGE.count("<h2>") == 2
        assert PAGE.index("Looking up government figures") < PAGE.index("Rating claims")

    def test_lookup_graph_is_what_was_recorded(self):
        L = DATA["lookup"]
        got = dict(re.findall(r'<span class="bench-name">(.*?)</span><meter min="0" max="75" value="(\d+)">', LOOKUP))
        assert got == {"Right": str(L["right"]), "Wrong": str(L["wrong"]), "Not answered": str(L["not_answered"])}
        assert L["right"] + L["wrong"] + L["not_answered"] == L["questions"]
        assert sum(L["right_detail"].values()) == L["right"]

    def test_every_recorded_score_is_a_bar_and_nothing_else_is(self):
        recorded = sorted([m["score"] for m in DATA["mirador"]] + [o["score"] for o in DATA["others"]])
        assert sorted(float(v) for _, _, v, _ in ROWS) == recorded

    def test_each_bar_shows_its_value_rounded(self):
        assert all(f"{float(v):.1f}" == shown for _, _, v, shown in ROWS)

    def test_the_bars_are_in_order(self):
        values = [float(v) for _, _, v, _ in ROWS]
        assert values == sorted(values, reverse=True)

    def test_mirador_bars_are_set_apart_and_named_by_mode(self):
        ours = {name: float(v) for us, name, v, _ in ROWS if us}
        assert ours == {f"Project Mirador ({m['mode']})": m["score"] for m in DATA["mirador"]}

    def test_the_mode_the_live_site_runs_is_said_and_is_its_model(self):
        import config
        live = [m for m in DATA["mirador"] if m["live"]]
        assert len(live) == 1 and live[0]["model"] == config.CLAUDE_SEARCH_MODEL
        assert f"The live site runs {live[0]['mode']} mode" in PAGE

    def test_the_margin_is_stated(self):
        assert f"{DATA['mirador'][0]['claims']} random claims from the test, so &plusmn;{DATA['margin']}" in PAGE

    def test_no_jargon_and_no_method_link(self):
        assert not re.search(r"roberta|bart|numt5|flan|program-fc|how it was run", PAGE, re.I)

    def test_no_model_vendor_is_named_for_mirador(self):
        assert not re.search(r"claude|haiku|sonnet|anthropic", PAGE, re.I)


class TestThePage:
    def test_it_is_served_and_listed(self, test_client):
        r = test_client.get("/benchmarks")
        assert r.status_code == 200 and "Project Mirador (fast)" in r.text
        assert "/benchmarks</loc>" in (BACK / "static" / "sitemap.xml").read_text()

    @pytest.mark.parametrize("name", ["index.html", "about.html", "privacy.html", "methodology.html",
                                      "developers.html", "benchmarks.html"])
    def test_every_page_links_to_it(self, name):
        assert '<a href="/benchmarks">Benchmarks</a>' in (BACK / "static" / name).read_text()

    def test_no_inline_script_or_style(self):
        assert "<script" not in PAGE and "style=" not in PAGE
