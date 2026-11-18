"""The site's pages added with the passage check: the method, the developer
page and the home page's own explanation of itself."""
import pathlib
import re

import pytest

import config

BACKEND = pathlib.Path(__file__).resolve().parents[1]
STATIC = BACKEND / "static"
HTML = {"accept": "text/html"}


def page(name: str) -> str:
    """A page's text with runs of whitespace collapsed, as a reader sees it."""
    return " ".join((STATIC / name).read_text().split())


def default(name: str) -> int:
    """A limit's default as config declares it. The test suite raises the live
    values so its own requests are never refused."""
    source = (BACKEND / "config" / "__init__.py").read_text()
    return int(re.search(rf'{name} = _env_int\("{name}", (\d+),', source).group(1))


class TestTextPages:
    @pytest.mark.parametrize("path,needle", [
        ("/methodology", "What the verdicts mean"),
        ("/developers", "POST /v2/check"),
    ])
    def test_served_as_pages(self, test_client, path, needle):
        r = test_client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert needle in r.text
        assert "script-src 'self'" in r.headers["content-security-policy"]

    @pytest.mark.parametrize("name", ["methodology.html", "developers.html", "index.html", "about.html",
                                      "privacy.html"])
    def test_no_inline_script_or_style(self, name):
        """The site's security policy allows neither; either would silently not run."""
        html = (STATIC / name).read_text()
        assert not re.search(r"<style\b", html) and not re.search(r'\sstyle="', html)
        for attrs in re.findall(r"<script\b([^>]*)>", html):
            assert "src=" in attrs or "application/ld+json" in attrs or "application/json" in attrs

    def test_search_engines_are_told(self, test_client):
        sitemap = test_client.get("/sitemap.xml").text
        assert "/methodology</loc>" in sitemap and "/developers</loc>" in sitemap

    def test_every_page_links_the_others(self):
        for name in ("index.html", "about.html", "privacy.html", "methodology.html", "developers.html"):
            foot = re.search(r'<footer class="foot">(.*?)</footer>', (STATIC / name).read_text(), re.S).group(1)
            for href in ("/about", "/methodology", "/developers", "/privacy"):
                assert f'href="{href}"' in foot, (name, href)


class TestWhatThePagesPromise:
    """Numbers and rules stated on the pages, held to the code they describe."""

    def test_the_limits_on_the_developer_page(self):
        text = page("developers.html")
        d = default
        assert f"{d('VERIFY_PER_MINUTE')} claim checks a minute and {d('VERIFY_PER_DAY')} a day" in text
        assert f"{d('CHECK_PER_MINUTE')} passage checks a minute and {d('CHECK_PER_DAY')} a day" in text
        assert f"{d('LOOKUP_PER_MINUTE')} lookups a minute and {d('LOOKUP_PER_DAY'):,} a day" in text
        assert f"up to {d('CHECK_MAX_CHARS'):,} characters" in text

    def test_the_verdict_rules_on_the_method_page(self):
        from claims import verdict as V
        text = page("methodology.html")
        assert f"no more than {V.NEAR_MISS_MAX_RELATIVE:.0%} of the official figure" in text
        assert f"a gap of more than {V.ROUNDING_MAX_RELATIVE:.0%}" in text

    def test_first_release_coverage_matches_the_engine(self):
        from services import derived
        assert set(derived._FIRST_PRINT) == {"unemployment_rate", "real_gdp_growth", "jobs_added", "gdp"}
        assert ("unemployment rate, for the jobs added in a month, for real GDP growth by quarter and year, "
                "and for a year&rsquo;s GDP in dollars") in " ".join(page("methodology.html").split())

    def test_the_test_run_on_the_method_page_is_the_one_on_file(self):
        """The score stated on the site, held to the committed results of that run."""
        import json
        run = json.loads((BACKEND / "eval" / "results" / "2026-11-17.json").read_text())
        declined = sum(1 for o in run["outcomes"] if o["expected"] == "No verdict" and o["correct"])
        assert run["degraded"] == 0 and run["model_calls"] == 0 and run["accuracy"] is not None
        text = page("methodology.html")
        assert (f"on November 17, 2026, checked {run['claims']} claims: {run['correct']} were answered "
                f"correctly, {declined} of them by rightly declining, and no model was called") in text

    def test_the_home_page_names_as_many_sources_as_the_about_page_lists(self):
        """"...the Treasury and ten more": three named plus ten."""
        answer = re.search(r"<h3>Where do the answers come from\?</h3> <p>.*?not from web pages: (.*?)</p>",
                           page("about.html")).group(1)
        named = [a for a in re.split(r" and, for other countries, |, ", answer.rstrip(".")) if a.strip()]
        assert len(named) == 13, named
        assert "ten more" in page("index.html")

    def test_the_home_page_examples_are_claims_not_lookups(self, test_client):
        app = (STATIC / "app.js").read_text()
        assert "V.examples.concat" in app
        assert 'postJSON("/v2/check", V.isLink(text) ? { url: text } : { text })' in app
