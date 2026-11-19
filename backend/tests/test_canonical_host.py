"""Search engines should index factcheckgov.app, not the vercel.app address."""
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"
HTML = {"accept": "text/html"}


def _on(host, **extra):
    return {"host": host, **extra}


class TestRedirect:
    @pytest.mark.parametrize("path", ["/", "/about", "/methodology", "/developers", "/privacy"])
    def test_pages_on_vercel_app_move_to_the_domain(self, test_client, path):
        r = test_client.get(path, headers=_on("stelthar-api.vercel.app", **HTML), follow_redirects=False)
        assert r.status_code == 301
        assert r.headers["location"] == f"https://factcheckgov.app{path}"

    def test_query_is_kept(self, test_client):
        r = test_client.get("/c?claim=x%20y", headers=_on("stelthar-api.vercel.app", **HTML),
                            follow_redirects=False)
        assert r.headers["location"] == "https://factcheckgov.app/c?claim=x%20y"

    def test_crawlers_are_moved_too(self, test_client):
        r = test_client.get("/", headers=_on("stelthar-api.vercel.app", **{
            "user-agent": "Mozilla/5.0 (compatible; bingbot/2.0)", "accept": "*/*"}),
            follow_redirects=False)
        assert r.status_code == 301

    def test_monitors_still_get_json_on_vercel_app(self, test_client):
        r = test_client.get("/", headers=_on("stelthar-api.vercel.app", accept="application/json"),
                            follow_redirects=False)
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_api_is_never_moved(self, test_client):
        r = test_client.post("/v2/verify", json={"claim": ""},
                             headers=_on("stelthar-api.vercel.app", **HTML), follow_redirects=False)
        assert r.status_code != 301

    def test_static_files_are_never_moved(self, test_client):
        r = test_client.get("/static/site.css", headers=_on("stelthar-api.vercel.app", **HTML),
                            follow_redirects=False)
        assert r.status_code == 200

    def test_canonical_host_and_previews_are_served(self, test_client):
        for host in ("factcheckgov.app", "stelthar-abc123-team.vercel.app"):
            r = test_client.get("/", headers=_on(host, **HTML), follow_redirects=False)
            assert r.status_code == 200 and "text/html" in r.headers["content-type"]


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
