"""Scoring Facticity's benchmark (eval only; nothing is published on the site)."""
import json
import pathlib

import pytest

from eval import facticity_benchmark as fb

DATA = json.loads((pathlib.Path(__file__).resolve().parent.parent / "eval" / "results"
                   / "facticity_benchmark.json").read_text())


def test_the_published_answers_reproduce_facticitys_table():
    assert [round(fb.accuracy(DATA["rows"], s)) for s in ("facticity", "perplexity", "bing")] == [92, 88, 78]


@pytest.mark.parametrize("verdict,shown", [("SUPPORTED", "TRUE"), ("MOSTLY_ACCURATE", "TRUE"),
                                           ("CONTRADICTED", "FALSE"), ("MISLEADING", "FALSE"),
                                           ("NO_EVIDENCE", "Can't verify"), ("", "Can't verify")])
def test_verdicts_read_as_the_service_shows_them(verdict, shown):
    assert fb.answer(verdict) == shown


def test_government_claims_are_marked_and_scored():
    rows = fb.in_focus(DATA["rows"])
    assert len(rows) == 25 and fb.accuracy(rows, "mirador") == DATA["accuracy_in_focus"]["mirador"]


def test_the_benchmarks_page_is_gone(test_client):
    assert test_client.get("/benchmarks").status_code == 404
    static = pathlib.Path(__file__).resolve().parent.parent / "static"
    assert not (static / "benchmarks.html").exists()
    assert all("/benchmarks" not in p.read_text() for p in static.glob("*.html"))
    assert "/benchmarks" not in (static / "sitemap.xml").read_text()
