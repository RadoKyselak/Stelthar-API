"""The public benchmark page: Facticity's own 120-claim benchmark with
Project Mirador added, generated from eval/results/facticity_benchmark.json."""
import json
import pathlib
import re

import pytest

from eval import facticity_benchmark as fb

BACK = pathlib.Path(__file__).resolve().parent.parent
PAGE = (BACK / "static" / "benchmarks.html").read_text()
DATA = json.loads((BACK / "eval" / "results" / "facticity_benchmark.json").read_text())


class TestTheResults:
    def test_page_is_exactly_what_the_results_generate(self):
        assert PAGE == fb.page(DATA)

    def test_accuracy_is_computed_from_the_rows(self):
        for s, a in DATA["accuracy"].items():
            assert a == fb.accuracy(DATA["rows"], s)

    def test_the_published_answers_reproduce_facticitys_own_figures(self):
        assert [round(DATA["accuracy"][s]) for s in ("facticity", "perplexity", "bing")] == [92, 88, 78]

    def test_all_120_claims_are_listed_with_every_answer(self):
        assert len(DATA["rows"]) == 120 and PAGE.count("<tr><td") == 120
        assert all(r["mirador"] in ("TRUE", "FALSE", "Can't verify") for r in DATA["rows"])

    @pytest.mark.parametrize("verdict,shown", [("SUPPORTED", "TRUE"), ("MOSTLY_ACCURATE", "TRUE"),
                                               ("CONTRADICTED", "FALSE"), ("MISLEADING", "FALSE"),
                                               ("NO_EVIDENCE", "Can't verify"), ("", "Can't verify")])
    def test_verdicts_read_as_the_service_shows_them(self, verdict, shown):
        assert fb.answer(verdict) == shown


class TestThePage:
    def test_no_model_vendor_is_named(self):
        assert not re.search(r"claude|haiku|sonnet|anthropic", PAGE, re.I)

    def test_old_metrics_are_gone(self):
        assert not re.search(r"quantemp|econwebarena|macro|politifact rated", PAGE, re.I)

    def test_it_is_served_and_listed(self, test_client):
        r = test_client.get("/benchmarks")
        assert r.status_code == 200 and "Project Mirador" in r.text
        assert "/benchmarks</loc>" in (BACK / "static" / "sitemap.xml").read_text()

    @pytest.mark.parametrize("name", ["index.html", "about.html", "privacy.html", "methodology.html",
                                      "developers.html", "benchmarks.html"])
    def test_every_page_links_to_it(self, name):
        assert '<a href="/benchmarks">Benchmarks</a>' in (BACK / "static" / name).read_text()

    def test_no_inline_script_or_style(self):
        assert "<script" not in PAGE and "style=" not in PAGE
