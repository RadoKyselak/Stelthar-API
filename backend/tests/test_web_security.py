"""The public surface: the web page and API must be safe to expose.

Each test names an attack and checks the defence against it.
"""
import pathlib
import re
from unittest.mock import AsyncMock, patch

import pytest

import config
from utils import spend_guard

STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"
HTML = {"accept": "text/html"}


@pytest.fixture(autouse=True)
def fresh_counters():
    spend_guard._reset_for_tests()
    yield
    spend_guard._reset_for_tests()


class TestPage:
    def test_browsers_get_the_page_clients_get_json(self, test_client):
        page = test_client.get("/", headers=HTML)
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        assert "Check a claim" in page.text and "Find a number" in page.text
        assert test_client.get("/").json()["status"] == "ok"

    def test_injected_markup_cannot_run(self, test_client):
        """XSS: no inline script or style, no eval, no other origin."""
        csp = test_client.get("/", headers=HTML).headers["content-security-policy"]
        for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'",
                          "connect-src 'self'", "object-src 'none'", "base-uri 'none'"):
            assert directive in csp, directive
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "*" not in csp

    def test_page_cannot_be_framed(self, test_client):
        """Clickjacking."""
        h = test_client.get("/", headers=HTML).headers
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY"

    def test_common_headers_on_every_response(self, test_client):
        for r in (test_client.get("/", headers=HTML), test_client.get("/"),
                  test_client.get("/static/app.js"), test_client.get("/nope"),
                  test_client.post("/verify", json={})):
            h = r.headers
            assert h["x-content-type-options"] == "nosniff", r.url
            assert h["referrer-policy"] == "no-referrer"
            assert "max-age=" in h["strict-transport-security"]
            assert "camera=()" in h["permissions-policy"]

    def test_data_responses_are_never_a_page(self, test_client):
        csp = test_client.get("/").headers["content-security-policy"]
        assert csp.startswith("default-src 'none'") and "script-src" not in csp

    def test_page_source_has_no_inline_code_or_outside_resources(self):
        html = (STATIC / "index.html").read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline script"
        assert "<style" not in html and " style=" not in html and re.search(r"\son\w+=", html) is None
        for m in re.finditer(r'(?:src|href)="([^"]+)"', html):
            url = m.group(1)
            # Only the extension's store page may be linked off-site; nothing is loaded from one.
            assert url.startswith("/") or url.startswith("https://chromewebstore.google.com/"), url

    def test_scripts_never_write_markup(self):
        for name in ("app.js", "result-view.js"):
            code = re.sub(r"/\*.*?\*/|//[^\n]*", "", (STATIC / name).read_text(), flags=re.S)
            assert not re.search(r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write"
                                 r"|\beval\(|new Function\(|setAttribute\(\s*[\"']style", code), name

    def test_shared_links_never_run_a_check(self):
        """A link like /?claim=… posted widely must not make every visitor's
        browser send a check (each could cost a paid search)."""
        code = (STATIC / "app.js").read_text()
        deep = code[code.index("Deep links"):]
        assert "run(" not in deep and "check(" not in deep and "find(" not in deep


class TestFiles:
    @pytest.mark.parametrize("name", ["app.js", "result-view.js", "site.css", "logo.png", "favicon.png"])
    def test_allowed_files(self, test_client, name):
        r = test_client.get(f"/static/{name}")
        assert r.status_code == 200 and r.headers["cross-origin-resource-policy"] == "same-origin"

    @pytest.mark.parametrize("path", [
        "/static/index.html", "/static/..%2Fmain.py", "/static/%2e%2e%2fconfig%2f__init__.py",
        "/static/../main.py", "/static/.env", "/main.py", "/.env", "/static/",
    ])
    def test_nothing_else_on_disk_is_reachable(self, test_client, path):
        """Path traversal."""
        r = test_client.get(path)
        assert r.status_code == 404, path
        assert "import" not in r.text and "API_KEY" not in r.text


class TestNoMap:
    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
    def test_api_docs_are_off(self, test_client, path):
        assert test_client.get(path).status_code == 404

    def test_health_hides_keys_and_budget(self, test_client):
        body = test_client.get("/health").json()
        assert body == {"status": "healthy", "service": "stelthar-api", "version": body["version"]}

    def test_health_details_need_the_operator_token(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "HEALTH_TOKEN", "operator-token")
        assert "ai_search" not in test_client.get("/health", headers={"x-health-token": "wrong"}).json()
        assert "ai_search" in test_client.get("/health", headers={"x-health-token": "operator-token"}).json()

    def test_no_token_configured_means_no_details(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "HEALTH_TOKEN", "")
        assert "ai_search" not in test_client.get("/health", headers={"x-health-token": ""}).json()


class TestCrossSite:
    def test_other_websites_cannot_call_the_api(self, test_client):
        """Another site making its visitors' browsers call this API."""
        r = test_client.options("/verify", headers={
            "origin": "https://evil.example", "access-control-request-method": "POST",
            "access-control-request-headers": "content-type"})
        assert r.headers.get("access-control-allow-origin") is None
        r = test_client.post("/v2/lookup", json={"query": "gdp"}, headers={"origin": "https://evil.example"})
        assert r.headers.get("access-control-allow-origin") is None

    def test_the_extension_still_can(self, test_client):
        origin = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
        r = test_client.options("/verify", headers={
            "origin": origin, "access-control-request-method": "POST",
            "access-control-request-headers": "content-type"})
        assert r.headers.get("access-control-allow-origin") == origin
        assert r.headers.get("access-control-allow-credentials") is None


class TestFlooding:
    def test_checks_are_limited_per_visitor(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "VERIFY_PER_MINUTE", 3)
        fake = AsyncMock(return_value={"verdict": "Supported"})
        with patch("main.verify_compat.verify", fake):
            codes = [test_client.post("/verify", json={"claim": "GDP was $27 trillion in 2023"}).status_code
                     for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        r = test_client.post("/verify", json={"claim": "GDP was $27 trillion in 2023"})
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        assert fake.await_count == 3          # refused before any work was done

    def test_daily_limit(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "LOOKUP_PER_DAY", 2)
        fake = AsyncMock(return_value=type("R", (), {"ok": True, "reason": "", "metric": "x"})())
        with patch("main.lookup", fake), patch("main.lookup_to_response_dict", lambda r: {"ok": True}):
            codes = [test_client.post("/v2/lookup", json={"query": "national debt"}).status_code
                     for _ in range(3)]
        assert codes == [200, 200, 429] and fake.await_count == 2

    def test_limits_are_per_visitor(self, test_client, monkeypatch):
        monkeypatch.setattr(config, "VERIFY_PER_MINUTE", 1)
        fake = AsyncMock(return_value={"verdict": "Supported"})
        with patch("main.verify_compat.verify", fake):
            a = test_client.post("/verify", json={"claim": "GDP was $27 trillion in 2023"},
                                 headers={"x-vercel-forwarded-for": "198.51.100.1"})
            b = test_client.post("/verify", json={"claim": "GDP was $27 trillion in 2023"},
                                 headers={"x-vercel-forwarded-for": "198.51.100.2"})
        assert a.status_code == b.status_code == 200

    def test_store_outage_does_not_block_free_checks(self, test_client, monkeypatch):
        class Down:
            async def incr(self, *a, **k):
                raise RuntimeError("redis down")
        monkeypatch.setattr(spend_guard, "get_store", lambda: Down())
        with patch("main.verify_compat.verify", AsyncMock(return_value={"verdict": "Supported"})):
            assert test_client.post("/verify", json={"claim": "GDP was $27 trillion in 2023"}).status_code == 200

    def test_huge_bodies_are_refused_unread(self, test_client):
        r = test_client.post("/verify", content=b'{"claim":"' + b"x" * 40000 + b'"}',
                             headers={"content-type": "application/json"})
        assert r.status_code == 413

    def test_errors_do_not_leak_internals(self, test_client):
        from exceptions import MiradorException
        with patch("main.verify_compat.verify",
                   AsyncMock(side_effect=MiradorException("Something failed", details={"key": "SECRET"}))):
            r = test_client.post("/verify", json={"claim": "GDP was $27 trillion in 2023"})
        assert r.status_code == 500 and "SECRET" not in r.text


def test_privacy_page_is_served_with_the_page_policy(test_client):
    r = test_client.get("/privacy")
    assert r.status_code == 200 and "Privacy" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "<script" not in r.text and "style=" not in r.text
