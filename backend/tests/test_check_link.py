"""Checking a page by its address (utils/safe_fetch.py, claims/article.py,
services/check_link.py).

The server downloads an address a stranger typed, so most of what is tested is
where a request may go. The network is replaced throughout: a fake resolver
and a fake transport, so no test fetches anything.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import config
from claims import article
from services import check_link as cl
from utils import safe_fetch as sf
from utils.safe_fetch import FetchFailed, FetchRefused

PUBLIC = "93.184.216.34"


@pytest.fixture(autouse=True)
def fresh():
    from utils import spend_guard
    spend_guard._reset_for_tests()
    cl._CACHE.clear()
    yield
    spend_guard._reset_for_tests()
    cl._CACHE.clear()


def resolver(table):
    async def resolve(host, port):
        ips = table.get(host)
        if ips is None:
            raise FetchFailed("That site could not be found.")
        if not all(sf._is_public(ip) for ip in ips):
            raise FetchRefused("That address is not on the public web.")
        return ips
    return resolve


def site(handler):
    seen = []

    def handle(request):
        seen.append(request)
        return handler(request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False)
    return client, seen


class TestAddresses:
    @pytest.mark.parametrize("url", [
        "ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)", "example.com/x", "",
        "http://example.com:8080/x",                # not a standard port
        "http://user:pass@example.com/x",           # credentials in the address
        "http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data/", "http://[::1]/x",
        "http://2130706433/x",                      # 127.0.0.1 written as one number
        "http://localhost/x", "http://intranet/x", "http://db.internal/x", "http://printer.local/x",
    ])
    def test_refused_by_how_it_is_written(self, url):
        with pytest.raises(FetchRefused):
            sf.check_address(url)

    def test_an_ordinary_page_passes(self):
        assert sf.check_address("https://Example.COM/a/b?x=1#frag") == ("https", "example.com", 443, "/a/b?x=1")

    @pytest.mark.parametrize("ip", ["10.0.0.5", "172.16.3.4", "192.168.1.1", "127.0.0.1", "169.254.169.254",
                                    "0.0.0.0", "100.64.0.1", "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1",
                                    "224.0.0.1", "not an address"])
    def test_addresses_that_are_not_public(self, ip):
        assert sf._is_public(ip) is False

    @pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
    def test_addresses_that_are(self, ip):
        assert sf._is_public(ip) is True


class TestFetch:
    async def test_connects_to_the_address_that_was_checked(self, monkeypatch):
        """Not to the name, which could resolve to something else a moment later."""
        monkeypatch.setattr(sf, "_resolve", resolver({"example.com": [PUBLIC]}))
        client, seen = site(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text="<p>hi</p>"))
        page = await sf.fetch_public_page("https://example.com/story?id=7", client=client)
        assert page.text == "<p>hi</p>" and page.host == "example.com" and page.url == "https://example.com/story?id=7"
        req = seen[0]
        assert req.url.host == PUBLIC and req.headers["host"] == "example.com"
        assert req.extensions["sni_hostname"] == "example.com"          # the certificate is checked by name
        assert "ProjectMirador" in req.headers["user-agent"]            # says who is asking

    async def test_a_name_that_resolves_to_a_private_address_is_refused(self, monkeypatch):
        monkeypatch.setattr(sf, "_resolve", resolver({"evil.example": ["10.0.0.5"]}))
        client, seen = site(lambda r: httpx.Response(200, text="secret"))
        with pytest.raises(FetchRefused):
            await sf.fetch_public_page("https://evil.example/", client=client)
        assert seen == []                                               # nothing was sent

    async def test_one_private_address_among_several_is_enough(self, monkeypatch):
        monkeypatch.setattr(sf, "_resolve", resolver({"mixed.example": [PUBLIC, "192.168.0.9"]}))
        client, seen = site(lambda r: httpx.Response(200, text="x"))
        with pytest.raises(FetchRefused):
            await sf.fetch_public_page("https://mixed.example/", client=client)
        assert seen == []

    async def test_a_redirect_is_checked_like_a_first_address(self, monkeypatch):
        monkeypatch.setattr(sf, "_resolve", resolver({"example.com": [PUBLIC]}))
        client, seen = site(lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"}))
        with pytest.raises(FetchRefused):
            await sf.fetch_public_page("https://example.com/go", client=client)
        assert len(seen) == 1                                           # the redirect was not followed

    async def test_a_redirect_to_another_public_page_is_followed(self, monkeypatch):
        monkeypatch.setattr(sf, "_resolve", resolver({"example.com": [PUBLIC], "www.example.com": [PUBLIC]}))

        def handler(r):
            if r.headers["host"] == "example.com":
                return httpx.Response(301, headers={"location": "https://www.example.com/story"})
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text="<p>there</p>")
        client, _ = site(handler)
        page = await sf.fetch_public_page("https://example.com/story", client=client)
        assert page.host == "www.example.com" and page.text == "<p>there</p>"

    async def test_redirects_stop(self, monkeypatch):
        monkeypatch.setattr(sf, "_resolve", resolver({"example.com": [PUBLIC]}))
        client, seen = site(lambda r: httpx.Response(302, headers={"location": "/again"}))
        with pytest.raises(FetchFailed):
            await sf.fetch_public_page("https://example.com/", client=client)
        assert len(seen) == sf.MAX_REDIRECTS + 1

    @pytest.mark.parametrize("status", [401, 403, 451])
    async def test_a_site_that_refuses_readers_is_said_plainly(self, monkeypatch, status):
        monkeypatch.setattr(sf, "_resolve", resolver({"example.com": [PUBLIC]}))
        client, _ = site(lambda r: httpx.Response(status, text="no"))
        with pytest.raises(FetchFailed, match="does not let its pages be read"):
            await sf.fetch_public_page("https://example.com/", client=client)

    async def test_only_text_is_read(self, monkeypatch):
        monkeypatch.setattr(sf, "_resolve", resolver({"example.com": [PUBLIC]}))
        client, _ = site(lambda r: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF"))
        with pytest.raises(FetchFailed, match="not a page of text"):
            await sf.fetch_public_page("https://example.com/report.pdf", client=client)

    async def test_a_page_is_cut_at_the_size_cap(self, monkeypatch):
        monkeypatch.setattr(sf, "_resolve", resolver({"example.com": [PUBLIC]}))
        monkeypatch.setattr(sf, "MAX_BYTES", 1000)
        client, _ = site(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text="a" * 5000))
        page = await sf.fetch_public_page("https://example.com/", client=client)
        assert len(page.text) == 1000

    async def test_a_site_that_does_not_answer(self, monkeypatch):
        monkeypatch.setattr(sf, "_resolve", resolver({"example.com": [PUBLIC]}))

        def handler(r):
            raise httpx.ReadTimeout("slow")
        client, _ = site(handler)
        with pytest.raises(FetchFailed, match="did not respond"):
            await sf.fetch_public_page("https://example.com/", client=client)


STORY = """<html><head><title>Fallback title</title><meta property="og:title" content="Jobs report: October">
<script>var x = "Unemployment was 99% in 2023 according to this script";</script></head><body>
<nav><p>Home page, about us, contact, subscribe to the newsletter and more links here</p></nav>
<article>
  <h1>Jobs report for October shows the labor market holding steady</h1>
  <p>The unemployment rate was 4.1 percent in October 2026, the Labor Department said.[1]</p>
  <div class="share-tools"><p>Share this article with your friends on social media right now</p></div>
  <p>Employers added jobs for the tenth month in a row, according to the report released Friday.</p>
  <ul><li>Wages rose faster than prices over the year, the report also showed in its tables.</li></ul>
  <p>Short one.</p>
</article>
<footer><p>Copyright 2026 Example News. All rights reserved. Terms of use and privacy policy apply.</p></footer>
</body></html>"""


class TestArticle:
    def test_the_story_is_kept_and_the_furniture_is_not(self):
        got = article.extract(STORY)
        assert got.title == "Jobs report: October"
        assert got.blocks == [
            "Jobs report for October shows the labor market holding steady",
            "The unemployment rate was 4.1 percent in October 2026, the Labor Department said.",
            "Employers added jobs for the tenth month in a row, according to the report released Friday.",
            "Wages rose faster than prices over the year, the report also showed in its tables.",
        ]

    def test_nothing_in_a_script_is_ever_text(self):
        assert "99%" not in article.extract(STORY).text

    def test_preformatted_releases_keep_their_paragraphs(self):
        html = ("<html><body><pre>Total nonfarm payroll employment rose by 165,000 in October,\n"
                "and the unemployment rate changed little at 4.1 percent.\n\n"
                "The number of long-term unemployed was little changed at 1.6 million in October.\n</pre></body></html>")
        got = article.extract(html)
        assert len(got.blocks) == 2 and got.blocks[0].startswith("Total nonfarm payroll employment rose by 165,000")

    def test_a_page_built_by_scripts_falls_back_to_its_structured_data(self):
        body = "The unemployment rate was 4.1 percent in October 2026, the Labor Department said. " * 6
        html = ('<html><head><script type="application/ld+json">{"@type": "NewsArticle", "articleBody": "%s"}'
                '</script></head><body><div id="root"></div></body></html>' % body)
        assert article.extract(html).blocks[0].startswith("The unemployment rate was 4.1 percent")

    def test_table_cells_are_not_sentences(self):
        html = ("<html><body><table><tr><td>Unemployment rate, seasonally adjusted, all persons 4.1% (2026)</td></tr>"
                "</table></body></html>")
        assert article.extract(html).blocks == []

    @pytest.mark.parametrize("html", ["", "<html>", "<p>" * 2000, "\x00\x01 not html at all"])
    def test_a_broken_page_is_an_empty_one(self, html):
        assert article.extract(html).blocks == []

    def test_the_text_is_capped(self):
        html = "<article>" + "".join(f"<p>Paragraph number {i} has enough words in it to count as text.</p>"
                                     for i in range(2000)) + "</article>"
        assert len(article.extract(html, max_chars=5000).text) <= 5100


class TestCheckLink:
    def fetched(self, text=STORY, ctype="text/html"):
        return AsyncMock(return_value=sf.Page(url="https://example.gov/story", host="example.gov",
                                              content_type=ctype, text=text))

    async def test_the_page_is_checked_as_a_page(self):
        checked = AsyncMock(return_value={"claims": [], "count": 0, "tally": {}, "truncated": False})
        with patch.object(cl, "fetch_public_page", self.fetched()), patch.object(cl, "check_text", checked):
            got = await cl.check_link("https://example.gov/story")
        assert checked.await_args.kwargs["mode"] == "page"              # free data only, strict selection
        assert "The unemployment rate was 4.1 percent in October 2026" in checked.await_args.args[0]
        assert got["page"] == {"url": "https://example.gov/story", "host": "example.gov",
                               "title": "Jobs report: October", "paragraphs": 4}

    @pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=abc", "https://youtu.be/abc",
                                     "https://x.com/someone/status/1", "https://m.tiktok.com/v/1"])
    async def test_video_and_social_pages_are_refused_before_any_fetch(self, url):
        fetch = self.fetched()
        with patch.object(cl, "fetch_public_page", fetch):
            with pytest.raises(FetchRefused, match="can't be read"):
                await cl.check_link(url)
        assert fetch.await_count == 0

    async def test_a_page_with_no_text_says_what_works_instead(self):
        with patch.object(cl, "fetch_public_page", self.fetched("<html><body><div id='app'></div></body></html>")):
            with pytest.raises(FetchFailed, match="Highlight the text on the page"):
                await cl.check_link("https://example.gov/app")

    async def test_a_refusing_site_says_what_works_instead(self):
        failing = AsyncMock(side_effect=FetchFailed("That site does not let its pages be read this way."))
        with patch.object(cl, "fetch_public_page", failing):
            with pytest.raises(FetchFailed, match="read this way. Highlight the text"):
                await cl.check_link("https://example.com/story")

    async def test_a_recheck_does_not_fetch_again(self):
        fetch = self.fetched()
        checked = AsyncMock(return_value={"claims": [], "count": 0, "tally": {}, "truncated": False})
        with patch.object(cl, "fetch_public_page", fetch), patch.object(cl, "check_text", checked):
            await cl.check_link("https://example.gov/story")
            await cl.check_link("https://example.gov/story")
        assert fetch.await_count == 1

    @pytest.mark.parametrize("text,expected", [
        ("https://example.gov/release", True), ("  http://example.gov/a?b=1  ", True),
        ("See https://example.gov/release for details", False), ("Unemployment was 3.6% in 2023", False),
        ("https://example.gov/a\nhttps://example.gov/b", False),
    ])
    def test_a_lone_address_is_a_link(self, text, expected):
        assert cl.looks_like_link(text) is expected


class TestEndpoint:
    def result(self):
        return {"mode": "page", "claims": [], "count": 0, "tally": {}, "truncated": False, "model_calls": 0,
                "elapsed_seconds": 0.1, "page": {"url": "https://example.gov/x", "host": "example.gov",
                                                 "title": "T", "paragraphs": 3}}

    def test_a_url_is_checked_as_a_page(self, test_client):
        fake = AsyncMock(return_value=self.result())
        with patch("routes.check.check_link", fake):
            r = test_client.post("/v2/check", json={"url": "https://example.gov/x"})
        assert r.status_code == 200 and r.json()["page"]["host"] == "example.gov"
        assert fake.await_args.args[0] == "https://example.gov/x"

    def test_why_a_page_could_not_be_read_reaches_the_person(self, test_client):
        with patch("routes.check.check_link", AsyncMock(side_effect=FetchRefused("That address is not on the "
                                                                                 "public web."))):
            r = test_client.post("/v2/check", json={"url": "http://10.0.0.5/admin"})
        assert r.status_code == 400 and r.json()["detail"] == "That address is not on the public web."

    @pytest.mark.parametrize("body", [{}, {"text": "GDP grew 2.5% in 2023", "url": "https://example.gov/x"}])
    def test_text_or_a_url_never_both_or_neither(self, test_client, body):
        assert test_client.post("/v2/check", json=body).status_code == 422

    def test_switched_off(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "CHECK_LINKS", False)
        fake = AsyncMock(return_value=self.result())
        with patch("routes.check.check_link", fake):
            r = test_client.post("/v2/check", json={"url": "https://example.gov/x"})
        assert r.status_code == 400 and "switched off" in r.json()["detail"] and fake.await_count == 0

    def test_it_counts_against_the_passage_allowance(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "CHECK_PER_MINUTE", 1)
        with patch("routes.check.check_link", AsyncMock(return_value=self.result())):
            codes = [test_client.post("/v2/check", json={"url": "https://example.gov/x"}).status_code
                     for _ in range(2)]
        assert codes == [200, 429]
