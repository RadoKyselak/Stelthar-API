"""Search engines should index factcheckgov.app, not the vercel.app address.

No redirect: visitors who come by the old address see the same site. Only
the canonical tags, sitemap and robots.txt tell search engines which copy to index."""
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"


class TestSignals:
    @pytest.mark.parametrize("name,path", [("index.html", "/"), ("about.html", "/about"),
                                           ("methodology.html", "/methodology"),
                                           ("developers.html", "/developers"),
                                           ("privacy.html", "/privacy")])
    def test_each_page_names_its_canonical_address(self, name, path):
        assert f'<link rel="canonical" href="https://factcheckgov.app{path}">' in (STATIC / name).read_text()

    def test_sitemap_and_robots_use_the_domain(self, test_client):
        sitemap = test_client.get("/sitemap.xml").text
        assert "https://factcheckgov.app/" in sitemap and "vercel.app" not in sitemap
        assert "Sitemap: https://factcheckgov.app/sitemap.xml" in test_client.get("/robots.txt").text

    def test_indexnow_key_is_served(self, test_client):
        key = "1c008d11c7ba75cfa5507734063fa160"
        assert test_client.get(f"/{key}.txt").text.strip() == key


def test_old_address_still_serves_the_site(test_client):
    r = test_client.get("/", headers={"host": "stelthar-api.vercel.app", "accept": "text/html"},
                        follow_redirects=False)
    assert r.status_code == 200 and '<link rel="canonical" href="https://factcheckgov.app/">' in r.text
